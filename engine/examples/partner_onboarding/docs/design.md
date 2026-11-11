# 合伙人注册服务 · 设计文档

| 项 | 内容 |
|---|---|
| 文档名称 | 合伙人注册服务设计文档 |
| 版本 | V1.1 |
| 关联需求 | 《合伙人注册需求文档》V1.0（P-03 合伙人注册） |
| 文档状态 | 示例工程设计 |
| 范围 | 仅 P-03 的短信验证码与注册提交后端接口 |

修订记录：

| 版本 | 日期 | 修订人 | 说明 |
|---|---|---|---|
| V1.1 | 2026-11-10 | — | 修复规则冲突：城市列表配置下发、验证码时效与错误次数、协议元数据入库、删除不可达 INVALID_CITY 分支 |
| V1.0 | 2026-11-04 | — | 首版：P-03 短信验证码与注册提交的接口、数据库与流程设计 |

## 一、概述

### 1.1 背景

合伙人注册原先依赖线下填表和人工录入，存在资料重复登记、身份证错录、审核进度
不透明等问题。本期将 P-03 注册流程线上化：用户分两步填写资料、获取短信验证码、
勾选合作协议后提交，服务端完成校验、幂等去重与入库，并生成待审核档案。

### 1.2 建设目标

- 提供 P-03 所需的后端接口：短信验证码发送、注册提交；
- 保证重复提交、网络重试不会产生重复档案；
- 提交成功后写入合伙人主档与审核日志，初始状态为待审核；
- 对身份证占用等敏感失败统一提示，避免被用于枚举；
- 提供可重复执行的测试支持接口，便于自动化验证。

### 1.3 不在本期范围

- P-04 审核状态查询与 P-05 首页工作台；
- 真实短信通道、图形验证码服务对接；
- 登录、鉴权、用户会话；
- 运营后台、审核流转、结算与分佣；
- 生产级数据库迁移、连接池、多实例部署。

## 二、总体设计

### 2.1 系统上下文

```
Web 前端（两步表单）
        │ HTTPS / JSON
        ▼
合伙人注册服务（FastAPI）
        │
        ▼
SQLite 数据库
```

本期只交付服务端。两步表单的步骤控制、字段即时提示、按钮置灰与倒计时由前端
实现；服务端负责最终校验、幂等与持久化，不维护「当前处于第几步」的会话状态。

具体分工：

- 第 1 步未通过不得进入第 2 步、中途退出不保留第 1 步数据，均由前端控制；
- 服务端只接收完整注册请求（含第 1、2 步全部字段），无法判断请求来自第几步；
- 直接调用注册接口的客户端必须自行保证字段完整，服务端按 3.4 的校验顺序全量校验。

### 2.2 技术选型

| 层次 | 选型 | 说明 |
|---|---|---|
| Web 框架 | FastAPI | 自动生成 OpenAPI，请求/响应模型即契约 |
| 参数校验 | Pydantic v2 | 长度、正则、枚举等声明式校验 |
| 数据库 | SQLite | 示例工程零外部依赖，SQL 与事务语义与主流关系库一致 |
| 运行容器 | uvicorn | 单进程启动，端口由 `PORT` 配置 |

### 2.3 模块划分

| 模块 | 文件 | 职责 |
|---|---|---|
| 接口层 | `app.py` | 路由、请求/响应模型、错误响应、频控 |
| 持久化层 | `db.py` | 连接、事务、查询、写入 |
| 数据定义 | `schema.sql` | 表、索引、外键 |
| 契约快照 | `openapi.json` | 供前端联调与离线查看 |

分层约束：

- 接口层不拼 SQL，不直接操作数据文件；
- 持久化层不返回 HTTP 状态码，不感知业务错误文案；
- 唯一索引冲突由接口层翻译为业务错误，数据库异常不外泄。

## 三、接口设计

### 3.1 通用约定

| 项 | 约定 |
|---|---|
| Base URL | `http://{host}:{port}` |
| 请求类型 | `application/json; charset=utf-8` |
| 成功响应 | HTTP 200，JSON 对象 |
| 时间格式 | ISO 8601 UTC，秒级，`Z` 结尾，如 `2026-11-04T12:30:04Z` |
| 错误响应 | `{"error_code": "...", "message": "..."}` |
| 字符编码 | UTF-8 |

接口清单：

| 方法 | 路径 | 说明 | 来源 |
|---|---|---|---|
| GET | `/health` | 健康检查 | 运维 |
| POST | `/api/partner/sms/send` | 发送短信验证码 | P-03 第 1 步 |
| POST | `/api/partner/register` | 提交注册 | P-03 第 2 步 |
| POST | `/_reset` | 清空测试数据 | 测试支持 |
| GET | `/_debug/state` | 查看测试数据 | 测试支持 |

### 3.2 健康检查

`GET /health`

用于存活探测，无请求参数。

响应示例：

```json
{"status": "ok"}
```

### 3.3 发送短信验证码

`POST /api/partner/sms/send`

#### 请求参数

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `mobile` | string | 是 | `^1[3-9]\d{9}$` | 11 位手机号 |
| `scene` | string | 否 | 固定 `register` | 使用场景，默认 `register` |
| `captcha_token` | string | 条件 | 非空 | 第 3 次获取起必填 |

#### 频控规则

| 规则 | 阈值 | 错误码 |
|---|---|---|
| 同号重发冷却 | 60 秒 | `SMS_COOLDOWN` |
| 单号每日上限 | 10 次 | `SMS_DAILY_LIMIT` |
| 单 IP 每小时上限 | 20 次 | `SMS_IP_LIMIT` |
| 图形验证码 | 同号第 3 次起 | `CAPTCHA_REQUIRED` |

每次请求都计入尝试次数，因此「第 3 次起要求图形验证码」按请求次数而非成功次数
计算。

窗口口径：单号每日上限按最近 24 小时滚动窗口计算，单 IP 每小时上限按最近 60 分钟
滚动窗口计算，均以 UTC 计时，窗口为左闭右开。若业务改为自然日/自然小时，需另行
确认时区与边界（见 7.3）。

单 IP 限流在 NAT/共享出口下可能误伤同网用户；本期不提供白名单或设备级限流，
是否调整由业务确认（见 7.3 Q-01）。

#### 验证码签发与时效

- `sms/send` 成功后为该手机号签发验证码，有效期 5 分钟；
- 同一验证码最多允许 5 次校验失败，第 5 次失败后立即作废，必须重新获取；
- 重新获取验证码会重置有效期与错误次数，验证码按手机号维度记录；
- 示例环境验证码固定为 `123456`，但仍执行上述时效与次数规则。

校验失败的错误码见 3.4「错误」。

#### 图形验证码契约

- `captcha_token` 由外部图形验证码服务签发，本服务不解析 token 内容，仅透传校验；
- 校验地址由 `CAPTCHA_VERIFY_URL` 配置；token 缺失返回 400 `CAPTCHA_REQUIRED`，
  校验不通过返回 400 `CAPTCHA_INVALID`；
- token 有效期、单 token 失败次数、锁定与重放策略由图形验证码服务负责，本服务
  按校验结果处理；
- 示例环境未接入图形验证码服务，接受任意非空 token；生产接入前需确认签发方、
  有效期、失败上限与错误码（见 7.3 Q-02）。

#### 短信通道失败

- 调用短信通道超时 5 秒；超时或失败返回 503 `SMS_CHANNEL_UNAVAILABLE`，提示
  「短信发送失败，请稍后重试」；
- 失败请求计入单号/单 IP 频控尝试次数，但不启动 60 秒冷却，允许用户立即重试；
- 发送失败时不记录验证码签发时间，也不清零该手机号已有的错误次数。

#### 响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `cooldown_seconds` | integer | 下次可发送的间隔秒数，固定 60 |
| `remaining_today` | integer | `max(0, 10 - 最近 24 小时已计入请求数)`，计数含本次请求 |
| `captcha_required` | boolean | `最近 24 小时已计入请求数（含本次）>= 2`，即为后续（第 3 次起）请求要求图形验证码 |

响应示例：

```json
{"cooldown_seconds": 60, "remaining_today": 9, "captcha_required": false}
```

#### 错误

| HTTP | error_code | 场景 |
|---|---|---|
| 400 | `CAPTCHA_REQUIRED` | 已触发图形验证码但未提供 token |
| 400 | `CAPTCHA_INVALID` | 图形验证码 token 校验不通过 |
| 422 | `VALIDATION_ERROR` | 手机号格式错误或缺失 |
| 429 | `SMS_COOLDOWN` | 60 秒内重复发送 |
| 429 | `SMS_DAILY_LIMIT` | 单号最近 24 小时超过 10 次 |
| 429 | `SMS_IP_LIMIT` | 单 IP 最近 60 分钟超过 20 次 |
| 503 | `SMS_CHANNEL_UNAVAILABLE` | 短信通道超时或发送失败 |

### 3.4 提交注册

`POST /api/partner/register`

#### 请求参数

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `name` | string | 是 | 2–20 字符 | 中文、字母、空格、`·`；不能纯数字/符号 |
| `mobile` | string | 是 | `^1[3-9]\d{9}$` | 手机号 |
| `sms_code` | string | 是 | `^\d{6}$`，5 分钟有效 | 短信验证码；错误累计 5 次作废 |
| `city` | string | 是 | 五市枚举，来源 `CITY_LIST` 配置 | 贵阳/成都/长沙/武汉/南京；必须显式选择，无默认值 |
| `id_card` | string | 是 | 18 位 | GB 11643 校验位 |
| `agreement_checked` | boolean | 是 | 必须为 `true` | 协议已勾选 |
| `agreement_version` | string | 是 | 当前 `v1.0` | 勾选时展示的协议版本 |
| `idempotency_key` | string | 是 | 1–64 字符 | 幂等键，重试必须沿用 |

#### 字段规范化与姓名规则

- `name` 长度按 Unicode 码点计数（Python `len`），首尾空白不做 trim，空格计入长度；
- 允许字符：中文（U+4E00–U+9FA5）、ASCII 字母、半角空格（U+0020）、间隔号 `·`（U+00B7）；
- 不允许全角字母/数字、全角空格（U+3000）及其他符号；
- 姓名必须至少包含一个中文或 ASCII 字母，纯空格、纯符号、纯数字均不通过；
- `id_card` 与业务唯一键中的身份证统一转大写后再比较与入库。

#### 城市列表配置

- 城市枚举由配置项 `CITY_LIST` 下发，默认五市：贵阳/成都/长沙/武汉/南京；
- 服务启动时加载配置；配置为空或加载失败时，`POST /api/partner/register` 返回
  `503 CITY_LIST_UNAVAILABLE`，不执行任何校验与写库；
- 前端在列表为空时提示「城市列表加载失败」并禁用提交，不允许以空枚举兜底。

#### 响应字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `partner_id` | string | 合伙人编号，格式 `P` + 6 位序号 |
| `status` | string | 固定 `pending` |
| `audit_status` | string | 固定 `pending` |
| `audit_step` | integer | 固定 `1`（提交资料） |
| `submit_at` | string | 提交时间，UTC |

响应示例：

```json
{
  "partner_id": "P000001",
  "status": "pending",
  "audit_status": "pending",
  "audit_step": 1,
  "submit_at": "2026-11-04T12:30:04Z"
}
```

#### 校验顺序

校验顺序影响返回哪个错误，按以下顺序执行：

0. 城市列表配置可用性（为空或加载失败 → 503，且不继续校验、不写库）；
1. 姓名规则；
2. 短信验证码（格式、5 分钟时效、累计 5 次错误作废）；
3. 城市枚举；
4. 身份证格式与校验位；
5. 协议勾选；
6. 协议版本；
7. 业务唯一键幂等判断；
8. 手机号是否已注册；
9. 身份证是否被占用；
10. 写库（同时记录协议勾选时间、IP、UA）。

#### 幂等与重复注册

业务唯一键为「手机号 + 身份证（大写）」。

| 条件 | 处理 |
|---|---|
| 唯一键已存在且 `idempotency_key` 相同、请求体一致 | 返回原档案，HTTP 200，`partner_id` 不变 |
| 唯一键已存在且 `idempotency_key` 相同、请求体不同 | 409 `IDEMPOTENCY_KEY_CONFLICT` |
| 唯一键已存在但 `idempotency_key` 不同 | 409 `DUPLICATE_REGISTRATION` |
| 手机号已存在、唯一键不同 | 409 `MOBILE_ALREADY_REGISTERED` |
| 身份证已存在、唯一键不同 | 400 `REGISTRATION_UNAVAILABLE`（通用提示） |

请求体一致性按 `name`、`mobile`、`id_card`（大写）、`city`、`agreement_version`
比较；任一字段变化视为不同请求。该规则为默认设计，业务若希望改为「返回首次结果」
需在 7.3 Q-03 确认。

身份证占用不使用「该身份证已注册」这类可枚举文案。

`409 MOBILE_ALREADY_REGISTERED` 只表示手机号已注册；前端根据 P-04 查询到的
`audit_status` 决定跳转 P-04 还是登录页，本服务不返回跳转地址，也不实现 P-04
状态查询。

#### 协议元数据

提交注册时除校验协议版本外，还需在同一事务内记录：

| 字段 | 来源 | 说明 |
|---|---|---|
| `agreement_version` | 请求体 | 用户勾选时展示的协议版本 |
| `agreement_checked_at` | 服务端接收时间 | UTC，精确到秒 |
| `agreement_ip` | 请求来源 IP | 优先取 `X-Forwarded-For` 首个地址，否则取连接地址 |
| `agreement_user_agent` | `User-Agent` 请求头 | 缺失时记空字符串 |

IP 与 UA 仅用于协议签署留痕，不写入业务日志正文。

协议版本更新后，已提交且仍为 `pending` 的档案保留提交时的 `agreement_version`，
不追溯重签；只有新提交必须使用当前版本。

#### 错误

| HTTP | error_code | 场景 |
|---|---|---|
| 400 | `INVALID_NAME` | 姓名不符合业务规则 |
| 400 | `INVALID_SMS_CODE` | 验证码错误，且未达到 5 次 |
| 400 | `SMS_CODE_EXPIRED` | 验证码超过 5 分钟有效期 |
| 400 | `SMS_CODE_ATTEMPTS_EXCEEDED` | 错误累计达到 5 次，验证码已作废 |
| 400 | `INVALID_ID_CARD` | 身份证格式或校验位错误 |
| 400 | `AGREEMENT_NOT_CHECKED` | 协议未勾选 |
| 400 | `AGREEMENT_VERSION_MISMATCH` | 协议版本不一致 |
| 409 | `DUPLICATE_REGISTRATION` | 同唯一键不同幂等键 |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | 同唯一键同幂等键，但请求体不一致 |
| 409 | `MOBILE_ALREADY_REGISTERED` | 手机号已注册 |
| 400 | `REGISTRATION_UNAVAILABLE` | 身份证占用，通用提示 |
| 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |
| 503 | `CITY_LIST_UNAVAILABLE` | 城市列表配置为空或加载失败，禁止提交 |

### 3.5 测试支持接口

以下接口不对外发布，不写入 OpenAPI：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/_reset` | 清空合伙人、审核日志、序号与频控计数 |
| GET | `/_debug/state` | 返回主档、审核日志与频控计数；身份证掩码输出 |

### 3.6 OpenAPI 快照

`openapi.json` 由 FastAPI 生成，作为前端联调与契约评审的静态快照。修改接口后
必须重新生成：

```bash
python - <<'PY'
import json
from app import app
open("openapi.json", "w", encoding="utf-8").write(
    json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"
)
PY
```

## 四、数据库设计

### 4.1 实体关系

```
partner_seq (1) ── 生成序号 ──> partner (1) ──< partner_audit_log (N)
```

- `partner`：合伙人主档，一次成功注册一行；
- `partner_audit_log`：审核日志，一次提交动作一行，通过 `partner_id` 关联主档；
- `partner_seq`：单行序号表，用于生成可读的 `partner_id`，重置时归零。

### 4.2 partner（合伙人主档）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `seq` | INTEGER | PK | 内部序号，与 `partner_id` 数字部分一致 |
| `partner_id` | TEXT | NOT NULL, UNIQUE | 对外编号，`P%06d` |
| `name` | TEXT | NOT NULL | 姓名 |
| `mobile` | TEXT | NOT NULL, UNIQUE | 手机号 |
| `city` | TEXT | NOT NULL | 城市 |
| `id_card` | TEXT | NOT NULL | 身份证，统一存大写 |
| `status` | TEXT | NOT NULL | 合伙人状态，初始 `pending` |
| `audit_status` | TEXT | NOT NULL | 审核状态，初始 `pending` |
| `audit_step` | INTEGER | NOT NULL, DEFAULT 1 | 审核步骤，初始 1 |
| `submit_at` | TEXT | NOT NULL | 提交时间，UTC |
| `idempotency_key` | TEXT | NOT NULL | 提交幂等键 |
| `agreement_version` | TEXT | NOT NULL | 勾选时的协议版本 |
| `agreement_checked_at` | TEXT | NOT NULL | 协议勾选确认时间，UTC |
| `agreement_ip` | TEXT | NOT NULL | 提交时来源 IP，缺失记空字符串 |
| `agreement_user_agent` | TEXT | NOT NULL | 提交时 User-Agent，缺失记空字符串 |
| `created_at` | TEXT | NOT NULL | 入库时间，UTC |

索引：`idx_partner_id_card(id_card)`，用于身份证占用检查。

### 4.3 partner_audit_log（审核日志）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | 日志序号 |
| `partner_id` | TEXT | NOT NULL, FK → `partner.partner_id` | 关联合伙人 |
| `action` | TEXT | NOT NULL | 动作，提交为 `submit` |
| `from_status` | TEXT | NULL | 变更前状态，提交时为 NULL |
| `to_status` | TEXT | NOT NULL | 变更后状态，`pending` |
| `audit_step` | INTEGER | NOT NULL | 审核步骤，提交为 1 |
| `created_at` | TEXT | NOT NULL | 发生时间，UTC |

索引：`idx_audit_partner(partner_id)`。

### 4.4 partner_seq（序号表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `name` | TEXT | PK | 序列名，当前只有 `partner` |
| `value` | INTEGER | NOT NULL | 当前序号 |

### 4.5 事务与并发

注册写入在一个事务内完成：

```
BEGIN
  UPDATE partner_seq SET value = value + 1
  SELECT value
  INSERT INTO partner (...)
  INSERT INTO partner_audit_log (...)
COMMIT
```

- 主档与日志同事务提交，任一失败整体回滚；
- `mobile`、`partner_id` 唯一索引兜底并发提交；
- 捕获唯一约束冲突后重新按业务唯一键查询：幂等键与请求体均一致时返回已存在档案
  （200），请求体不一致返回 409 `IDEMPOTENCY_KEY_CONFLICT`，其余按手机号/身份证
  冲突返回 409 `MOBILE_ALREADY_REGISTERED` 或 400 `REGISTRATION_UNAVAILABLE`；
- 当前为单进程 + SQLite 单写者模型，不支持多实例并发写入；多实例部署前必须改为
  数据库唯一约束 + 重试/冲突查询模型。

### 4.6 数据安全

| 项 | 处理 |
|---|---|
| 身份证展示 | 接口不返回身份证；调试接口掩码为前 6 位 + `********` + 后 4 位 |
| 身份证存储 | 示例库存大写原文，仅用于唯一性校验；生产必须加密或散列，不得明文长期保存 |
| 协议元数据 | `agreement_ip` / `agreement_user_agent` 随主档保存，保留期限与主档一致；`/_debug/state` 不返回这两个字段；仅审计/运维后台可读；仅在受信代理后信任 `X-Forwarded-For` |
| 日志 | 不打印请求体，避免身份证明文进入日志 |
| 数据清理 | `/_reset` 清空全部注册数据，仅用于测试 |

## 五、业务流程设计

### 5.1 短信验证码流程

```
接收请求
  ├─ 参数校验（手机号格式）──失败──> 422
  ├─ 清理过期计数窗口
  ├─ 每日上限检查 ───────────命中──> 429 SMS_DAILY_LIMIT
  ├─ 单 IP 上限检查 ─────────命中──> 429 SMS_IP_LIMIT
  ├─ 第 3 次起要求图形验证码 ─缺失──> 400 CAPTCHA_REQUIRED
  ├─ 60 秒冷却检查 ──────────命中──> 429 SMS_COOLDOWN
  ├─ 记录发送时间与计数
  ├─ 为该手机号签发验证码（示例固定 123456，有效期 5 分钟，错误计数清零）
  └─ 返回冷却时间、剩余次数、是否需图形验证码
```

示例环境不接入真实短信通道，验证码固定为 `123456`，但签发时间与错误次数仍按
3.3「验证码签发与时效」执行：签发后 5 分钟有效，累计 5 次校验失败即作废，
重新获取后重置。

### 5.2 注册提交流程

```
接收请求
  ├─ 城市列表配置检查 ────为空/加载失败──> 503 CITY_LIST_UNAVAILABLE
  ├─ 字段校验（姓名/短信验证码时效与错误次数/城市/身份证/协议）
  ├─ 查询业务唯一键（mobile + id_card）
  │    ├─ 已存在且幂等键相同 ──> 200，返回原 partner_id
  │    └─ 已存在且幂等键不同 ──> 409 DUPLICATE_REGISTRATION
  ├─ 手机号已存在 ────────────> 409 MOBILE_ALREADY_REGISTERED
  ├─ 身份证已存在 ────────────> 400 REGISTRATION_UNAVAILABLE
  ├─ 事务：取序号 → 写主档（协议版本/勾选时间/IP/UA）→ 写审核日志
  └─ 200，返回 partner_id 与待审核状态
```

### 5.3 状态设计

| 字段 | 初始值 | 说明 |
|---|---|---|
| `status` | `pending` | 合伙人状态 |
| `audit_status` | `pending` | 审核状态 |
| `audit_step` | `1` | 提交资料；后续 2/3/4 由 P-04 实现 |

P-03 只产生初始状态，不负责状态流转。`audit_step` 2/3/4 的推进、审核通过/驳回
后的字段变化与通知，均由 P-04 定义和实现，不在本服务范围；本服务不提供审核状态
查询接口。

## 六、非功能设计

| 项 | 设计 |
|---|---|
| 可用性 | `/health` 供存活探测；单进程启动 |
| 性能 | 示例基线（4C8G、本地 SQLite、单进程）：注册接口 p95 ≤ 200 ms，短信发送接口 p95 ≤ 100 ms（不含外部通道耗时）；以 10 并发、60 秒窗口统计；生产 SLA 另行确定 |
| 一致性 | 主档与日志同事务；唯一索引防重 |
| 安全 | 身份证输出掩码、占用提示统一、频控防刷、无明文日志 |
| 可观测性 | uvicorn 访问日志；业务错误返回稳定 `error_code` |
| 时区 | 入库时间统一 UTC，展示时由前端转换为本地时间 |
| 配置 | `PORT` 指定端口，`PARTNER_DB` 指定数据库文件，`CITY_LIST` 指定城市列表 |

## 七、约束与实现差异

### 7.1 已知约束

- 服务无鉴权，注册接口按公开接口设计；生产部署必须在网关层增加鉴权、限流与
  审计访问控制；
- SQLite 单文件库，不适合多实例共享写入；
- `CREATE TABLE IF NOT EXISTS` 不处理表结构升级，生产需引入迁移工具；
- 频控计数、验证码签发时间与错误次数在内存中，进程重启后清零/失效；
- 短信通道与图形验证码为外部依赖；示例环境用固定验证码并接受任意非空 token，
  生产接入前需确认契约（见 7.3 Q-02）；
- 调试与重置接口未做环境隔离，生产部署必须关闭。

### 7.2 与需求文档的差异

| 需求点 | 当前实现 | 原因 |
|---|---|---|
| 已注册用户按审核状态重定向 | 不涉及 | 属前端与 P-04 范围 |
| 两步表单服务端状态 | 不保存 | 步骤控制由前端负责 |

### 7.3 待业务确认

以下事项涉及业务策略或外部服务事实，本文档先给出默认实现；确认后需回写本文档
与需求文档，不能只改代码。规则库中的 Q-04（验证码时效）、Q-05（城市列表来源）、
Q-06（协议元数据）已通过 V1.1 修订关闭。

| 编号 | 事项 | 当前默认 | 需确认 |
|---|---|---|---|
| Q-01 | 单 IP 限流对 NAT/共享出口的误伤 | 维持 20 次/小时，无白名单、无设备级限流 | 是否调整阈值、增加白名单或按设备限流 |
| Q-02 | 图形验证码服务契约 | token 由外部服务签发，本服务透传校验；示例接受任意非空 token | 签发方、有效期、失败上限、错误码与重放策略 |
| Q-03 | 同一幂等键携带不同请求体 | 返回 409 `IDEMPOTENCY_KEY_CONFLICT` | 是否改为返回首次结果或覆盖 |
| D-01 | 频控窗口口径 | 最近 24 小时 / 最近 60 分钟滚动窗口，UTC | 是否改为自然日/自然小时，以及采用的时区 |
| D-02 | 生产性能 SLA | 示例基线 p95 200 ms / 100 ms，10 并发 | 生产响应时间、并发量与压测环境 |

## 八、部署与运行

```bash
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8030
```

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8030` | 直接运行 `python app.py` 时的端口 |
| `PARTNER_DB` | `./partner.db` | SQLite 文件路径 |
| `CITY_LIST` | 五市枚举 | 城市列表 JSON 数组；为空时注册接口返回 503 |
| `CAPTCHA_VERIFY_URL` | 无 | 图形验证码校验服务地址；未配置时示例接受任意非空 token |

部署检查：

1. `/health` 返回 200；
2. `openapi.json` 与当前代码一致；
3. 生产环境关闭 `/_reset` 与 `/_debug/state`；
4. 数据库文件目录可写，并纳入备份与权限控制。
