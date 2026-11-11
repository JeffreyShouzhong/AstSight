# 接口自动化用例（按接口 · 文本版，待审）

- 项目：`partner_onboarding`
- context_id：`9bc8d1ccd52f00f89015d331ac6a6e9fd772791808346ad717d2a3a11f324a5c`
- 规则库：`rs-1be1cd3c8400d052` v3 `0ff935397c9ae89b…`
- 用例数：38
- 来源：`cases.draft.json`
- 状态：**已批准**（2026-11-11，reviewer `JeffreyShouzhong2`）。本文档保留为设计记录；带 TC 编号的正式文本版见 `cases.approved.md`。

AstSight 的用例对象以 `endpoint` 为执行归属，生成脚本时按接口分组，每个接口一个 `test_<method>_<path>.py`。本文档按接口重新组织同一批用例，便于接口自动化评审；规则驱动只决定“为什么测”，接口决定“在哪里执行”。

## 总览

| 接口 | 用例数 | positive | negative | boundary | business | 多步骤 | 前置状态 |
|---|---|---|---|---|---|---|---|
| `GET /health` | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `POST /api/partner/sms/send` | 9 | 1 | 4 | 0 | 4 | 4 | 0 |
| `POST /api/partner/register` | 29 | 2 | 22 | 4 | 1 | 3 | 4 |

索引：

| 接口 | case_key | 标题 | 类别 | 优先级 | 步骤 |
|---|---|---|---|---|---|
| `POST /api/partner/sms/send` | `sms_send_success_cooldown` | 发送短信验证码成功返回固定 60 秒冷却 | `positive` | `P0` | 0 |
| `POST /api/partner/sms/send` | `sms_send_missing_mobile` | 缺少 mobile 时返回 422 VALIDATION_ERROR | `negative` | `P0` | 0 |
| `POST /api/partner/sms/send` | `sms_send_mobile_second_digit_invalid` | 手机号第二位不在 3–9 时返回 422 | `negative` | `P1` | 0 |
| `POST /api/partner/sms/send` | `sms_send_scene_not_register` | scene 不是固定值 register 时返回 422 | `negative` | `P2` | 0 |
| `POST /api/partner/sms/send` | `sms_send_same_mobile_within_cooldown` | 同号 60 秒内重复发送返回 429 SMS_COOLDOWN | `business` | `P0` | 2 |
| `POST /api/partner/sms/send` | `sms_captcha_required_from_third_request` | 同号第 3 次请求起要求图形验证码 | `business` | `P0` | 3 |
| `POST /api/partner/sms/send` | `sms_daily_limit_after_ten_requests` | 同号当日第 11 次请求返回 429 SMS_DAILY_LIMIT | `business` | `P0` | 11 |
| `POST /api/partner/sms/send` | `sms_ip_hourly_limit_after_twenty_requests` | 单 IP 每小时第 21 次请求返回 429 SMS_IP_LIMIT | `business` | `P1` | 21 |
| `POST /api/partner/sms/send` | `sms_channel_unavailable` | 短信通道超时或失败返回 503 SMS_CHANNEL_UNAVAILABLE | `negative` | `P0` | 0 |
| `POST /api/partner/register` | `register_success_pending` | 资料合法时注册成功并返回待审核初始状态 | `positive` | `P0` | 0 |
| `POST /api/partner/register` | `register_name_empty` | 姓名为空时返回 422 校验错误 | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_name_too_long` | 姓名超过 20 字符时返回 422 校验错误 | `boundary` | `P1` | 0 |
| `POST /api/partner/register` | `register_name_pure_digits` | 姓名为纯数字时返回 400 INVALID_NAME | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_name_pure_symbols` | 姓名为纯符号时返回 400 INVALID_NAME | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_mobile_second_digit_invalid` | 注册手机号第二位不在 3–9 时返回 422 | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_sms_code_bad_format` | 验证码不是 6 位数字时返回 422 | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_sms_code_wrong_value` | 验证码取值错误时返回 400 INVALID_SMS_CODE | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_city_not_in_enum` | 城市不在五市枚举内时返回 422 | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_city_missing` | 未显式选择城市时返回 422 | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_id_card_wrong_length` | 身份证不足 18 位时返回 422 | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_id_card_bad_checksum` | 身份证校验位错误时返回 400 INVALID_ID_CARD | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_agreement_not_checked` | 协议未勾选时返回 400 AGREEMENT_NOT_CHECKED | `negative` | `P0` | 0 |
| `POST /api/partner/register` | `register_agreement_checked_missing` | 缺少 agreement_checked 时返回 422 | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_agreement_version_mismatch` | 协议版本不一致时返回 400 AGREEMENT_VERSION_MISMATCH | `negative` | `P0` | 0 |
| `POST /api/partner/register` | `register_idempotency_key_too_long` | 幂等键超过 64 字符时返回 422 | `boundary` | `P1` | 0 |
| `POST /api/partner/register` | `register_idempotency_key_empty` | 幂等键为空时返回 422 | `boundary` | `P2` | 0 |
| `POST /api/partner/register` | `register_validation_order_name_before_sms` | 姓名规则先于验证码校验 | `negative` | `P2` | 0 |
| `POST /api/partner/register` | `register_validation_order_sms_before_agreement` | 验证码校验先于协议勾选校验 | `negative` | `P2` | 0 |
| `POST /api/partner/register` | `register_idempotent_retry_same_key` | 唯一键与幂等键都相同，重试返回原档案 | `positive` | `P0` | 0 |
| `POST /api/partner/register` | `register_duplicate_unique_key_different_idempotency` | 唯一键相同但幂等键不同返回 409 DUPLICATE_REGISTRATION | `negative` | `P0` | 0 |
| `POST /api/partner/register` | `register_mobile_already_registered` | 手机号已注册且唯一键不同返回 409 MOBILE_ALREADY_REGISTERED | `negative` | `P0` | 0 |
| `POST /api/partner/register` | `register_id_card_occupied_generic_message` | 身份证被占用返回 400 REGISTRATION_UNAVAILABLE 且不暴露可枚举文案 | `negative` | `P0` | 0 |
| `POST /api/partner/register` | `sms_code_attempts_exceeded` | 同一验证码累计 5 次错误后作废，正确验证码也返回 SMS_CODE_ATTEMPTS_EXCEEDED | `negative` | `P0` | 7 |
| `POST /api/partner/register` | `register_name_outer_spaces_allowed` | 姓名首尾空白不 trim 且空格计入长度，合法姓名注册成功 | `boundary` | `P2` | 0 |
| `POST /api/partner/register` | `register_name_fullwidth_rejected` | 姓名包含全角字母时返回 400 INVALID_NAME | `negative` | `P1` | 0 |
| `POST /api/partner/register` | `register_name_spaces_only` | 姓名只有空格时返回 400 INVALID_NAME | `negative` | `P2` | 0 |
| `POST /api/partner/register` | `register_id_card_lowercase_normalized` | 身份证末位小写 x 转大写后与同一业务唯一键匹配 | `business` | `P1` | 2 |
| `POST /api/partner/register` | `register_idempotency_key_conflict` | 同唯一键同幂等键但请求体不一致返回 409 IDEMPOTENCY_KEY_CONFLICT | `negative` | `P0` | 2 |

## `GET /health`

| 项 | 内容 |
|---|---|
| 请求参数 | 无 |
| 成功响应 | `200`，`{"status": "ok"}` |
| 错误响应 | 未声明 |
| 下游依赖 | 无 |
| 用例数 | 0 |

> 缺口：`GET /health` 在画像中存在，但没有业务规则引用，因此 A4 规则驱动用例集未生成健康检查用例。若接口自动化需要冒烟覆盖，应在用例集之外增加健康检查 smoke，而不是编造业务规则。

## `POST /api/partner/sms/send`

| 项 | 内容 |
|---|---|
| 请求字段 | `mobile` 必填，`^1[3-9]\d{9}$`；`captcha_token` 可选；`scene` 固定 `register` |
| 成功响应 | `200`，返回 `cooldown_seconds`、`remaining_today`、`captcha_required` |
| 错误响应 | `400` `CAPTCHA_REQUIRED`；`422` `VALIDATION_ERROR`；`429` `SMS_COOLDOWN` / `SMS_DAILY_LIMIT` / `SMS_IP_LIMIT`；`503` `SMS_CHANNEL_UNAVAILABLE`（设计声明，画像未列） |
| 下游依赖 | `http:host:sms-gateway.internal POST /v1/messages` |
| 用例数 | 9 |

### 用例清单（9 条）

| # | case_key | 标题 | 类别 | 优先级 | 规则 |
|---|---|---|---|---|---|
| 1 | `sms_send_success_cooldown` | 发送短信验证码成功返回固定 60 秒冷却 | `positive` | `P0` | BR-33、BR-03、CR-09、BR-45、BR-46 |
| 2 | `sms_send_missing_mobile` | 缺少 mobile 时返回 422 VALIDATION_ERROR | `negative` | `P0` | BR-30、CR-09、BR-28 |
| 3 | `sms_send_mobile_second_digit_invalid` | 手机号第二位不在 3–9 时返回 422 | `negative` | `P1` | BR-03、BR-30、CR-09 |
| 4 | `sms_send_scene_not_register` | scene 不是固定值 register 时返回 422 | `negative` | `P2` | BR-30、CR-10 |
| 5 | `sms_send_same_mobile_within_cooldown` | 同号 60 秒内重复发送返回 429 SMS_COOLDOWN | `business` | `P0` | BR-05 |
| 6 | `sms_captcha_required_from_third_request` | 同号第 3 次请求起要求图形验证码 | `business` | `P0` | BR-08、BR-09、BR-10 |
| 7 | `sms_daily_limit_after_ten_requests` | 同号当日第 11 次请求返回 429 SMS_DAILY_LIMIT | `business` | `P0` | BR-06、BR-10 |
| 8 | `sms_ip_hourly_limit_after_twenty_requests` | 单 IP 每小时第 21 次请求返回 429 SMS_IP_LIMIT | `business` | `P1` | BR-07 |
| 9 | `sms_channel_unavailable` | 短信通道超时或失败返回 503 SMS_CHANNEL_UNAVAILABLE | `negative` | `P0` | BR-42 |

### 用例明细

#### 1. `sms_send_success_cooldown` · 发送短信验证码成功返回固定 60 秒冷却

- 级别 / 类别：`business` / `positive`
- 优先级 / 断言强度：`P0` / `L2`
- 覆盖角色：`primary`
- 规则：BR-33 (`RK-899e2c5bc982`)、BR-03 (`RK-cbaf5b6a3bd5`)、CR-09 (`RK-70256ae39a01`)、BR-45 (`RK-e58f2db36923`)、BR-46 (`RK-af9e0cdec1ee`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码
  > `POST /api/partner/sms/send`
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 响应字段
  > | `cooldown_seconds` | integer | 下次可发送的间隔秒数，固定 60 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 响应字段
  > | `remaining_today` | integer | `max(0, 10 - 最近 24 小时已计入请求数)`，计数含本次请求 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 响应字段
  > | `captcha_required` | boolean | `最近 24 小时已计入请求数（含本次）>= 2`，即为后续（第 3 次起）请求要求图形验证码 |

Mock 绑定：

- `http:host:sms-gateway.internal POST /v1/messages` → `success`

请求：

- `POST /api/partner/sms/send`
- body:

```json
{
  "mobile": "13800138000"
}
```

断言：

- `status_code` eq `200`
- `body_field` `cooldown_seconds` eq `60`
- `body_field` `remaining_today` eq `9`
- `body_field` `captcha_required` eq `false`

设计说明：BR-33 规定 cooldown_seconds 固定 60；BR-45/BR-46 规定首次请求计数为 1，remaining_today 为 9 且 captcha_required 为 false；响应字段由契约声明。

---

#### 2. `sms_send_missing_mobile` · 缺少 mobile 时返回 422 VALIDATION_ERROR

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P0` / `L1`
- 覆盖角色：`primary`
- 规则：BR-30 (`RK-04f39ff8eaa4`)、CR-09 (`RK-70256ae39a01`)、BR-28 (`RK-6892294000bf`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误
  > | 422 | `VALIDATION_ERROR` | 手机号格式错误或缺失 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.1 通用约定
  > | 错误响应 | `{"error_code": "...", "message": "..."}` |

请求：

- `POST /api/partner/sms/send`
- body:

```json
{
  "scene": "register"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`
- `body_field` `message` exists

设计说明：BR-30 规定字段缺失返回 422；BR-28 规定错误体含 error_code/message。

---

#### 3. `sms_send_mobile_second_digit_invalid` · 手机号第二位不在 3–9 时返回 422

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-03 (`RK-cbaf5b6a3bd5`)、BR-30 (`RK-04f39ff8eaa4`)、CR-09 (`RK-70256ae39a01`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 请求参数
  > | `mobile` | string | 是 | `^1[3-9]\d{9}$` | 11 位手机号 |
- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 手机号 11 位、1 开头、第二位 3–9

请求：

- `POST /api/partner/sms/send`
- body:

```json
{
  "mobile": "12800138000"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：BR-03 要求第二位 3–9；格式校验失败由 BR-30/CR-09 归为 422。

---

#### 4. `sms_send_scene_not_register` · scene 不是固定值 register 时返回 422

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P2` / `L1`
- 覆盖角色：`primary`
- 规则：BR-30 (`RK-04f39ff8eaa4`)、CR-10 (`RK-0bb256c6882e`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 请求参数
  > | `scene` | string | 否 | 固定 `register` | 使用场景，默认 `register` |

请求：

- `POST /api/partner/sms/send`
- body:

```json
{
  "mobile": "13800138000",
  "scene": "other"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：设计 3.3 规定 scene 固定 register；枚举/常量校验失败按 BR-30 返回 422。

---

#### 5. `sms_send_same_mobile_within_cooldown` · 同号 60 秒内重复发送返回 429 SMS_COOLDOWN

- 级别 / 类别：`business` / `business`
- 优先级 / 断言强度：`P0` / `L2`
- 覆盖角色：`None`
- 规则：BR-05 (`RK-b5c2cb77d54e`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误
  > | 429 | `SMS_COOLDOWN` | 60 秒内重复发送 |

Mock 绑定：

- `http:host:sms-gateway.internal POST /v1/messages` → `success`

步骤（2 步）：

**1. `first` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138000"
}
```

断言：

- `status_code` eq `200`

**2. `second` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138000"
}
```

断言：

- `status_code` eq `429`
- `body_field` `error_code` eq `SMS_COOLDOWN`

设计说明：BR-05 规定同号 60 秒内重复发送返回 429 SMS_COOLDOWN；第一次成功建立冷却窗口。

---

#### 6. `sms_captcha_required_from_third_request` · 同号第 3 次请求起要求图形验证码

- 级别 / 类别：`business` / `business`
- 优先级 / 断言强度：`P0` / `L3`
- 覆盖角色：`None`
- 规则：BR-08 (`RK-e05a487fde65`)、BR-09 (`RK-2e101dd5af91`)、BR-10 (`RK-e397722a1497`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 频控规则
  > | 图形验证码 | 同号第 3 次起 | `CAPTCHA_REQUIRED` |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误
  > | 400 | `CAPTCHA_REQUIRED` | 已触发图形验证码但未提供 token |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 频控规则
  > 每次请求都计入尝试次数，因此「第 3 次起要求图形验证码」按请求次数而非成功次数
  > 计算。

Mock 绑定：

- `http:host:sms-gateway.internal POST /v1/messages` → `success`

步骤（3 步）：

**1. `first` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138001"
}
```

断言：

- `status_code` eq `200`

**2. `second` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138001"
}
```

断言：

- `status_code` eq `429`
- `body_field` `error_code` eq `SMS_COOLDOWN`

**3. `third` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138001"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `CAPTCHA_REQUIRED`

设计说明：BR-08/09/10：第 2 次请求虽因冷却失败仍计入次数，因此第 3 次请求触发 CAPTCHA_REQUIRED。

---

#### 7. `sms_daily_limit_after_ten_requests` · 同号当日第 11 次请求返回 429 SMS_DAILY_LIMIT

- 级别 / 类别：`business` / `business`
- 优先级 / 断言强度：`P0` / `L3`
- 覆盖角色：`None`
- 规则：BR-06 (`RK-9191fe629f9b`)、BR-10 (`RK-e397722a1497`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误
  > | 429 | `SMS_DAILY_LIMIT` | 单号最近 24 小时超过 10 次 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 频控规则
  > 每次请求都计入尝试次数，因此「第 3 次起要求图形验证码」按请求次数而非成功次数
  > 计算。

Mock 绑定：

- `http:host:sms-gateway.internal POST /v1/messages` → `success`

步骤（11 步）：

**1. `send_01` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `200`

**2. `send_02` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `429`
- `body_field` `error_code` eq `SMS_COOLDOWN`

**3. `send_03` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `CAPTCHA_REQUIRED`

**4. `send_04` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `CAPTCHA_REQUIRED`

**5. `send_05` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `CAPTCHA_REQUIRED`

**6. `send_06` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `CAPTCHA_REQUIRED`

**7. `send_07` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `CAPTCHA_REQUIRED`

**8. `send_08` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `CAPTCHA_REQUIRED`

**9. `send_09` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `CAPTCHA_REQUIRED`

**10. `send_10` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `CAPTCHA_REQUIRED`

**11. `send_11` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `429`
- `body_field` `error_code` eq `SMS_DAILY_LIMIT`

设计说明：BR-06/10：每次请求（含冷却/验证码失败）都计数，第 11 次请求先命中每日上限。

---

#### 8. `sms_ip_hourly_limit_after_twenty_requests` · 单 IP 每小时第 21 次请求返回 429 SMS_IP_LIMIT

- 级别 / 类别：`business` / `business`
- 优先级 / 断言强度：`P1` / `L3`
- 覆盖角色：`None`
- 规则：BR-07 (`RK-cf94631dac66`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误
  > | 429 | `SMS_IP_LIMIT` | 单 IP 最近 60 分钟超过 20 次 |

Mock 绑定：

- `http:host:sms-gateway.internal POST /v1/messages` → `success`

步骤（21 步）：

**1. `send_01` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138001"
}
```

断言：

- `status_code` eq `200`

**2. `send_02` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138002"
}
```

断言：

- `status_code` eq `200`

**3. `send_03` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138003"
}
```

断言：

- `status_code` eq `200`

**4. `send_04` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138004"
}
```

断言：

- `status_code` eq `200`

**5. `send_05` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138005"
}
```

断言：

- `status_code` eq `200`

**6. `send_06` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138006"
}
```

断言：

- `status_code` eq `200`

**7. `send_07` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138007"
}
```

断言：

- `status_code` eq `200`

**8. `send_08` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138008"
}
```

断言：

- `status_code` eq `200`

**9. `send_09` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138009"
}
```

断言：

- `status_code` eq `200`

**10. `send_10` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138010"
}
```

断言：

- `status_code` eq `200`

**11. `send_11` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138011"
}
```

断言：

- `status_code` eq `200`

**12. `send_12` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138012"
}
```

断言：

- `status_code` eq `200`

**13. `send_13` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138013"
}
```

断言：

- `status_code` eq `200`

**14. `send_14` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138014"
}
```

断言：

- `status_code` eq `200`

**15. `send_15` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138015"
}
```

断言：

- `status_code` eq `200`

**16. `send_16` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138016"
}
```

断言：

- `status_code` eq `200`

**17. `send_17` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138017"
}
```

断言：

- `status_code` eq `200`

**18. `send_18` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138018"
}
```

断言：

- `status_code` eq `200`

**19. `send_19` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138019"
}
```

断言：

- `status_code` eq `200`

**20. `send_20` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138020"
}
```

断言：

- `status_code` eq `200`

**21. `send_21` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138021"
}
```

断言：

- `status_code` eq `429`
- `body_field` `error_code` eq `SMS_IP_LIMIT`

设计说明：BR-07：前 20 个不同号码各成功一次，第 21 次请求从同一 IP 发出时命中每小时上限。

---

#### 9. `sms_channel_unavailable` · 短信通道超时或失败返回 503 SMS_CHANNEL_UNAVAILABLE

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P0` / `L2`
- 覆盖角色：`primary`
- 规则：BR-42 (`RK-8581f236b81a`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 短信通道失败
  > - 调用短信通道超时 5 秒；超时或失败返回 503 `SMS_CHANNEL_UNAVAILABLE`，提示
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误
  > | 503 | `SMS_CHANNEL_UNAVAILABLE` | 短信通道超时或发送失败 |

Mock 绑定：

- `http:host:sms-gateway.internal POST /v1/messages` → `rule_BR-42`

请求：

- `POST /api/partner/sms/send`
- body:

```json
{
  "mobile": "13800138021"
}
```

断言：

- `status_code` eq `503`
- `body_field` `error_code` eq `SMS_CHANNEL_UNAVAILABLE`
- `body_field` `message` exists

设计说明：BR-42 明确通道超时/失败返回 503 SMS_CHANNEL_UNAVAILABLE；Mock 注入短信网关失败分支。

假设：

- OpenAPI 快照尚未声明 503 响应，依据 docs/design.md 3.3「短信通道失败」与错误表断言。

---

## `POST /api/partner/register`

| 项 | 内容 |
|---|---|
| 请求字段 | `name`、`mobile`、`sms_code`、`city`、`id_card`、`agreement_checked`、`agreement_version`、`idempotency_key` 均必填 |
| 成功响应 | `200`，返回 `partner_id`、`status`、`audit_status`、`audit_step`、`submit_at` |
| 错误响应 | `400` 业务错误；`409` `DUPLICATE_REGISTRATION` / `MOBILE_ALREADY_REGISTERED` / `IDEMPOTENCY_KEY_CONFLICT`；`422` `VALIDATION_ERROR` |
| 下游依赖 | `http:host:id-verify.internal POST /v1/verify`、`http:host:partner-core.internal POST /internal/partners` |
| 用例数 | 29 |

### 用例清单（29 条）

| # | case_key | 标题 | 类别 | 优先级 | 规则 |
|---|---|---|---|---|---|
| 1 | `register_success_pending` | 资料合法时注册成功并返回待审核初始状态 | `positive` | `P0` | BR-24、BR-25、BR-29、CR-01、CR-02、CR-03、CR-04、CR-05、CR-06、CR-07、CR-08 |
| 2 | `register_name_empty` | 姓名为空时返回 422 校验错误 | `negative` | `P1` | BR-02、CR-07、BR-30 |
| 3 | `register_name_too_long` | 姓名超过 20 字符时返回 422 校验错误 | `boundary` | `P1` | BR-02、CR-07、BR-30 |
| 4 | `register_name_pure_digits` | 姓名为纯数字时返回 400 INVALID_NAME | `negative` | `P1` | BR-02、BR-50 |
| 5 | `register_name_pure_symbols` | 姓名为纯符号时返回 400 INVALID_NAME | `negative` | `P1` | BR-02、BR-50 |
| 6 | `register_mobile_second_digit_invalid` | 注册手机号第二位不在 3–9 时返回 422 | `negative` | `P1` | BR-03、CR-06、BR-30 |
| 7 | `register_sms_code_bad_format` | 验证码不是 6 位数字时返回 422 | `negative` | `P1` | BR-04、CR-08、BR-30 |
| 8 | `register_sms_code_wrong_value` | 验证码取值错误时返回 400 INVALID_SMS_CODE | `negative` | `P1` | BR-04、BR-22 |
| 9 | `register_city_not_in_enum` | 城市不在五市枚举内时返回 422 | `negative` | `P1` | BR-11、CR-03、BR-30 |
| 10 | `register_city_missing` | 未显式选择城市时返回 422 | `negative` | `P1` | BR-11、CR-03、BR-30 |
| 11 | `register_id_card_wrong_length` | 身份证不足 18 位时返回 422 | `negative` | `P1` | BR-12、CR-04、BR-30 |
| 12 | `register_id_card_bad_checksum` | 身份证校验位错误时返回 400 INVALID_ID_CARD | `negative` | `P1` | BR-12 |
| 13 | `register_agreement_not_checked` | 协议未勾选时返回 400 AGREEMENT_NOT_CHECKED | `negative` | `P0` | BR-15 |
| 14 | `register_agreement_checked_missing` | 缺少 agreement_checked 时返回 422 | `negative` | `P1` | CR-01、BR-15、BR-30 |
| 15 | `register_agreement_version_mismatch` | 协议版本不一致时返回 400 AGREEMENT_VERSION_MISMATCH | `negative` | `P0` | BR-16、BR-17 |
| 16 | `register_idempotency_key_too_long` | 幂等键超过 64 字符时返回 422 | `boundary` | `P1` | BR-23、CR-05、BR-30 |
| 17 | `register_idempotency_key_empty` | 幂等键为空时返回 422 | `boundary` | `P2` | BR-23、CR-05、BR-30 |
| 18 | `register_validation_order_name_before_sms` | 姓名规则先于验证码校验 | `negative` | `P2` | BR-22、BR-02 |
| 19 | `register_validation_order_sms_before_agreement` | 验证码校验先于协议勾选校验 | `negative` | `P2` | BR-22、BR-04 |
| 20 | `register_idempotent_retry_same_key` | 唯一键与幂等键都相同，重试返回原档案 | `positive` | `P0` | BR-19、BR-18 |
| 21 | `register_duplicate_unique_key_different_idempotency` | 唯一键相同但幂等键不同返回 409 DUPLICATE_REGISTRATION | `negative` | `P0` | BR-20、BR-18 |
| 22 | `register_mobile_already_registered` | 手机号已注册且唯一键不同返回 409 MOBILE_ALREADY_REGISTERED | `negative` | `P0` | BR-21、BR-18、BR-56 |
| 23 | `register_id_card_occupied_generic_message` | 身份证被占用返回 400 REGISTRATION_UNAVAILABLE 且不暴露可枚举文案 | `negative` | `P0` | BR-13、BR-14 |
| 24 | `sms_code_attempts_exceeded` | 同一验证码累计 5 次错误后作废，正确验证码也返回 SMS_CODE_ATTEMPTS_EXCEEDED | `negative` | `P0` | BR-37、BR-38、BR-04 |
| 25 | `register_name_outer_spaces_allowed` | 姓名首尾空白不 trim 且空格计入长度，合法姓名注册成功 | `boundary` | `P2` | BR-48、BR-02 |
| 26 | `register_name_fullwidth_rejected` | 姓名包含全角字母时返回 400 INVALID_NAME | `negative` | `P1` | BR-49、BR-02 |
| 27 | `register_name_spaces_only` | 姓名只有空格时返回 400 INVALID_NAME | `negative` | `P2` | BR-50、BR-02 |
| 28 | `register_id_card_lowercase_normalized` | 身份证末位小写 x 转大写后与同一业务唯一键匹配 | `business` | `P1` | BR-51、BR-18、BR-19 |
| 29 | `register_idempotency_key_conflict` | 同唯一键同幂等键但请求体不一致返回 409 IDEMPOTENCY_KEY_CONFLICT | `negative` | `P0` | BR-54、BR-55、BR-19 |

### 用例明细

#### 1. `register_success_pending` · 资料合法时注册成功并返回待审核初始状态

- 级别 / 类别：`business` / `positive`
- 优先级 / 断言强度：`P0` / `L1`
- 覆盖角色：`primary`
- 规则：BR-24 (`RK-21a1b8ccab98`)、BR-25 (`RK-bf9a2027d9f1`)、BR-29 (`RK-9c3f7ce28514`)、CR-01 (`RK-755d80c5554f`)、CR-02 (`RK-cfb46be98a9b`)、CR-03 (`RK-ff2cf864a137`)、CR-04 (`RK-f76e39c48b0d`)、CR-05 (`RK-7e1a14f4b6a1`)、CR-06 (`RK-55d70cbafcf1`)、CR-07 (`RK-d9106457ce17`)、CR-08 (`RK-ca34a125ecf9`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册
  > `POST /api/partner/register`
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 响应字段
  > | `partner_id` | string | 合伙人编号，格式 `P` + 6 位序号 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 响应字段
  > | `status` | string | 固定 `pending` |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 响应字段
  > | `submit_at` | string | 提交时间，UTC |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.1 通用约定
  > | 时间格式 | ISO 8601 UTC，秒级，`Z` 结尾，如 `2026-11-04T12:30:04Z` |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 五、业务流程设计 > 5.3 状态设计
  > | `status` | `pending` | 合伙人状态 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 五、业务流程设计 > 5.2 注册提交流程
  > 200，返回 partner_id 与待审核状态
- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 成功后写入 `partners`（`status=pending`、`audit_status=pending`、`audit_step=1`）并写一条审核日志，跳转 P-04
- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图
  > 提交 → 幂等校验 → 写入 `partners` + 审核日志 → 跳 P-04

Mock 绑定：

- `http:host:id-verify.internal POST /v1/verify` → `success`
- `http:host:partner-core.internal POST /internal/partners` → `success`

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `200`
- `body_field` `status` eq `pending`
- `body_field` `audit_status` eq `pending`
- `body_field` `audit_step` eq `1`
- `body_field` `partner_id` exists
- `body_field` `submit_at` exists
- `body_field` `submit_at` contains `Z`

设计说明：BR-24/BR-25/BR-29 规定响应状态、编号格式与时间格式；契约规则保证字段类型与必填。

备注：协议版本落库（BR-34）无法由注册响应断言；需求与设计在勾选时间/IP/UA 记录上存在差异，见 ruleset 冲突「协议元数据冲突」。

---

#### 2. `register_name_empty` · 姓名为空时返回 422 校验错误

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-02 (`RK-fcdb6785d7aa`)、CR-07 (`RK-d9106457ce17`)、BR-30 (`RK-04f39ff8eaa4`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 为空、纯符号、纯数字、超 20 字 → 输入框下方提示并阻止下一步
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `name` | string | 是 | 2–20 字符 | 中文、字母、空格、`·`；不能纯数字/符号 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：BR-02/CR-07 规定姓名至少 2 字符；长度校验失败按 BR-30 返回 422。

---

#### 3. `register_name_too_long` · 姓名超过 20 字符时返回 422 校验错误

- 级别 / 类别：`business` / `boundary`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-02 (`RK-fcdb6785d7aa`)、CR-07 (`RK-d9106457ce17`)、BR-30 (`RK-04f39ff8eaa4`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 2–20 个字符，允许中文、字母、空格、`·`
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `name` | string | 是 | 2–20 字符 | 中文、字母、空格、`·`；不能纯数字/符号 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张张张张张张张张张张张张张张张张张张张张张",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：BR-02/CR-07 规定姓名上限 20 字符；超长按 BR-30 返回 422。

---

#### 4. `register_name_pure_digits` · 姓名为纯数字时返回 400 INVALID_NAME

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-02 (`RK-fcdb6785d7aa`)、BR-50 (`RK-67c225171fe7`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 为空、纯符号、纯数字、超 20 字 → 输入框下方提示并阻止下一步
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `INVALID_NAME` | 姓名不符合业务规则 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则
  > - 姓名必须至少包含一个中文或 ASCII 字母，纯空格、纯符号、纯数字均不通过；

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "12345",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_NAME`
- `body_field` `message` exists

设计说明：BR-02 明确纯数字姓名不通过；设计错误表给出 400 INVALID_NAME。

---

#### 5. `register_name_pure_symbols` · 姓名为纯符号时返回 400 INVALID_NAME

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-02 (`RK-fcdb6785d7aa`)、BR-50 (`RK-67c225171fe7`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 为空、纯符号、纯数字、超 20 字 → 输入框下方提示并阻止下一步
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `INVALID_NAME` | 姓名不符合业务规则 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则
  > - 姓名必须至少包含一个中文或 ASCII 字母，纯空格、纯符号、纯数字均不通过；

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "!!",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_NAME`
- `body_field` `message` exists

设计说明：BR-02 明确纯符号姓名不通过；设计错误表给出 400 INVALID_NAME。

---

#### 6. `register_mobile_second_digit_invalid` · 注册手机号第二位不在 3–9 时返回 422

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-03 (`RK-cbaf5b6a3bd5`)、CR-06 (`RK-55d70cbafcf1`)、BR-30 (`RK-04f39ff8eaa4`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 手机号 11 位、1 开头、第二位 3–9
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `mobile` | string | 是 | `^1[3-9]\d{9}$` | 手机号 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "12800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：BR-03/CR-06 给出手机号正则；格式失败按 BR-30 返回 422。

---

#### 7. `register_sms_code_bad_format` · 验证码不是 6 位数字时返回 422

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-04 (`RK-54198c262de1`)、CR-08 (`RK-ca34a125ecf9`)、BR-30 (`RK-04f39ff8eaa4`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 验证码 6 位、5 分钟有效
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `sms_code` | string | 是 | `^\d{6}$`，5 分钟有效 | 短信验证码；错误累计 5 次作废 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "12345",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：BR-04/CR-08 要求 6 位数字验证码；正则失败按 BR-30 返回 422。

---

#### 8. `register_sms_code_wrong_value` · 验证码取值错误时返回 400 INVALID_SMS_CODE

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-04 (`RK-54198c262de1`)、BR-22 (`RK-6621154ef46e`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `INVALID_SMS_CODE` | 验证码错误，且未达到 5 次 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 五、业务流程设计 > 5.1 短信验证码流程
  > 示例环境不接入真实短信通道，验证码固定为 `123456`，但签发时间与错误次数仍按

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "654321",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_SMS_CODE`
- `body_field` `message` exists

设计说明：设计 5.1 声明示例固定验证码 123456；错误取值由设计错误表给出 400 INVALID_SMS_CODE。

备注：验证码时效/错误次数冲突：设计 7.2 声明示例不校验时效与次数；本用例只断言取值错误分支。

---

#### 9. `register_city_not_in_enum` · 城市不在五市枚举内时返回 422

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-11 (`RK-ae178eedf9d4`)、CR-03 (`RK-ff2cf864a137`)、BR-30 (`RK-04f39ff8eaa4`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 枚举五选一：贵阳/成都/长沙/武汉/南京；必须显式选择，不设默认值
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `city` | string | 是 | 五市枚举，来源 `CITY_LIST` 配置 | 贵阳/成都/长沙/武汉/南京；必须显式选择，无默认值 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "深圳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：BR-11/CR-03 规定五市枚举；非法枚举按 BR-30 返回 422。

备注：ruleset 冲突：INVALID_CITY 分支不可达（正常由 422 拦截）；城市列表来源冲突（需求配置下发 vs 设计固定枚举）。

---

#### 10. `register_city_missing` · 未显式选择城市时返回 422

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-11 (`RK-ae178eedf9d4`)、CR-03 (`RK-ff2cf864a137`)、BR-30 (`RK-04f39ff8eaa4`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 枚举五选一：贵阳/成都/长沙/武汉/南京；必须显式选择，不设默认值
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `city` | string | 是 | 五市枚举，来源 `CITY_LIST` 配置 | 贵阳/成都/长沙/武汉/南京；必须显式选择，无默认值 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "123456",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：BR-11 要求显式选择、不设默认值；必填缺失按 BR-30 返回 422。

---

#### 11. `register_id_card_wrong_length` · 身份证不足 18 位时返回 422

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-12 (`RK-a253eb796c16`)、CR-04 (`RK-f76e39c48b0d`)、BR-30 (`RK-04f39ff8eaa4`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 18 位，前 17 位数字、末位数字或 `X/x`，必须通过 GB 11643 校验位（ISO 7064 MOD 11-2）
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `id_card` | string | 是 | 18 位 | GB 11643 校验位 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：BR-12/CR-04 要求 18 位；长度失败按 BR-30 返回 422。

---

#### 12. `register_id_card_bad_checksum` · 身份证校验位错误时返回 400 INVALID_ID_CARD

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-12 (`RK-a253eb796c16`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 18 位，前 17 位数字、末位数字或 `X/x`，必须通过 GB 11643 校验位（ISO 7064 MOD 11-2）
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `INVALID_ID_CARD` | 身份证格式或校验位错误 |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "110105194912310021",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_ID_CARD`
- `body_field` `message` exists

设计说明：BR-12 要求通过 GB 11643 校验位；设计错误表给出 400 INVALID_ID_CARD。

---

#### 13. `register_agreement_not_checked` · 协议未勾选时返回 400 AGREEMENT_NOT_CHECKED

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P0` / `L1`
- 覆盖角色：`primary`
- 规则：BR-15 (`RK-ee3a4096128d`)

需求来源：

- `docs/requirements.md` · 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register）
  > 《合伙人合作协议》默认未勾选，必须用户主动勾选
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `AGREEMENT_NOT_CHECKED` | 协议未勾选 |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": false,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `AGREEMENT_NOT_CHECKED`
- `body_field` `message` exists

设计说明：BR-15 要求主动勾选；设计错误表给出 400 AGREEMENT_NOT_CHECKED。

---

#### 14. `register_agreement_checked_missing` · 缺少 agreement_checked 时返回 422

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：CR-01 (`RK-755d80c5554f`)、BR-15 (`RK-ee3a4096128d`)、BR-30 (`RK-04f39ff8eaa4`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `agreement_checked` | boolean | 是 | 必须为 `true` | 协议已勾选 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：CR-01 声明 agreement_checked 必填；缺失按 BR-30 返回 422，区别于 false 的业务 400。

---

#### 15. `register_agreement_version_mismatch` · 协议版本不一致时返回 400 AGREEMENT_VERSION_MISMATCH

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P0` / `L1`
- 覆盖角色：`primary`
- 规则：BR-16 (`RK-b2e4ff881538`)、BR-17 (`RK-cc5f2612cb7a`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `agreement_version` | string | 是 | 当前 `v1.0` | 勾选时展示的协议版本 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `AGREEMENT_VERSION_MISMATCH` | 协议版本不一致 |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v2.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `AGREEMENT_VERSION_MISMATCH`
- `body_field` `message` exists

设计说明：BR-17 规定当前版本 v1.0；BR-16 规定版本不一致返回 400。

---

#### 16. `register_idempotency_key_too_long` · 幂等键超过 64 字符时返回 422

- 级别 / 类别：`business` / `boundary`
- 优先级 / 断言强度：`P1` / `L1`
- 覆盖角色：`primary`
- 规则：BR-23 (`RK-d5120507c6a9`)、CR-05 (`RK-7e1a14f4b6a1`)、BR-30 (`RK-04f39ff8eaa4`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `idempotency_key` | string | 是 | 1–64 字符 | 幂等键，重试必须沿用 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：BR-23/CR-05 规定 1–64 字符；超长按 BR-30 返回 422。

---

#### 17. `register_idempotency_key_empty` · 幂等键为空时返回 422

- 级别 / 类别：`business` / `boundary`
- 优先级 / 断言强度：`P2` / `L1`
- 覆盖角色：`primary`
- 规则：BR-23 (`RK-d5120507c6a9`)、CR-05 (`RK-7e1a14f4b6a1`)、BR-30 (`RK-04f39ff8eaa4`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `idempotency_key` | string | 是 | 1–64 字符 | 幂等键，重试必须沿用 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 422 | `VALIDATION_ERROR` | 字段缺失、长度/正则/枚举校验失败（含城市不在枚举内） |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": ""
}
```

断言：

- `status_code` eq `422`
- `body_field` `error_code` eq `VALIDATION_ERROR`

设计说明：BR-23/CR-05 规定最小长度 1；空值按 BR-30 返回 422。

---

#### 18. `register_validation_order_name_before_sms` · 姓名规则先于验证码校验

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P2` / `L1`
- 覆盖角色：`primary`
- 规则：BR-22 (`RK-6621154ef46e`)、BR-02 (`RK-fcdb6785d7aa`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 校验顺序
  > 1. 姓名规则；
  > 2. 短信验证码（格式、5 分钟时效、累计 5 次错误作废）；
  > 3. 城市枚举；
  > 4. 身份证格式与校验位；
  > 5. 协议勾选；
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `INVALID_NAME` | 姓名不符合业务规则 |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "12345",
  "mobile": "13800138000",
  "sms_code": "654321",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_NAME`

设计说明：BR-22 规定姓名规则先于验证码；同时提供两类非法输入时先返回 INVALID_NAME。

---

#### 19. `register_validation_order_sms_before_agreement` · 验证码校验先于协议勾选校验

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P2` / `L1`
- 覆盖角色：`primary`
- 规则：BR-22 (`RK-6621154ef46e`)、BR-04 (`RK-54198c262de1`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 校验顺序
  > 1. 姓名规则；
  > 2. 短信验证码（格式、5 分钟时效、累计 5 次错误作废）；
  > 3. 城市枚举；
  > 4. 身份证格式与校验位；
  > 5. 协议勾选；
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `INVALID_SMS_CODE` | 验证码错误，且未达到 5 次 |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "张三",
  "mobile": "13800138000",
  "sms_code": "654321",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": false,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-13800138000-001"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_SMS_CODE`

设计说明：BR-22 规定验证码先于协议勾选；两类非法输入同时出现时先返回 INVALID_SMS_CODE。

---

#### 20. `register_idempotent_retry_same_key` · 唯一键与幂等键都相同，重试返回原档案

- 级别 / 类别：`business` / `positive`
- 优先级 / 断言强度：`P0` / `L2`
- 覆盖角色：`primary`
- 规则：BR-19 (`RK-54b6a8eb1318`)、BR-18 (`RK-ef51d27c937a`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册
  > | 唯一键已存在且 `idempotency_key` 相同、请求体一致 | 返回原档案，HTTP 200，`partner_id` 不变 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 响应字段
  > | `partner_id` | string | 合伙人编号，格式 `P` + 6 位序号 |

前置状态：

- `db_state` · `state_partner_registered`（数据目录预置）

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "李四",
  "mobile": "13900139000",
  "sms_code": "123456",
  "city": "成都",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "seed-key-000"
}
```

断言：

- `status_code` eq `200`
- `body_field` `partner_id` eq `P000000`
- `body_field` `status` eq `pending`

设计说明：BR-19 规定同一唯一键 + 同一幂等键返回原档案；预置状态中的 partner_id 为 P000000，重试应原样返回。

---

#### 21. `register_duplicate_unique_key_different_idempotency` · 唯一键相同但幂等键不同返回 409 DUPLICATE_REGISTRATION

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P0` / `L1`
- 覆盖角色：`primary`
- 规则：BR-20 (`RK-f18646e6a101`)、BR-18 (`RK-ef51d27c937a`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册
  > | 唯一键已存在但 `idempotency_key` 不同 | 409 `DUPLICATE_REGISTRATION` |

前置状态：

- `db_state` · `state_partner_registered`（数据目录预置）

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "李四",
  "mobile": "13900139000",
  "sms_code": "123456",
  "city": "成都",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "seed-key-999"
}
```

断言：

- `status_code` eq `409`
- `body_field` `error_code` eq `DUPLICATE_REGISTRATION`

设计说明：BR-20 规定唯一键已存在且幂等键不同时返回 409 DUPLICATE_REGISTRATION。

---

#### 22. `register_mobile_already_registered` · 手机号已注册且唯一键不同返回 409 MOBILE_ALREADY_REGISTERED

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P0` / `L1`
- 覆盖角色：`primary`
- 规则：BR-21 (`RK-42ced5ce95ff`)、BR-18 (`RK-ef51d27c937a`)、BR-56 (`RK-deb60e373901`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册
  > | 手机号已存在、唯一键不同 | 409 `MOBILE_ALREADY_REGISTERED` |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册
  > `409 MOBILE_ALREADY_REGISTERED` 只表示手机号已注册；前端根据 P-04 查询到的

前置状态：

- `db_state` · `state_partner_registered`（数据目录预置）

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "赵六",
  "mobile": "13900139000",
  "sms_code": "123456",
  "city": "南京",
  "id_card": "110105194912310038",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "seed-key-998"
}
```

断言：

- `status_code` eq `409`
- `body_field` `error_code` eq `MOBILE_ALREADY_REGISTERED`

设计说明：BR-21 规定手机号已存在但唯一键不同时返回 409 MOBILE_ALREADY_REGISTERED；请求使用另一个合法身份证号。

假设：

- 画像未声明 Location 响应头，因此用 409 + error_code 证明本服务不返回跳转地址；跳转逻辑属于前端/P-04。

---

#### 23. `register_id_card_occupied_generic_message` · 身份证被占用返回 400 REGISTRATION_UNAVAILABLE 且不暴露可枚举文案

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P0` / `L1`
- 覆盖角色：`primary`
- 规则：BR-13 (`RK-93bd5aec1ad5`)、BR-14 (`RK-b007f22321c4`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册
  > | 身份证已存在、唯一键不同 | 400 `REGISTRATION_UNAVAILABLE`（通用提示） |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册
  > 身份证占用不使用「该身份证已注册」这类可枚举文案。

前置状态：

- `db_state` · `state_partner_id_card_occupied`（数据目录预置）

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "孙七",
  "mobile": "13800138000",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "seed-key-997"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `REGISTRATION_UNAVAILABLE`
- `body_field` `message` exists
- `body_field` `message` ne `该身份证已注册`

设计说明：BR-13/BR-14 规定身份证占用返回通用 400 REGISTRATION_UNAVAILABLE，禁止使用可枚举文案；预置状态占用同一身份证。

---

#### 24. `sms_code_attempts_exceeded` · 同一验证码累计 5 次错误后作废，正确验证码也返回 SMS_CODE_ATTEMPTS_EXCEEDED

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P0` / `L3`
- 覆盖角色：`primary`
- 规则：BR-37 (`RK-e2e269050df5`)、BR-38 (`RK-21d8574fb237`)、BR-04 (`RK-54198c262de1`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 验证码签发与时效
  > - 同一验证码最多允许 5 次校验失败，第 5 次失败后立即作废，必须重新获取；
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `SMS_CODE_ATTEMPTS_EXCEEDED` | 错误累计达到 5 次，验证码已作废 |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `INVALID_SMS_CODE` | 验证码错误，且未达到 5 次 |

步骤（7 步）：

**1. `send_code` · `POST /api/partner/sms/send`**

body：

```json
{
  "mobile": "13800138022"
}
```

断言：

- `status_code` eq `200`

**2. `wrong_01` · `POST /api/partner/register`**

body：

```json
{
  "name": "张三",
  "mobile": "13800138022",
  "sms_code": "000000",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "attempt-key-01"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_SMS_CODE`

**3. `wrong_02` · `POST /api/partner/register`**

body：

```json
{
  "name": "张三",
  "mobile": "13800138022",
  "sms_code": "000000",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "attempt-key-02"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_SMS_CODE`

**4. `wrong_03` · `POST /api/partner/register`**

body：

```json
{
  "name": "张三",
  "mobile": "13800138022",
  "sms_code": "000000",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "attempt-key-03"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_SMS_CODE`

**5. `wrong_04` · `POST /api/partner/register`**

body：

```json
{
  "name": "张三",
  "mobile": "13800138022",
  "sms_code": "000000",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "attempt-key-04"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_SMS_CODE`

**6. `wrong_05` · `POST /api/partner/register`**

body：

```json
{
  "name": "张三",
  "mobile": "13800138022",
  "sms_code": "000000",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "attempt-key-05"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` in `["INVALID_SMS_CODE", "SMS_CODE_ATTEMPTS_EXCEEDED"]`

**7. `correct_after_limit` · `POST /api/partner/register`**

body：

```json
{
  "name": "张三",
  "mobile": "13800138022",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "attempt-key-06"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `SMS_CODE_ATTEMPTS_EXCEEDED`

设计说明：BR-37/BR-38：第 5 次校验失败后验证码作废，后续即使提交正确验证码也返回 SMS_CODE_ATTEMPTS_EXCEEDED。

假设：

- 第 5 次失败返回 INVALID_SMS_CODE 还是 SMS_CODE_ATTEMPTS_EXCEEDED 存在边界歧义，使用 in 断言覆盖两种设计口径。
- 示例环境验证码固定为 123456；用例先调用 sms/send 建立验证码记录。

---

#### 25. `register_name_outer_spaces_allowed` · 姓名首尾空白不 trim 且空格计入长度，合法姓名注册成功

- 级别 / 类别：`business` / `boundary`
- 优先级 / 断言强度：`P2` / `L2`
- 覆盖角色：`primary`
- 规则：BR-48 (`RK-2b7d4d54be3f`)、BR-02 (`RK-fcdb6785d7aa`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则
  > - `name` 长度按 Unicode 码点计数（Python `len`），首尾空白不做 trim，空格计入长度；
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数
  > | `name` | string | 是 | 2–20 字符 | 中文、字母、空格、`·`；不能纯数字/符号 |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": " 张三 ",
  "mobile": "13800138023",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-name-outer-spaces"
}
```

断言：

- `status_code` eq `200`
- `body_field` `status` eq `pending`
- `body_field` `partner_id` exists

设计说明：BR-48/BR-02：首尾空格不裁剪、空格计入长度，` 张三 ` 长度为 5 且包含中文，应通过姓名规则。

---

#### 26. `register_name_fullwidth_rejected` · 姓名包含全角字母时返回 400 INVALID_NAME

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P1` / `L2`
- 覆盖角色：`primary`
- 规则：BR-49 (`RK-73a9395ce979`)、BR-02 (`RK-fcdb6785d7aa`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则
  > - 不允许全角字母/数字、全角空格（U+3000）及其他符号；
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `INVALID_NAME` | 姓名不符合业务规则 |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "ＡＢ",
  "mobile": "13800138026",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-name-fullwidth"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_NAME`

设计说明：BR-49/BR-02：全角字母不在允许字符集内，应由姓名业务规则返回 400 INVALID_NAME。

---

#### 27. `register_name_spaces_only` · 姓名只有空格时返回 400 INVALID_NAME

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P2` / `L2`
- 覆盖角色：`primary`
- 规则：BR-50 (`RK-67c225171fe7`)、BR-02 (`RK-fcdb6785d7aa`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则
  > - 姓名必须至少包含一个中文或 ASCII 字母，纯空格、纯符号、纯数字均不通过；
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误
  > | 400 | `INVALID_NAME` | 姓名不符合业务规则 |

请求：

- `POST /api/partner/register`
- body:

```json
{
  "name": "  ",
  "mobile": "13800138027",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-name-spaces-only"
}
```

断言：

- `status_code` eq `400`
- `body_field` `error_code` eq `INVALID_NAME`

设计说明：BR-50/BR-02：姓名长度为 2 满足长度约束，但不含任何中文或 ASCII 字母，应返回 400 INVALID_NAME。

---

#### 28. `register_id_card_lowercase_normalized` · 身份证末位小写 x 转大写后与同一业务唯一键匹配

- 级别 / 类别：`business` / `business`
- 优先级 / 断言强度：`P1` / `L3`
- 覆盖角色：`primary`
- 规则：BR-51 (`RK-e1d1fc36b631`)、BR-18 (`RK-ef51d27c937a`)、BR-19 (`RK-54b6a8eb1318`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则
  > - `id_card` 与业务唯一键中的身份证统一转大写后再比较与入库。
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册
  > | 唯一键已存在且 `idempotency_key` 相同、请求体一致 | 返回原档案，HTTP 200，`partner_id` 不变 |

步骤（2 步）：

**1. `first_lowercase` · `POST /api/partner/register`**

body：

```json
{
  "name": "张三",
  "mobile": "13800138024",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002x",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-lower-x"
}
```

断言：

- `status_code` eq `200`
- `body_field` `partner_id` exists

**2. `second_uppercase_same_key` · `POST /api/partner/register`**

body：

```json
{
  "name": "张三",
  "mobile": "13800138024",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-lower-x"
}
```

断言：

- `status_code` eq `200`
- `body_field` `partner_id` eq `{"$ref": "first_lowercase.partner_id"}`

设计说明：BR-51/BR-18/BR-19：小写 x 与大写 X 归一化后属于同一业务唯一键和同一请求体，第二次应返回首次 partner_id。

---

#### 29. `register_idempotency_key_conflict` · 同唯一键同幂等键但请求体不一致返回 409 IDEMPOTENCY_KEY_CONFLICT

- 级别 / 类别：`business` / `negative`
- 优先级 / 断言强度：`P0` / `L3`
- 覆盖角色：`primary`
- 规则：BR-54 (`RK-b9761fab3cc1`)、BR-55 (`RK-88249f9b80de`)、BR-19 (`RK-54b6a8eb1318`)

需求来源：

- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册
  > | 唯一键已存在且 `idempotency_key` 相同、请求体不同 | 409 `IDEMPOTENCY_KEY_CONFLICT` |
- `docs/design.md` · 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册
  > 请求体一致性按 `name`、`mobile`、`id_card`（大写）、`city`、`agreement_version`
  > 比较；任一字段变化视为不同请求。

步骤（2 步）：

**1. `first_submit` · `POST /api/partner/register`**

body：

```json
{
  "name": "张三",
  "mobile": "13800138025",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-conflict"
}
```

断言：

- `status_code` eq `200`
- `body_field` `partner_id` exists

**2. `second_changed_name` · `POST /api/partner/register`**

body：

```json
{
  "name": "李四",
  "mobile": "13800138025",
  "sms_code": "123456",
  "city": "贵阳",
  "id_card": "11010519491231002X",
  "agreement_checked": true,
  "agreement_version": "v1.0",
  "idempotency_key": "reg-conflict"
}
```

断言：

- `status_code` eq `409`
- `body_field` `error_code` eq `IDEMPOTENCY_KEY_CONFLICT`

设计说明：BR-54/BR-55/BR-19：业务唯一键与幂等键相同但 name 变化，请求体视为不一致，应返回 409 而不是首次档案。

---

## 规则索引（本用例集引用）

| rule_id | rule_key | 规则 | 来源 |
|---|---|---|---|
| BR-33 | `RK-899e2c5bc982` | 发送成功返回的 cooldown_seconds 固定为 60 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 响应字段 |
| BR-03 | `RK-cbaf5b6a3bd5` | 手机号为 11 位、1 开头、第二位 3–9 | 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register） |
| CR-09 | `RK-70256ae39a01` | body.mobile: type=string; pattern=^1[3-9]\d{9}$; required=True | POST /api/partner/sms/send |
| BR-45 | `RK-e58f2db36923` | `remaining_today` 按 `max(0, 10 - 最近 24 小时已计入请求数)` 计算，计数含本次请求 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 响应字段 |
| BR-46 | `RK-af9e0cdec1ee` | 最近 24 小时已计入请求数（含本次）达到 2 次后，`captcha_required` 为真 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 响应字段 |
| BR-30 | `RK-04f39ff8eaa4` | 字段缺失、长度、正则或枚举校验失败（含城市不在枚举内）时返回 422 `VALIDATION_ERROR` | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误 |
| BR-28 | `RK-6892294000bf` | 错误响应统一为包含 error_code 与 message 的 JSON 对象 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.1 通用约定 |
| CR-10 | `RK-0bb256c6882e` | body.scene: type=string | POST /api/partner/sms/send |
| BR-24 | `RK-21a1b8ccab98` | 提交成功后 status 为 pending、audit_status 为 pending、audit_step 为 1 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 响应字段 |
| BR-25 | `RK-bf9a2027d9f1` | partner_id 的格式为 P + 6 位序号 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 响应字段 |
| BR-29 | `RK-9c3f7ce28514` | 时间字段使用 ISO 8601 UTC、秒级、Z 结尾的格式 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.1 通用约定 |
| CR-01 | `RK-755d80c5554f` | body.agreement_checked: type=boolean; required=True | POST /api/partner/register |
| CR-02 | `RK-cfb46be98a9b` | body.agreement_version: type=string; required=True | POST /api/partner/register |
| CR-03 | `RK-ff2cf864a137` | body.city: type=string; enum=["贵阳", "成都", "长沙", "武汉", "南京"]; required=True | POST /api/partner/register |
| CR-04 | `RK-f76e39c48b0d` | body.id_card: type=string; maxLength=18; minLength=18; required=True | POST /api/partner/register |
| CR-05 | `RK-7e1a14f4b6a1` | body.idempotency_key: type=string; maxLength=64; minLength=1; required=True | POST /api/partner/register |
| CR-06 | `RK-55d70cbafcf1` | body.mobile: type=string; pattern=^1[3-9]\d{9}$; required=True | POST /api/partner/register |
| CR-07 | `RK-d9106457ce17` | body.name: type=string; maxLength=20; minLength=2; required=True | POST /api/partner/register |
| CR-08 | `RK-ca34a125ecf9` | body.sms_code: type=string; pattern=^\d{6}$; required=True | POST /api/partner/register |
| BR-02 | `RK-fcdb6785d7aa` | 姓名为 2–20 个字符，允许中文、字母、空格、`·`；为空、纯符号、纯数字、超 20 字时不通过 | 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register） |
| BR-50 | `RK-67c225171fe7` | 姓名必须至少包含一个中文或 ASCII 字母，纯空格、纯符号、纯数字均不通过 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则 |
| BR-04 | `RK-54198c262de1` | 验证码为 6 位 | 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register） |
| BR-22 | `RK-6621154ef46e` | 注册校验按城市列表配置可用性、姓名规则、短信验证码（格式、5 分钟时效、累计 5 次错误作废）、城市枚举、身份证格式与校验位、协议勾选、协议版本、业务唯一键幂等判断、手机号是否已注册、身份证是否被占用、写库的顺序执行 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 校验顺序 |
| BR-11 | `RK-ae178eedf9d4` | 所在城市必须从贵阳/成都/长沙/武汉/南京五选一显式选择，不设默认值 | 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register） |
| BR-12 | `RK-a253eb796c16` | 身份证号为 18 位，前 17 位为数字，末位为数字或 X/x，并通过 GB 11643 校验位（ISO 7064 MOD 11-2） | 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register） |
| BR-15 | `RK-ee3a4096128d` | 协议默认未勾选，必须由用户主动勾选 | 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register） |
| BR-16 | `RK-b2e4ff881538` | 协议版本不一致时返回 400 AGREEMENT_VERSION_MISMATCH | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误 |
| BR-17 | `RK-cc5f2612cb7a` | agreement_version 的当前版本为 v1.0 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数 |
| BR-23 | `RK-d5120507c6a9` | idempotency_key 长度为 1–64 字符，重试必须沿用同一个值 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数 |
| BR-19 | `RK-54b6a8eb1318` | 唯一键已存在且 `idempotency_key` 相同、请求体一致时，返回原档案，HTTP 200，`partner_id` 不变 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-18 | `RK-ef51d27c937a` | 注册业务唯一键为「手机号 + 身份证（大写）」 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-20 | `RK-f18646e6a101` | 唯一键已存在但 idempotency_key 不同时返回 409 DUPLICATE_REGISTRATION | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-21 | `RK-42ced5ce95ff` | 手机号已存在且唯一键不同时返回 409 MOBILE_ALREADY_REGISTERED | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-56 | `RK-deb60e373901` | `409 MOBILE_ALREADY_REGISTERED` 只表示手机号已注册，本服务不返回跳转地址，也不实现 P-04 状态查询 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-13 | `RK-93bd5aec1ad5` | 身份证已被占用时返回 400 REGISTRATION_UNAVAILABLE，并使用通用提示 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误 |
| BR-14 | `RK-b007f22321c4` | 身份证占用的提示与其他情况一致，禁止提示「该身份证已注册」，以防枚举 | 合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-register） |
| BR-05 | `RK-b5c2cb77d54e` | 同号在 60 秒内重复发送返回 429 SMS_COOLDOWN | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误 |
| BR-08 | `RK-e05a487fde65` | 图形验证码从同号第 3 次起触发 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 频控规则 |
| BR-09 | `RK-2e101dd5af91` | 已触发图形验证码但未提供 token 时返回 400 CAPTCHA_REQUIRED | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误 |
| BR-10 | `RK-e397722a1497` | 图形验证码按请求次数而非成功次数计算 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 频控规则 |
| BR-06 | `RK-9191fe629f9b` | 单号最近 24 小时超过 10 次时返回 429 `SMS_DAILY_LIMIT` | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误 |
| BR-07 | `RK-cf94631dac66` | 单 IP 最近 60 分钟超过 20 次时返回 429 `SMS_IP_LIMIT` | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误 |
| BR-42 | `RK-8581f236b81a` | 调用短信通道超时 5 秒或发送失败时返回 503 `SMS_CHANNEL_UNAVAILABLE` | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 短信通道失败 |
| BR-37 | `RK-e2e269050df5` | 同一验证码最多允许 5 次校验失败，第 5 次失败后立即作废 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 验证码签发与时效 |
| BR-38 | `RK-21d8574fb237` | 验证码错误累计达到 5 次时返回 400 `SMS_CODE_ATTEMPTS_EXCEEDED` | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误 |
| BR-48 | `RK-2b7d4d54be3f` | `name` 长度按 Unicode 码点计数，首尾空白不做 trim，空格计入长度 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则 |
| BR-49 | `RK-73a9395ce979` | 姓名允许中文、ASCII 字母、半角空格与间隔号 `·`，不允许全角字母/数字、全角空格及其他符号 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则 |
| BR-51 | `RK-e1d1fc36b631` | `id_card` 与业务唯一键中的身份证统一转大写后再比较与入库 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则 |
| BR-54 | `RK-b9761fab3cc1` | 同唯一键、同 `idempotency_key` 但请求体不一致时返回 409 `IDEMPOTENCY_KEY_CONFLICT` | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-55 | `RK-88249f9b80de` | 请求体一致性按 `name`、`mobile`、`id_card`（大写）、`city`、`agreement_version` 比较，任一字段变化视为不同请求 | 合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |

## 未覆盖规则（草案显式声明）

| rule_id | rule_key | 规则 | 原因 |
|---|---|---|---|
| BR-41 | `RK-27def6ca9607` | `captcha_token` 由外部图形验证码服务签发，本服务不解析 token 内容，仅透传校验 | 图形验证码由外部服务签发，示例环境接受任意非空 token，CAPTCHA_INVALID 分支不可达。 |
| BR-40 | `RK-aef02461ba06` | 图形验证码 token 校验不通过时返回 400 `CAPTCHA_INVALID` | 图形验证码由外部服务签发，示例环境接受任意非空 token，CAPTCHA_INVALID 分支不可达。 |
| BR-52 | `RK-0f04fc213b28` | 城市枚举由配置项 `CITY_LIST` 下发，默认五市：贵阳/成都/长沙/武汉/南京 | CITY_LIST 配置在服务启动时加载，当前画像与 Mock 无法切换为空/失败配置。 |
| BR-53 | `RK-2270b37d1605` | 城市列表配置为空或加载失败时，`POST /api/partner/register` 返回 503 `CITY_LIST_UNAVAILABLE`，不执行校验与写库 | CITY_LIST 配置在服务启动时加载，当前画像与 Mock 无法切换为空/失败配置。 |
| BR-60 | `RK-688ec12eb515` | 协议元数据中的 IP 与 UA 不写入业务日志正文 | 协议元数据仅落库，未通过响应字段或画像内调试接口暴露；需数据库断言或专用测试接口。 |
| BR-58 | `RK-83dbe1f39182` | 提交注册时记录 `agreement_ip`，优先取 `X-Forwarded-For` 首个地址，否则取连接地址 | 协议元数据仅落库，未通过响应字段或画像内调试接口暴露；需数据库断言或专用测试接口。 |
| BR-59 | `RK-c401b0c2729a` | 提交注册时记录 `agreement_user_agent`，取 `User-Agent` 请求头，缺失时记空字符串 | 协议元数据仅落库，未通过响应字段或画像内调试接口暴露；需数据库断言或专用测试接口。 |
| BR-57 | `RK-c786351e5661` | 提交注册时记录 `agreement_checked_at`，取服务端接收时间，UTC 精确到秒 | 协议元数据仅落库，未通过响应字段或画像内调试接口暴露；需数据库断言或专用测试接口。 |
| BR-61 | `RK-e0400def32f3` | 协议版本更新后，已提交且仍为 `pending` 的档案保留提交时的 `agreement_version`，不追溯重签 | 协议元数据仅落库，未通过响应字段或画像内调试接口暴露；需数据库断言或专用测试接口。 |
| BR-27 | `RK-d2d2df562935` | 提交动作的审核日志为 action=submit、from_status=NULL、to_status=pending、audit_step=1 | 审核日志仅落库，画像不含可读取日志的接口。 |
| BR-62 | `RK-d88ca5c53771` | 并发提交发生唯一约束冲突后按业务唯一键重查；幂等键与请求体一致时返回已存在档案（200） | 事务回滚与真实并发冲突无法用单进程顺序请求构造。 |
| BR-63 | `RK-dab1ae36c5fb` | 并发冲突重查后请求体不一致时返回 409 `IDEMPOTENCY_KEY_CONFLICT` | 事务回滚与真实并发冲突无法用单进程顺序请求构造。 |
| BR-26 | `RK-ff48f61206c3` | 合伙人主档与审核日志在同一事务内提交，任一失败整体回滚 | 事务回滚与真实并发冲突无法用单进程顺序请求构造。 |
| BR-32 | `RK-14cee17a910d` | 日志不得打印请求体，避免身份证明文进入日志 | 数据安全约束涉及日志、掩码与调试接口，当前画像无法直接观测。 |
| BR-64 | `RK-2a847a10ae14` | 协议元数据 `agreement_ip` 与 `agreement_user_agent` 随主档保存，保留期限与主档一致 | 数据安全约束涉及日志、掩码与调试接口，当前画像无法直接观测。 |
| BR-31 | `RK-8441f643b13e` | 接口响应不返回身份证，调试接口按前 6 位 + ******** + 后 4 位掩码 | 数据安全约束涉及日志、掩码与调试接口，当前画像无法直接观测。 |
| BR-65 | `RK-93cccaa68d09` | `/_debug/state` 不返回 `agreement_ip` 与 `agreement_user_agent` | 数据安全约束涉及日志、掩码与调试接口，当前画像无法直接观测。 |
| BR-66 | `RK-7255e61ae7e3` | 示例环境（4C8G、本地 SQLite、单进程）注册接口 p95 ≤ 200 ms、短信发送接口 p95 ≤ 100 ms（不含外部通道耗时），以 10 并发、60 秒窗口统计 | 性能基线需要压测环境与并发统计，单接口用例无法验证。 |
| BR-67 | `RK-60baf9d77c09` | 生产部署必须在网关层增加鉴权、限流与审计访问控制 | 生产网关鉴权/部署约束，示例服务无鉴权且画像不含网关。 |
| BR-01 | `RK-6ef0b7b08ea3` | 未注册用户可免登录访问 | 该 section 已有关联用例，但以下规则依赖不可观测状态、外部时钟/服务或配置，当前接口与 Mock 无法单独构造。 |
| BR-34 | `RK-d869bfddf8e8` | 提交注册时记录协议版本号 | 该 section 已有关联用例，但以下规则依赖不可观测状态、外部时钟/服务或配置，当前接口与 Mock 无法单独构造。 |
| BR-39 | `RK-616729cde108` | 重新获取验证码会重置有效期与错误次数 | 该 section 已有关联用例，但以下规则依赖不可观测状态、外部时钟/服务或配置，当前接口与 Mock 无法单独构造。 |
| BR-35 | `RK-9749d67fe795` | 短信验证码有效期为 5 分钟 | 该 section 已有关联用例，但以下规则依赖不可观测状态、外部时钟/服务或配置，当前接口与 Mock 无法单独构造。 |
| BR-36 | `RK-84c646a8f260` | 验证码超过 5 分钟有效期时返回 400 `SMS_CODE_EXPIRED` | 该 section 已有关联用例，但以下规则依赖不可观测状态、外部时钟/服务或配置，当前接口与 Mock 无法单独构造。 |
| BR-43 | `RK-1c52485c015e` | 短信通道失败请求计入单号/单 IP 频控尝试次数，但不启动 60 秒冷却，允许用户立即重试 | 该 section 已有关联用例，但以下规则依赖不可观测状态、外部时钟/服务或配置，当前接口与 Mock 无法单独构造。 |
| BR-44 | `RK-4f91ae1eb7a3` | 短信通道发送失败时不记录验证码签发时间，也不清零该手机号已有的错误次数 | 该 section 已有关联用例，但以下规则依赖不可观测状态、外部时钟/服务或配置，当前接口与 Mock 无法单独构造。 |
| BR-47 | `RK-5b5f731d5892` | 频控窗口为最近 24 小时与最近 60 分钟滚动窗口，均以 UTC 计时，左闭右开 | 该 section 已有关联用例，但以下规则依赖不可观测状态、外部时钟/服务或配置，当前接口与 Mock 无法单独构造。 |

## 执行说明

- 当前仅有用例草案，未生成 pytest 脚本；需先人工审核并通过 `cases approve`。
- 批准后按接口生成脚本：`test_post_api_partner_sms_send.py`、`test_post_api_partner_register.py`。`GET /health` 没有业务用例，不会生成脚本。
- 多步骤用例在同一测试函数内顺序执行，中间步骤的响应可供后续步骤引用。
- 4 条带 `preconditions` 的用例需要 C5b `data_plan.json` 预置数据库状态。
- 示例服务仍是旧实现，新增设计用例执行前需要先升级 `app.py`、`schema.sql`、`openapi.json` 到 V1.1；否则失败应归因为实现差距，不是用例错误。
