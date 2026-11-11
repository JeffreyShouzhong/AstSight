# partner_onboarding

> 由 `ruleset.json` 渲染：版本 v3，生成于 2026-11-10T14:11:13+00:00。
> 本文件是只读视图。规则有误请回写需求文档后重新抽取，不要直接编辑规则库——手改会让规则库变成第二个人工维护的真相源。

## 1 统计

| 项 | 数量 |
|---|---|
| 业务规则 | 67 |
| A 级（有出处、可判定） | 67（100.0%） |
| B 级（需推断或缺值） | 0 |
| 无原文落地（ungrounded） | 0 |
| 契约规则（CR） | 10 |
| 冲突 | 0 |
| 其中资损 | 0 |
| 其中静默失效 | 0 |
| 定义缺口 | 1 |
| 隐含假设 | 3 |
| 待确认问题 | 5 |

## 2 规则表

| 编号 | 规则 | 级 | 类型 | 状态 | 出处 |
|---|---|---|---|---|---|
| BR-01 | 未注册用户可免登录访问 | A | business_constraint | 有效 | docs/requirements.md#合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-regi… |
| BR-02 | 姓名为 2–20 个字符，允许中文、字母、空格、`·`；为空、纯符号、纯数字、超 20 字时不通过 | A | validation | 有效 | docs/requirements.md#合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-regi… |
| BR-03 | 手机号为 11 位、1 开头、第二位 3–9 | A | validation | 有效 | docs/requirements.md#合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-regi… |
| BR-04 | 验证码为 6 位 | A | validation | 有效 | docs/requirements.md#合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-regi… |
| BR-05 | 同号在 60 秒内重复发送返回 429 SMS_COOLDOWN | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误 |
| BR-06 | 单号最近 24 小时超过 10 次时返回 429 `SMS_DAILY_LIMIT` | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误 |
| BR-07 | 单 IP 最近 60 分钟超过 20 次时返回 429 `SMS_IP_LIMIT` | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误 |
| BR-08 | 图形验证码从同号第 3 次起触发 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 频控规则 |
| BR-09 | 已触发图形验证码但未提供 token 时返回 400 CAPTCHA_REQUIRED | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 错误 |
| BR-10 | 图形验证码按请求次数而非成功次数计算 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 频控规则 |
| BR-11 | 所在城市必须从贵阳/成都/长沙/武汉/南京五选一显式选择，不设默认值 | A | validation | 有效 | docs/requirements.md#合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-regi… |
| BR-12 | 身份证号为 18 位，前 17 位为数字，末位为数字或 X/x，并通过 GB 11643 校验位（ISO 7064 MOD 11-2） | A | validation | 有效 | docs/requirements.md#合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-regi… |
| BR-13 | 身份证已被占用时返回 400 REGISTRATION_UNAVAILABLE，并使用通用提示 | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误 |
| BR-14 | 身份证占用的提示与其他情况一致，禁止提示「该身份证已注册」，以防枚举 | A | business_constraint | 有效 | docs/requirements.md#合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-regi… |
| BR-15 | 协议默认未勾选，必须由用户主动勾选 | A | business_constraint | 有效 | docs/requirements.md#合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-regi… |
| BR-16 | 协议版本不一致时返回 400 AGREEMENT_VERSION_MISMATCH | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误 |
| BR-17 | agreement_version 的当前版本为 v1.0 | A | validation | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数 |
| BR-18 | 注册业务唯一键为「手机号 + 身份证（大写）」 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-19 | 唯一键已存在且 `idempotency_key` 相同、请求体一致时，返回原档案，HTTP 200，`partner_id` 不变 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-20 | 唯一键已存在但 idempotency_key 不同时返回 409 DUPLICATE_REGISTRATION | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-21 | 手机号已存在且唯一键不同时返回 409 MOBILE_ALREADY_REGISTERED | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-22 | 注册校验按城市列表配置可用性、姓名规则、短信验证码（格式、5 分钟时效、累计 5 次错误作废）、城市枚举、身份证格式与校验位、协议勾选、协议版本、业务唯一键幂等判断、手机号是否已注册、身份证是否被占用、写库的顺序执行 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 校验顺序 |
| BR-23 | idempotency_key 长度为 1–64 字符，重试必须沿用同一个值 | A | validation | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 请求参数 |
| BR-24 | 提交成功后 status 为 pending、audit_status 为 pending、audit_step 为 1 | A | state_transition | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 响应字段 |
| BR-25 | partner_id 的格式为 P + 6 位序号 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 响应字段 |
| BR-26 | 合伙人主档与审核日志在同一事务内提交，任一失败整体回滚 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 四、数据库设计 > 4.5 事务与并发 |
| BR-27 | 提交动作的审核日志为 action=submit、from_status=NULL、to_status=pending、audit_step=1 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 四、数据库设计 > 4.3 partner_audit_log（审核日志） |
| BR-28 | 错误响应统一为包含 error_code 与 message 的 JSON 对象 | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.1 通用约定 |
| BR-29 | 时间字段使用 ISO 8601 UTC、秒级、Z 结尾的格式 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.1 通用约定 |
| BR-30 | 字段缺失、长度、正则或枚举校验失败（含城市不在枚举内）时返回 422 `VALIDATION_ERROR` | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误 |
| BR-31 | 接口响应不返回身份证，调试接口按前 6 位 + ******** + 后 4 位掩码 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 四、数据库设计 > 4.6 数据安全 |
| BR-32 | 日志不得打印请求体，避免身份证明文进入日志 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 四、数据库设计 > 4.6 数据安全 |
| BR-33 | 发送成功返回的 cooldown_seconds 固定为 60 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 响应字段 |
| BR-34 | 提交注册时记录协议版本号 | A | data_consistency | 有效 | docs/requirements.md#合伙人注册需求文档 > 六、功能详细说明 > 6.1 产品流程图 > 6.2.1 P-03 合伙人注册（p-regi… |
| BR-35 | 短信验证码有效期为 5 分钟 | A | validation | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 验证码签发与时效 |
| BR-36 | 验证码超过 5 分钟有效期时返回 400 `SMS_CODE_EXPIRED` | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误 |
| BR-37 | 同一验证码最多允许 5 次校验失败，第 5 次失败后立即作废 | A | state_transition | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 验证码签发与时效 |
| BR-38 | 验证码错误累计达到 5 次时返回 400 `SMS_CODE_ATTEMPTS_EXCEEDED` | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 错误 |
| BR-39 | 重新获取验证码会重置有效期与错误次数 | A | state_transition | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 验证码签发与时效 |
| BR-40 | 图形验证码 token 校验不通过时返回 400 `CAPTCHA_INVALID` | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 图形验证码契约 |
| BR-41 | `captcha_token` 由外部图形验证码服务签发，本服务不解析 token 内容，仅透传校验 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 图形验证码契约 |
| BR-42 | 调用短信通道超时 5 秒或发送失败时返回 503 `SMS_CHANNEL_UNAVAILABLE` | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 短信通道失败 |
| BR-43 | 短信通道失败请求计入单号/单 IP 频控尝试次数，但不启动 60 秒冷却，允许用户立即重试 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 短信通道失败 |
| BR-44 | 短信通道发送失败时不记录验证码签发时间，也不清零该手机号已有的错误次数 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 短信通道失败 |
| BR-45 | `remaining_today` 按 `max(0, 10 - 最近 24 小时已计入请求数)` 计算，计数含本次请求 | A | calculation | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 响应字段 |
| BR-46 | 最近 24 小时已计入请求数（含本次）达到 2 次后，`captcha_required` 为真 | A | calculation | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 响应字段 |
| BR-47 | 频控窗口为最近 24 小时与最近 60 分钟滚动窗口，均以 UTC 计时，左闭右开 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.3 发送短信验证码 > 频控规则 |
| BR-48 | `name` 长度按 Unicode 码点计数，首尾空白不做 trim，空格计入长度 | A | validation | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则 |
| BR-49 | 姓名允许中文、ASCII 字母、半角空格与间隔号 `·`，不允许全角字母/数字、全角空格及其他符号 | A | validation | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则 |
| BR-50 | 姓名必须至少包含一个中文或 ASCII 字母，纯空格、纯符号、纯数字均不通过 | A | validation | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则 |
| BR-51 | `id_card` 与业务唯一键中的身份证统一转大写后再比较与入库 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 字段规范化与姓名规则 |
| BR-52 | 城市枚举由配置项 `CITY_LIST` 下发，默认五市：贵阳/成都/长沙/武汉/南京 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 城市列表配置 |
| BR-53 | 城市列表配置为空或加载失败时，`POST /api/partner/register` 返回 503 `CITY_LIST_UNAVAILABLE`，不执行校验与写库 | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 城市列表配置 |
| BR-54 | 同唯一键、同 `idempotency_key` 但请求体不一致时返回 409 `IDEMPOTENCY_KEY_CONFLICT` | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-55 | 请求体一致性按 `name`、`mobile`、`id_card`（大写）、`city`、`agreement_version` 比较，任一字段变化视为不同请求 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-56 | `409 MOBILE_ALREADY_REGISTERED` 只表示手机号已注册，本服务不返回跳转地址，也不实现 P-04 状态查询 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 幂等与重复注册 |
| BR-57 | 提交注册时记录 `agreement_checked_at`，取服务端接收时间，UTC 精确到秒 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 协议元数据 |
| BR-58 | 提交注册时记录 `agreement_ip`，优先取 `X-Forwarded-For` 首个地址，否则取连接地址 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 协议元数据 |
| BR-59 | 提交注册时记录 `agreement_user_agent`，取 `User-Agent` 请求头，缺失时记空字符串 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 协议元数据 |
| BR-60 | 协议元数据中的 IP 与 UA 不写入业务日志正文 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 协议元数据 |
| BR-61 | 协议版本更新后，已提交且仍为 `pending` 的档案保留提交时的 `agreement_version`，不追溯重签 | A | state_transition | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 三、接口设计 > 3.4 提交注册 > 协议元数据 |
| BR-62 | 并发提交发生唯一约束冲突后按业务唯一键重查；幂等键与请求体一致时返回已存在档案（200） | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 四、数据库设计 > 4.5 事务与并发 |
| BR-63 | 并发冲突重查后请求体不一致时返回 409 `IDEMPOTENCY_KEY_CONFLICT` | A | error_handling | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 四、数据库设计 > 4.5 事务与并发 |
| BR-64 | 协议元数据 `agreement_ip` 与 `agreement_user_agent` 随主档保存，保留期限与主档一致 | A | data_consistency | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 四、数据库设计 > 4.6 数据安全 |
| BR-65 | `/_debug/state` 不返回 `agreement_ip` 与 `agreement_user_agent` | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 四、数据库设计 > 4.6 数据安全 |
| BR-66 | 示例环境（4C8G、本地 SQLite、单进程）注册接口 p95 ≤ 200 ms、短信发送接口 p95 ≤ 100 ms（不含外部通道耗时），以 10 并发、60 秒窗口统计 | A | other | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 六、非功能设计 |
| BR-67 | 生产部署必须在网关层增加鉴权、限流与审计访问控制 | A | business_constraint | 有效 | docs/design.md#合伙人注册服务 · 设计文档 > 七、约束与实现差异 > 7.1 已知约束 |

## 3 冲突（文档内部可判定，不需业务确认）

未检出。若文档已有语义级矛盾却在这里为空，说明抽取没覆盖到，不是文档没问题。

## 4 定义缺口（文档该写未写）

| 编号 | 类型 | 维度 | 缺口 | 原因 | 影响规则 |
|---|---|---|---|---|---|
| GAP-01 | 该写未写 | state_transition | P-04 的 audit_step 2/3/4 状态迁移与审核通过/驳回后的字段变化未定义 | 设计 5.3 声明由 P-04 定义，不在本服务范围 | BR-24 |

## 5 隐含假设（原文没写但规则默认成立）

| 编号 | 假设 | 不成立时的影响 | 涉及规则 |
|---|---|---|---|
| AS-01 | 生产环境身份证加密/散列的算法、密钥管理与轮换策略未定义 | 设计 4.6 只要求示例库之外加密或散列，未给实现约束 | BR-31、BR-32 |
| AS-02 | 协议元数据的审计/运维访问角色与授权方式未定义 | 设计 4.6 只写仅审计/运维后台可读，服务本身无鉴权与角色模型 | BR-64、BR-65 |
| AS-03 | 示例环境图形验证码接受任意非空 token，CAPTCHA_INVALID 分支无法在示例环境触发 | 设计 3.3/7.1 说明示例未接入外部服务，生产契约待 Q-02 确认 | BR-40 |

## 6 业务确认清单

| 编号 | 问题 | 为什么必须问 | 不回答的后果 | 出处 | 阻塞规则 |
|---|---|---|---|---|---|
| Q-01 | 单 IP 最近 60 分钟 20 次在 NAT/共享出口下会误伤同网用户，是否接受？是否需要白名单或按设备限流？ | 需要业务决策与外部网络事实 | 影响频控策略与边界用例 | docs/design.md#七、约束与实现差异 > 7.3 待业务确认 | BR-07 |
| Q-02 | 图形验证码 token 的签发方、有效期、失败上限、错误码与重放策略是什么？示例环境接受任意非空 token，生产如何联调？ | 依赖外部图形验证码服务的事实 | CAPTCHA_INVALID 等分支在示例环境不可达 | docs/design.md#七、约束与实现差异 > 7.3 待业务确认 | BR-40、BR-41 |
| Q-03 | 同一 idempotency_key 携带不同请求体时，是否接受默认的 409 IDEMPOTENCY_KEY_CONFLICT，还是改为返回首次结果或覆盖？ | 属于业务策略，文档先给默认实现 | 影响幂等与重试用例的预期结果 | docs/design.md#七、约束与实现差异 > 7.3 待业务确认 | BR-54、BR-55 |
| D-01 | 频控窗口是否维持最近 24 小时/最近 60 分钟滚动窗口，还是改为自然日/自然小时？若改，采用哪个时区？ | 涉及业务口径与时区选择 | 影响频控边界用例与跨窗口重置断言 | docs/design.md#七、约束与实现差异 > 7.3 待业务确认 | BR-06、BR-07、BR-47 |
| D-02 | 生产环境注册/短信接口的响应时间、并发量与压测环境指标是多少？ | 示例基线不能替代生产 SLA | 影响性能用例的判定基准 | docs/design.md#七、约束与实现差异 > 7.3 待业务确认 | BR-66 |

## 7 枚举未闭合项（`in/not_in` 未声明 exhaustive）

无。所有 `in/not_in` 约束都显式声明了 `exhaustive`。
