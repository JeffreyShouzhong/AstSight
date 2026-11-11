"""模拟下游调用：短信网关、实名认证、合伙人核心服务。

示例服务本身不依赖真实下游，这里用三个明文 HTTP 调用点演示 C3 的
「路由 → handler → 下游依赖」识别链路：

- ``send_sms`` → 短信网关 ``POST /v1/messages``；
- ``register`` → 实名认证 ``POST /v1/verify``；
- ``register`` → 合伙人核心服务 ``POST /internal/partners``。

地址默认是占位域名，调用失败返回 ``False``；需要联调真实下游或 Mock 时，
用环境变量覆盖：

- ``SMS_GATEWAY_URL``：短信网关地址；未配置时视为示例模拟通道，直接返回成功。
  配置后真实调用，失败由 ``send_sms`` 返回 503 ``SMS_CHANNEL_UNAVAILABLE``。
- ``ID_VERIFY_URL`` / ``PARTNER_CORE_URL``：实名认证与核心服务地址；失败不影响
  注册主流程。
"""
from __future__ import annotations

import json
import os
import urllib.request

SMS_GATEWAY_URL = os.getenv("SMS_GATEWAY_URL", "").rstrip("/")
# 实名认证与核心服务在需求/设计中没有定义响应语义，示例默认不调用；
# 显式配置地址时才发起请求，失败返回 False 但不阻断注册主流程。
ID_VERIFY_URL = os.getenv("ID_VERIFY_URL", "").rstrip("/")
PARTNER_CORE_URL = os.getenv("PARTNER_CORE_URL", "").rstrip("/")
_TIMEOUT_SECONDS = 5.0
_JSON_HEADERS = {"Content-Type": "application/json"}


def send_sms_code(mobile: str, code: str) -> bool:
    """把验证码提交给短信网关；未配置地址时按示例模拟通道处理。"""
    if not SMS_GATEWAY_URL:
        return True
    body = json.dumps(
        {"mobile": mobile, "code": code, "scene": "register"},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{SMS_GATEWAY_URL}/v1/messages",
                data=body,
                headers=_JSON_HEADERS,
                method="POST",
            ),
            timeout=_TIMEOUT_SECONDS,
        ) as response:
            response.read()
        return True
    except OSError:
        return False


def verify_identity(name: str, id_card: str) -> bool:
    """调用实名认证服务校验姓名与身份证；未配置地址时跳过。"""
    if not ID_VERIFY_URL:
        return True
    body = json.dumps(
        {"name": name, "id_card": id_card},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{ID_VERIFY_URL}/v1/verify",
                data=body,
                headers=_JSON_HEADERS,
                method="POST",
            ),
            timeout=_TIMEOUT_SECONDS,
        ) as response:
            response.read()
        return True
    except OSError:
        return False


def sync_partner_to_core(partner_id: str, name: str, mobile: str) -> bool:
    """把新合伙人档案同步到核心服务；未配置地址时跳过。"""
    if not PARTNER_CORE_URL:
        return True
    body = json.dumps(
        {"partner_id": partner_id, "name": name, "mobile": mobile},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{PARTNER_CORE_URL}/internal/partners",
                data=body,
                headers=_JSON_HEADERS,
                method="POST",
            ),
            timeout=_TIMEOUT_SECONDS,
        ) as response:
            response.read()
        return True
    except OSError:
        return False
