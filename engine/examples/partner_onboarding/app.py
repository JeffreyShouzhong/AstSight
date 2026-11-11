"""AstSight 最小示例服务：合伙人注册 P-03（FastAPI）。

只实现《合伙人注册需求文档》P-03 的短信与注册接口，用来验证
“需求 → 规则 → 用例 → pytest”链路。刻意保持：

- SQLite 持久化（`db.py` + `schema.sql`），`POST /_reset` 可逐用例重置；
- 固定验证码 `123456`，示例服务不接入真实短信通道；
- 频控按“请求次数”计数，便于快速构造 429/图形验证码分支；
- OpenAPI 由 FastAPI 生成，示例目录内另存静态 `openapi.json` 供离线分析。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import db
import downstreams

app = FastAPI(
    title="AstSight Partner Onboarding Example",
    version="1.1.0",
    description="最小示例：P-03 合伙人注册。固定验证码 123456，仅供自动化测试。",
)

CITY_ENUM = ("贵阳", "成都", "长沙", "武汉", "南京")
CURRENT_AGREEMENT_VERSION = "v1.0"
DEMO_SMS_CODE = "123456"
SMS_COOLDOWN_SECONDS = 60
SMS_DAILY_LIMIT = 10
SMS_IP_HOURLY_LIMIT = 20
SMS_CODE_TTL_SECONDS = 5 * 60
SMS_CODE_MAX_FAILED_ATTEMPTS = 5

Name = str
Mobile = str
IdCard = str
City = Literal["贵阳", "成都", "长沙", "武汉", "南京"]


# ----------------------------------------------------------------- 模型


class SendSmsRequest(BaseModel):
    mobile: str = Field(
        ...,
        pattern=r"^1[3-9]\d{9}$",
        description="11 位手机号，1 开头、第二位 3–9",
        examples=["13800138000"],
    )
    captcha_token: str | None = Field(
        default=None,
        description="图形验证码 token；第 3 次起必填，示例服务接受任意非空值",
    )
    scene: Literal["register"] = Field(default="register")


class SendSmsResponse(BaseModel):
    cooldown_seconds: int = Field(description="距离下次可发送的秒数")
    remaining_today: int = Field(description="当日剩余可尝试次数")
    captcha_required: bool = Field(description="后续是否要求图形验证码")


class RegisterRequest(BaseModel):
    name: str = Field(
        ...,
        min_length=2,
        max_length=20,
        description="2–20 个字符，允许中文、字母、空格与间隔号",
        examples=["张三"],
    )
    mobile: str = Field(
        ...,
        pattern=r"^1[3-9]\d{9}$",
        description="11 位手机号",
        examples=["13800138000"],
    )
    sms_code: str = Field(
        ...,
        pattern=r"^\d{6}$",
        description="6 位数字验证码；示例服务固定为 123456",
        examples=[DEMO_SMS_CODE],
    )
    city: City = Field(description="城市枚举五选一")
    id_card: str = Field(
        ...,
        min_length=18,
        max_length=18,
        description="18 位身份证号，必须通过 GB 11643 校验位",
        examples=["11010519491231002X"],
    )
    agreement_checked: bool = Field(description="协议必须由用户主动勾选")
    agreement_version: str = Field(
        description=f"勾选时展示的协议版本；当前为 {CURRENT_AGREEMENT_VERSION}",
        examples=[CURRENT_AGREEMENT_VERSION],
    )
    idempotency_key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="注册提交幂等键；重试必须沿用同一个值",
        examples=["reg-13800138000-001"],
    )


class RegisterResponse(BaseModel):
    partner_id: str
    status: Literal["pending"]
    audit_status: Literal["pending"]
    audit_step: Literal[1]
    submit_at: str


class ErrorResponse(BaseModel):
    error_code: str
    message: str


# ----------------------------------------------------------------- 状态

# 注册数据落在 SQLite（db.py）；频控窗口与验证码状态仍在内存，重置时一并清空。
_SMS_ATTEMPTS: dict[str, list[datetime]] = {}
_IP_ATTEMPTS: dict[str, list[datetime]] = {}
_LAST_SMS_AT: dict[str, datetime] = {}
# mobile -> {code, issued_at, failed_attempts, invalidated}
_SMS_CODES: dict[str, dict] = {}


def reset_state() -> None:
    """清空数据库与内存频控；生成用例的 conftest 会在每条用例前调用 `/_reset`。"""
    db.reset_db()
    _SMS_ATTEMPTS.clear()
    _IP_ATTEMPTS.clear()
    _LAST_SMS_AT.clear()
    _SMS_CODES.clear()


reset_state()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error_code": code, "message": message})


def _valid_name(name: str) -> bool:
    if not 2 <= len(name) <= 20:
        return False
    allowed = set(" ·")
    return all(
        char in allowed
        or "a" <= char.lower() <= "z"
        or "一" <= char <= "龥"
        for char in name
    ) and any(
        "a" <= char.lower() <= "z" or "一" <= char <= "龥" for char in name
    )


def _valid_id_card(value: str) -> bool:
    if len(value) != 18 or not value[:17].isdigit():
        return False
    if value[17].upper() not in "0123456789X":
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check_codes = "10X98765432"
    total = sum(int(char) * weight for char, weight in zip(value[:17], weights))
    return check_codes[total % 11] == value[17].upper()


def _prune(values: list[datetime], cutoff: datetime) -> list[datetime]:
    return [value for value in values if value >= cutoff]


def _city_list() -> tuple[str, ...] | None:
    """城市枚举来自 `CITY_LIST` 配置（JSON 数组）；未配置时用默认五市。

    配置为空串、非法 JSON、非数组或空数组时返回 None，注册接口据此返回 503。
    """
    raw = os.getenv("CITY_LIST")
    if raw is None:
        return CITY_ENUM
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, list):
        return None
    cities = tuple(str(item) for item in value if str(item).strip())
    return cities or None


def _verify_sms_code(mobile: str, code: str) -> None:
    """按手机号校验短信验证码：时效 5 分钟、累计 5 次失败作废。

    示例环境没有前置发送记录时仍接受固定验证码，便于直接构造正向用例；
    一旦调用过 `sms/send`，就以该手机号的签发记录为准。
    """
    record = _SMS_CODES.get(mobile)
    if record is None:
        if code == DEMO_SMS_CODE:
            return
        raise _error(400, "INVALID_SMS_CODE", "验证码错误或已过期")
    if record["invalidated"]:
        raise _error(
            400, "SMS_CODE_ATTEMPTS_EXCEEDED", "验证码错误次数已达上限，请重新获取"
        )
    if _now() - record["issued_at"] > timedelta(seconds=SMS_CODE_TTL_SECONDS):
        raise _error(400, "SMS_CODE_EXPIRED", "验证码已过期，请重新获取")
    if code == record["code"]:
        return
    record["failed_attempts"] += 1
    if record["failed_attempts"] >= SMS_CODE_MAX_FAILED_ATTEMPTS:
        record["invalidated"] = True
        raise _error(
            400, "SMS_CODE_ATTEMPTS_EXCEEDED", "验证码错误次数已达上限，请重新获取"
        )
    raise _error(400, "INVALID_SMS_CODE", "验证码错误或已过期")


def _body_matches(existing: sqlite3.Row, payload: RegisterRequest) -> bool:
    """幂等重试的请求体一致性：name/mobile/id_card(大写)/city/agreement_version。"""
    return (
        existing["name"] == payload.name
        and existing["mobile"] == payload.mobile
        and str(existing["id_card"]).upper() == payload.id_card.upper()
        and existing["city"] == payload.city
        and existing["agreement_version"] == payload.agreement_version
    )


def _resolve_existing(
    existing: sqlite3.Row, payload: RegisterRequest
) -> RegisterResponse:
    """唯一键已存在：先判幂等键，再判请求体一致性。"""
    if existing["idempotency_key"] != payload.idempotency_key:
        raise _error(409, "DUPLICATE_REGISTRATION", "该手机号与证件已提交过注册")
    if not _body_matches(existing, payload):
        raise _error(
            409, "IDEMPOTENCY_KEY_CONFLICT", "幂等键已用于其他请求，请使用新的幂等键"
        )
    return RegisterResponse(
        partner_id=existing["partner_id"],
        status="pending",
        audit_status="pending",
        audit_step=1,
        submit_at=existing["submit_at"],
    )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else ""


# ----------------------------------------------------------------- 系统


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/_reset", tags=["system"], include_in_schema=False)
def reset() -> dict[str, str]:
    reset_state()
    return {"status": "reset"}


@app.get("/_debug/state", tags=["system"], include_in_schema=False)
def debug_state() -> dict:
    with db.transaction() as connection:
        partners = db.list_partners(connection)
        audit_logs = db.list_audit_logs(connection)
    for partner in partners:
        partner["id_card"] = _mask_id_card(partner["id_card"])
        # 协议元数据只供审计/运维后台读取，调试接口不返回。
        partner.pop("agreement_ip", None)
        partner.pop("agreement_user_agent", None)
    return {
        "partners": partners,
        "audit_logs": audit_logs,
        "sms_attempts": {key: len(value) for key, value in _SMS_ATTEMPTS.items()},
    }


# ----------------------------------------------------------------- 短信


@app.post(
    "/api/partner/sms/send",
    response_model=SendSmsResponse,
    responses={
        400: {"model": ErrorResponse, "description": "手机号非法或缺少图形验证码"},
        422: {"model": ErrorResponse, "description": "请求体字段校验失败"},
        429: {"model": ErrorResponse, "description": "频控命中"},
        503: {"model": ErrorResponse, "description": "短信通道不可用"},
    },
    tags=["partner"],
)
def send_sms(payload: SendSmsRequest, request: Request) -> SendSmsResponse:
    now = _now()
    mobile = payload.mobile
    client_ip = request.client.host if request.client else "unknown"

    day_ago = now - timedelta(days=1)
    hour_ago = now - timedelta(hours=1)
    mobile_attempts = _prune(_SMS_ATTEMPTS.get(mobile, []), day_ago)
    ip_attempts = _prune(_IP_ATTEMPTS.get(client_ip, []), hour_ago)
    _SMS_ATTEMPTS[mobile] = mobile_attempts
    _IP_ATTEMPTS[client_ip] = ip_attempts

    if len(mobile_attempts) >= SMS_DAILY_LIMIT:
        raise _error(429, "SMS_DAILY_LIMIT", "该手机号今日获取次数已达上限")
    if len(ip_attempts) >= SMS_IP_HOURLY_LIMIT:
        raise _error(429, "SMS_IP_LIMIT", "当前网络获取过于频繁，请稍后再试")

    # 每次请求都计入尝试次数，因此第 3 次起要求图形验证码。
    captcha_required = len(mobile_attempts) >= 2
    mobile_attempts.append(now)
    ip_attempts.append(now)
    if captcha_required and not (payload.captcha_token or "").strip():
        raise _error(400, "CAPTCHA_REQUIRED", "请先完成图形验证码")

    last_sent = _LAST_SMS_AT.get(mobile)
    if last_sent is not None and now - last_sent < timedelta(seconds=SMS_COOLDOWN_SECONDS):
        retry_after = SMS_COOLDOWN_SECONDS - int((now - last_sent).total_seconds())
        raise _error(429, "SMS_COOLDOWN", f"请在 {max(retry_after, 1)} 秒后重试")

    # 通道失败：已计入本次频控尝试，但不启动冷却、不签发验证码、不清零错误次数。
    if not downstreams.send_sms_code(mobile, DEMO_SMS_CODE):
        raise _error(503, "SMS_CHANNEL_UNAVAILABLE", "短信发送失败，请稍后重试")

    _LAST_SMS_AT[mobile] = now
    _SMS_CODES[mobile] = {
        "code": DEMO_SMS_CODE,
        "issued_at": now,
        "failed_attempts": 0,
        "invalidated": False,
    }
    return SendSmsResponse(
        cooldown_seconds=SMS_COOLDOWN_SECONDS,
        remaining_today=max(0, SMS_DAILY_LIMIT - len(mobile_attempts)),
        captcha_required=len(mobile_attempts) >= 2,
    )


# ----------------------------------------------------------------- 注册


@app.post(
    "/api/partner/register",
    response_model=RegisterResponse,
    responses={
        400: {"model": ErrorResponse, "description": "字段校验失败或资料不可用"},
        409: {"model": ErrorResponse, "description": "重复注册或幂等键冲突"},
        422: {"model": ErrorResponse, "description": "请求体字段校验失败"},
        503: {"model": ErrorResponse, "description": "城市列表配置不可用"},
    },
    tags=["partner"],
)
def register(payload: RegisterRequest, request: Request) -> RegisterResponse:
    received_at = _now()
    cities = _city_list()
    if cities is None:
        raise _error(503, "CITY_LIST_UNAVAILABLE", "城市列表加载失败，请稍后重试")

    if not _valid_name(payload.name):
        raise _error(400, "INVALID_NAME", "姓名需为 2–20 个字符，且不能是纯数字或纯符号")
    _verify_sms_code(payload.mobile, payload.sms_code)
    if payload.city not in cities:
        raise _error(422, "VALIDATION_ERROR", "city: 城市不在允许列表内")
    if not _valid_id_card(payload.id_card):
        raise _error(400, "INVALID_ID_CARD", "身份证号格式或校验位错误")
    if not payload.agreement_checked:
        raise _error(400, "AGREEMENT_NOT_CHECKED", "请先勾选《合伙人合作协议》")
    if payload.agreement_version != CURRENT_AGREEMENT_VERSION:
        raise _error(400, "AGREEMENT_VERSION_MISMATCH", "协议版本已更新，请重新勾选")

    downstreams.verify_identity(payload.name, payload.id_card)

    with db.transaction() as connection:
        response = _register_in_transaction(
            connection,
            payload,
            checked_at=_iso(received_at),
            client_ip=_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
    downstreams.sync_partner_to_core(response.partner_id, payload.name, payload.mobile)
    return response


def _register_in_transaction(
    connection: sqlite3.Connection,
    payload: RegisterRequest,
    *,
    checked_at: str,
    client_ip: str,
    user_agent: str,
) -> RegisterResponse:
    existing = db.find_by_business_key(
        connection, payload.mobile, payload.id_card
    )
    if existing is not None:
        return _resolve_existing(existing, payload)

    if db.find_by_mobile(connection, payload.mobile) is not None:
        raise _error(409, "MOBILE_ALREADY_REGISTERED", "该手机号已注册，请查看审核进度或登录")
    if db.find_by_id_card(connection, payload.id_card) is not None:
        # 证件占用与其它失败共用通用提示，避免枚举。
        raise _error(400, "REGISTRATION_UNAVAILABLE", "该证件信息暂无法完成注册，请联系客服")

    seq = db.next_partner_sequence(connection)
    partner_id = f"P{seq:06d}"
    submit_at = checked_at
    try:
        db.insert_partner(
            connection,
            seq=seq,
            partner_id=partner_id,
            name=payload.name,
            mobile=payload.mobile,
            city=payload.city,
            id_card=payload.id_card,
            status="pending",
            audit_status="pending",
            audit_step=1,
            submit_at=submit_at,
            idempotency_key=payload.idempotency_key,
            agreement_version=payload.agreement_version,
            agreement_checked_at=checked_at,
            agreement_ip=client_ip,
            agreement_user_agent=user_agent,
        )
        db.insert_audit_log(
            connection,
            partner_id=partner_id,
            action="submit",
            from_status=None,
            to_status="pending",
            audit_step=1,
            created_at=submit_at,
        )
    except sqlite3.IntegrityError as exc:
        # 并发提交时唯一索引兜底：先按业务唯一键重查，幂等/请求体规则与前置检查一致。
        existing = db.find_by_business_key(
            connection, payload.mobile, payload.id_card
        )
        if existing is not None:
            return _resolve_existing(existing, payload)
        if "partner.mobile" in str(exc):
            raise _error(
                409, "MOBILE_ALREADY_REGISTERED", "该手机号已注册，请查看审核进度或登录"
            ) from exc
        raise _error(
            400, "REGISTRATION_UNAVAILABLE", "该证件信息暂无法完成注册，请联系客服"
        ) from exc

    return RegisterResponse(
        partner_id=partner_id,
        status="pending",
        audit_status="pending",
        audit_step=1,
        submit_at=submit_at,
    )


def _mask_id_card(value: str) -> str:
    return value[:6] + "********" + value[-4:]


# ----------------------------------------------------------------- 错误体


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "error_code" in detail:
        body = detail
    else:
        body = {"error_code": "HTTP_ERROR", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", []) if part != "body")
    message = str(first.get("msg") or "请求参数校验失败")
    return JSONResponse(
        status_code=422,
        content={
            "error_code": "VALIDATION_ERROR",
            "message": f"{field}: {message}" if field else message,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8030")))
