# 最小示例：合伙人注册（P-03）

用于验证 AstSight 的「需求 → 规则 → 用例 → pytest」链路。被测服务只实现
《合伙人注册需求文档》P-03 的短信与注册接口，不包含 P-04 审核状态和 P-05 工作台。

服务设计（模块划分、接口、数据库与实现差异）见 [docs/design.md](docs/design.md)。

## 启动

```bash
cd engine/examples/partner_onboarding
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8030
```

- OpenAPI：`http://127.0.0.1:8030/openapi.json`
- 健康检查：`GET /health`
- 重置数据：`POST /_reset`（未写入 OpenAPI，生成用例的 conftest 逐条调用）
- 调试状态：`GET /_debug/state`（未写入 OpenAPI，查看已注册档案与审核日志）

静态契约 `openapi.json` 与实现保持一致，供 `rules prepare` / `cases prepare`
在服务未启动时离线分析。改接口后重新生成：

```bash
python - <<'PY'
import json
from app import app
open("openapi.json", "w", encoding="utf-8").write(
    json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"
)
PY
```

## 示例约定（设计 V1.1）

- 固定短信验证码 `123456`；按手机号签发，5 分钟有效，错误累计 5 次后作废
  （`SMS_CODE_ATTEMPTS_EXCEEDED`），重新发送会重置为新的有效验证码。
  未配置 `SMS_GATEWAY_URL` 时按示例模拟通道返回成功；配置后真实调用网关，
  通道失败返回 503 `SMS_CHANNEL_UNAVAILABLE`。
- 频控按请求次数计数：同号 60 秒冷却、每日 10 次、单 IP 每小时 20 次；
  第 3 次起要求 `captcha_token`（示例服务接受任意非空值）。
- 城市列表由 `CITY_LIST` 环境变量（JSON 数组）下发；未设置时用内置枚举，
  设置但为空/非法时注册返回 503 `CITY_LIST_UNAVAILABLE`。
- 成功注册返回 `partner_id`、`status=pending`、`audit_status=pending`、`audit_step=1`。
- 以「手机号 + 身份证」为业务唯一键；同一 `idempotency_key` 且请求体一致
  （`name/mobile/id_card`（不区分大小写）`/city/agreement_version`）时返回同一个
  `partner_id`，同键不同体返回 409 `IDEMPOTENCY_KEY_CONFLICT`，不同键重复注册返回
  409 `DUPLICATE_REGISTRATION`。
- 身份证统一按大写存储；姓名按设计字符集（中文、英文字母、半角空格、`·`）校验，
  不裁剪首尾空格，且至少包含一个字母或中文。
- 协议勾选元数据（`agreement_checked_at`、`agreement_ip`、`agreement_user_agent`）
  落库；IP 取 `X-Forwarded-For` 第一段，缺省用连接对端地址。
- 身份证占用返回通用提示，避免枚举；唯一约束并发冲突后按业务唯一键重查。

## 模拟下游调用

`downstreams.py` 定义了三个占位下游（明文 HTTP，失败只返回 `False`），用于演示
C3 从路由 handler 识别下游依赖：

| 触发接口 | 下游 | 调用 | 环境变量 |
|---|---|---|---|
| `POST /api/partner/sms/send` | 短信网关 | `POST /v1/messages` | `SMS_GATEWAY_URL` |
| `POST /api/partner/register` | 实名认证 | `POST /v1/verify` | `ID_VERIFY_URL` |
| `POST /api/partner/register` | 合伙人核心服务 | `POST /internal/partners` | `PARTNER_CORE_URL` |

三个下游都是**配置后才会调用**：`SMS_GATEWAY_URL` 未配置时模拟通道直接返回成功；
实名认证与核心服务的响应语义需求/设计未定义，未配置即跳过（注册结果不受影响）。
跑 Mock 用例时只把 `SMS_GATEWAY_URL` 指向 Mock 的 `sms/send` 路由即可。

在 `engine/` 下重新生成画像和 Mock 计划：

```bash
PYTHONPATH=. python -m astsight run \
  --project examples/partner_onboarding --mode full \
  --openapi examples/partner_onboarding/openapi.json --skip-execute

PYTHONPATH=. python -m astsight mock plan \
  --project examples/partner_onboarding \
  --openapi examples/partner_onboarding/openapi.json
```

预期 `profiles.json` 的 `downstream_deps` 共 3 条，`mock-plan.json` 的 `routes`
同样 3 条；分支与注入原值需按实际下游人工补全。

## 数据库

注册数据落在 SQLite，频控窗口仍在内存：

- `schema.sql`：`partner`、`partner_audit_log`、`partner_seq` 三张表；
- `db.py`：连接/事务与写入函数，`insert_partner`、`insert_audit_log` 是
  delta 演示里的「DB 插入变更」落点；
- 默认库文件 `partner.db`（已 gitignore），可用 `PARTNER_DB` 指定其它路径；
- `POST /_reset` 会清表并重置序号，生成用例仍可逐条隔离。

DDL 可直接作为 C5a 输入：

```bash
cd engine
PYTHONPATH=. python -m astsight data catalog \
  --project examples/partner_onboarding \
  --ddl examples/partner_onboarding/schema.sql
```

### 验证「DB 插入变更 → API」感知

`insert_partner` 不在路由层，只有代码图谱能把它回溯到注册接口。把示例复制成
临时 git 仓库，在 feature 分支只改 `db.py` 里 `insert_partner` 的一行
（例如 `name` → `name.strip()`），然后：

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/repo --mode delta --base main --target feature \
  --base-url http://127.0.0.1:8030 \
  --reset-url http://127.0.0.1:8030/_reset \
  --openapi /path/to/repo/openapi.json \
  --codegraph on
```

预期 `changed_apis.json` 里是：

```json
{
  "handler": "POST /api/partner/register",
  "confidence": "medium",
  "evidence": "inferred",
  "attribution": "call_graph",
  "changed_symbols": ["db.insert_partner"],
  "trace": ["insert_partner", "_register_in_transaction", "register", "POST /api/partner/register"]
}
```

`medium/inferred` 是调用链推断，不是方法级直接命中；不要把它当成 `verified`。
改 `schema.sql` 这类 `.sql` 文件时，CodeGraph 不产出符号，会进
`unsupported_files` 并让门禁 fail-closed；这类变更需要显式处理或
`--allow-unmapped`，不能假装已覆盖。

## 跑规则链路

完整参数、两段式流程与排错见
[C1 规则生成使用文档](../../../doc/C1-规则生成使用文档.md)。

在 `engine/` 下，一条命令完成 C1（准备上下文 → A1 抽规则 → 校验 → 写库）：

```bash
PYTHONPATH=. python -m astsight rules c1 \
  --project examples/partner_onboarding \
  --requirements examples/partner_onboarding/docs/requirements.md \
  --design examples/partner_onboarding/docs/design.md \
  --openapi examples/partner_onboarding/openapi.json
```

- `--requirements` 至少一个；`--design` 可选、可重复；`--openapi` 可选，不传则不生成 CR 契约规则。
- `rules c1` 是 `rules extract` 的别名；模型通过环境变量配置（见 `engine/README.md`）。
- 产物：`<project>/ruleset.json` + `<project>/ruleset.md`，上下文与草案在
  `<project>/.astsight/rules/<context_id>/`。
- 加 `--dry-run` 只校验、不写规则库。

需求文档提供业务约束，设计文档提供接口、错误码、事务与数据模型约束；两份都作为
A1 的输入。当前 `ruleset.json` 为 v3（`rs-1be1cd3c8400d052`）：67 条业务规则
（BR）+ 10 条来自 OpenAPI 的契约规则（CR），0 冲突 / 4 缺口 / 5 待确认。
待确认项 Q-01/Q-02/Q-03 与 D-01/D-02 涉及产品口径、与接口具体显现无关，按项目
约定保留为信息项，不阻塞用例。前端步骤控制、P-04 跳转等服务端不实现的内容以
缺口形式保留，不进入可执行规则。

如果模型在框架外运行，改用两段式：

```bash
PYTHONPATH=. python -m astsight rules prepare \
  --project examples/partner_onboarding \
  --requirements examples/partner_onboarding/docs/requirements.md \
  --design examples/partner_onboarding/docs/design.md \
  --openapi examples/partner_onboarding/openapi.json
```

按输出的 `context.json` / `prompt.md` 写 `rules.draft.json` 后导入：

```bash
PYTHONPATH=. python -m astsight rules import \
  --project examples/partner_onboarding \
  --context examples/partner_onboarding/.astsight/rules/<context_id> \
  --draft examples/partner_onboarding/.astsight/rules/<context_id>/rules.draft.json
```

生成用例上下文并调用 A4（`--data-catalog` 把可造状态一并打包；有下游 Mock 语义时
再加 `--mock-semantics`）。造数与下游分支都是两段式：确定性骨架 + 可选的模型语义层。

```bash
# 1) C5a 骨架：表/列/FK/拓扑
PYTHONPATH=. python -m astsight data catalog \
  --project examples/partner_onboarding \
  --ddl examples/partner_onboarding/schema.sql \
  --out .astsight/data

# 2) 字段业务语义与候选业务态（本示例用框架外模型产物 data_semantics.draft.json；
#    也可以加 --llm 直接调用模型）
PYTHONPATH=. python -m astsight data semantics \
  --project examples/partner_onboarding \
  --catalog .astsight/data/data_catalog.json \
  --draft examples/partner_onboarding/data_semantics.draft.json \
  --ruleset examples/partner_onboarding/ruleset.json

# 3) 首次上下文：用于从 C3 下游 + 规则生成 Mock 语义
PYTHONPATH=. python -m astsight cases prepare \
  --project examples/partner_onboarding \
  --requirements examples/partner_onboarding/docs/requirements.md \
  --openapi examples/partner_onboarding/openapi.json \
  --data-catalog examples/partner_onboarding/.astsight/data/data_catalog.json

# 4) C3.5.1 Mock 语义：契约 success + 规则映射分支
PYTHONPATH=. python -m astsight mock semantics \
  --project examples/partner_onboarding \
  --context examples/partner_onboarding/.astsight/casegen/<context_id> \
  --ruleset examples/partner_onboarding/ruleset.json \
  --draft examples/partner_onboarding/mock_semantics.draft.json

# 5) 重新打包上下文，A4 才能同时校验 state_key 与 mock_bindings
PYTHONPATH=. python -m astsight cases prepare \
  --project examples/partner_onboarding \
  --requirements examples/partner_onboarding/docs/requirements.md \
  --openapi examples/partner_onboarding/openapi.json \
  --data-catalog examples/partner_onboarding/.astsight/data/data_catalog.json \
  --mock-semantics examples/partner_onboarding/.astsight/mock_semantics.json
```

本示例产出的 `data_catalog.json` 有 21 条字段语义、2 个可执行状态
（`state_partner_registered`、`state_partner_id_card_occupied`）和 6 个缺口；
`mock_semantics.json` 有 3 个下游、5 个分支（每个下游都有 `success`，短信网关
另有 `rule_BR-42` 的失败分支）和 1 个缺口。注意 `partner-core` 上的
`rule_BR-53`（`CITY_LIST_UNAVAILABLE`）是已知误映射，当前无用例绑定，下次重新生成
Mock 语义时需要修正。`mock_semantics.json` 内记录的 `context_id` 是生成时的
来源上下文；重新打包后以新的 `casegen/<context_id>/context.json` 为准。

A4 草案先 `cases import --dry-run` 校验，再导入为待审 proposal：

```bash
PYTHONPATH=. python -m astsight cases import \
  --project examples/partner_onboarding \
  --context examples/partner_onboarding/.astsight/casegen/<context_id> \
  --draft examples/partner_onboarding/cases.draft.json \
  --ruleset examples/partner_onboarding/ruleset.json \
  --require-rules
```

当前示例的用例库为 `caseset.json` v4：38 条已批准用例（sms 9 / register 29），
由 `prop-9c638f7d5dba6e19`（16 new / 16 update / 6 unchanged，0 error / 0 warning）
经人工逐条 `approve` 建立，随后又有两条人工批准的修正：
`prop-610371d8b1981ac7`（TC-0029 补短信网关 `success` Mock 绑定）与
`prop-44d9f59e45cdf922`（TC-0021 移除实名认证/核心服务的未使用绑定）。
生成这些用例的上下文是 `.astsight/casegen/9bc8d1ccd52f00f89015d331ac6a6e9fd772791808346ad717d2a3a11f324a5c`
（2 份文档、3 个端点，打包 catalog 与 Mock 语义）。
旧提案 `prop-d30172d8dcd6ca3a`、`prop-4dee9aa420157005`、`prop-a9b6f61c027f3e02`、
`prop-02bc6b8d2997fda5` 与 `prop-f31bb79d8dca21a4` 已被取代，仍以 `pending` 留在
`.astsight/cases/proposals/`，运行时会产生 degraded 警告；不要批准。

其中 7 条是 `steps` 多请求用例，用连续请求覆盖原先无法构造的频控与状态规则：

| 用例 | 步骤 | 覆盖规则 |
|---|---|---|
| `sms_send_same_mobile_within_cooldown` | 2 | BR-05：同号 60 秒内第二次发送 429 `SMS_COOLDOWN` |
| `sms_captcha_required_from_third_request` | 3 | BR-08/09/10：第 2 次失败也计数，第 3 次要求验证码 |
| `sms_daily_limit_after_ten_requests` | 11 | BR-06/10：第 11 次请求 429 `SMS_DAILY_LIMIT` |
| `sms_ip_hourly_limit_after_twenty_requests` | 21 | BR-07：同 IP 第 21 次请求 429 `SMS_IP_LIMIT` |
| `sms_code_attempts_exceeded` | 7 | BR-37/38：发送 + 5 次错误码后验证码作废 |
| `register_id_card_lowercase_normalized` | 2 | BR-51/18/19：小写身份证注册后按大写归一 |
| `register_idempotency_key_conflict` | 2 | BR-54/55/19：同幂等键不同请求体 409 |

这些用例已编译为 pytest 并对本示例服务实际执行，38/38 通过；逐步请求共享同一个
`client`，每条用例前仍由 `/_reset` 重置内存状态。

审核并批准（必须由人完成）：

```bash
PYTHONPATH=. python -m astsight cases approve \
  --project examples/partner_onboarding \
  --proposal examples/partner_onboarding/.astsight/cases/proposals/<proposal_id> \
  --reviewer <your-name>
```

启动服务后执行已批准用例。38 条中有 1 条（`sms_channel_unavailable`）需要短信网关
返回 503 的 `rule_BR-42` 分支，必须由下游 Mock 提供；只跑其余用例可以不配 Mock：

```bash
cd engine/examples/partner_onboarding
python -m uvicorn app:app --host 127.0.0.1 --port 8030
# 另一个终端，在 engine/ 下：
PYTHONPATH=. python -m astsight run \
  --project examples/partner_onboarding \
  --mode cases \
  --base-url http://127.0.0.1:8030 \
  --reset-url http://127.0.0.1:8030/_reset \
  --data-plan examples/partner_onboarding/.astsight/data/data_plan.json \
  --db-url sqlite:///examples/partner_onboarding/partner.db
```

带 Mock 跑全量：`.astsight/mock-plan.json` 只是路由骨架（`branches` 为空），需要把
`.astsight/mock_semantics.json` 的分支合并进去（`agent run` 会自动完成；手工执行时用
`build_mock_plan`），起内嵌 Mock 后把 `SMS_GATEWAY_URL` 指向短信路由
`/_dep/re957cffc768e` 再启动服务。实名认证与核心服务不要指向 Mock——示例默认不调用，
指向 Mock 反而会产生 unbound 呼叫。2026-11-11 的全量执行结果：38/38 通过，门禁 PASS，
Mock 26 次调用全部 `ok`。

`--reset-url` 要写完整 URL：Git Bash 会把以 `/` 开头的参数改写成 Windows 路径。
该参数让生成的 `client` fixture 在每条用例前调用示例服务的隐藏重置接口，保证有状态
接口可重复执行。只想生成 pytest 代码、不连接服务时，加 `--skip-execute`。

## 边界

- C5b 造数计划覆盖 4 条带 `preconditions` 的用例（TC-0006/0008/0012/0013），
  本次执行 0 blocked；新增前置状态用例仍需重新 `data plan`，无计划的用例继续标
  blocked，不会伪造状态。
- 示例用的是单连接 SQLite + 事务，不覆盖真实数据库的迁移、连接池与并发隔离；
  「唯一约束冲突后重查」只能靠同进程顺序请求近似，无法构造真实并发。
- 健康检查没有可引用的业务规则，当前 A4 草案不生成 `GET /health` 的 business
  用例，避免把无关规则（如 BR-01）挂到契约冒烟上。
- 幂等重试、重复注册与身份证占用通过 `data_catalog.states` 构造前置行；频控窗口
  保存在进程内存，`table_values` 无法构造历史请求，因此冷却/每日/IP 上限和第三次
  验证码改用 `steps` 连续请求覆盖（见上表），不再写入 `uncovered_requirements`。
- `steps` 只支持单用例内串行；跨用例捕获、并发、真实时钟等待仍不支持。时间窗口
  类规则（验证码 5 分钟过期、等待 60 秒后的频控恢复、24h/60min 滚动窗口重置）只验证了
  连续请求立即命中的一侧，不等价于计时后的恢复行为。
- 图形验证码由外部服务签发，示例接受任意非空 token，`CAPTCHA_INVALID` 分支不可达；
  `CITY_LIST` 在进程启动时读取，画像内无法切换为空/失败配置；协议元数据只落库，
  调试接口不返回，均未形成可执行用例。
- Mock 语义 3 个下游 / 5 个分支：短信网关有 `success` + `rule_BR-42`（通道不可用），
  实名认证与核心服务只有契约 `success`，但两者响应语义未定义、示例默认不调用；
  `partner-core` 上的 `rule_BR-53`（`CITY_LIST_UNAVAILABLE`）是已知误映射，无用例绑定。
  这些缺口需回到 A1/语义层修正，不在 Mock 层猜响应。
- 这个目录不是独立 git 仓库；`delta` 模式需要仓库根，请使用临时仓库或复制到自己的仓库。
