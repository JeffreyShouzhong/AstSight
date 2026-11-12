# AstSight 项目进度

> 最后更新：2026-11-12
> 分支：`main`
> 当前阶段：C6/C7 完成（`caseset.json` v4，38 条已批准用例 38/38 通过，门禁 PASS）

## 当前状态

| 阶段 | 状态 | 产物 / 证据 |
|---|---|---|
| C1 规则抽取 | ✅ v3 | `engine/examples/partner_onboarding/ruleset.json`：67 条 BR + 10 条 CR；0 冲突 / 4 缺口 / 5 待确认 |
| 路由清单 | ✅ | 5 条，全部 `graph_repaired` / `verified` |
| C3 接口画像 | ✅ V1.1 | `.astsight/runs/20261111-183042-853-full/profiles.json`：5 个接口（含 `/_reset`、`/_debug/state`），3 条下游依赖，响应码含 503；`--skip-execute --allow-unmapped` 只生成不执行 |
| 下游模拟 | ✅ | `downstreams.py`：短信网关 / 实名认证 / 核心服务 3 个调用点；Mock 计划 3 路由 / 3 注入 |
| C5a 数据目录 | ✅ 已补语义 | `.astsight/data/data_catalog.json`：3 表、21 条字段语义、**2 个可执行状态**、6 缺口 / 1 待确认 |
| C3.5.1 Mock 语义 | ✅ 新增 | `.astsight/mock_semantics.json`：3 deps / 3 `success` 分支 / 4 缺口；失败分支因无规则依据留缺口 |
| A4 上下文 | ✅ v3 | `.astsight/casegen/9bc8d1cc…`：2 docs、3 endpoints；使用 v3 ruleset + 新 mock semantics |
| A4 用例草案 | ✅ v3 | `cases.draft.json`：38 条用例（register 29 / sms 9）；7 条 `steps` 多请求用例；27 条 BR 明确未覆盖 |
| 导入待审 | ✅ 已生成 | 提案 `prop-9c638f7d5dba6e19`：16 new / 16 update / 6 unchanged；0 error / 0 warning / 0 unaccounted |
| 多请求模型 | ✅ 已落地 | `steps[]` + 跨步骤 `$ref`；校验/指纹/codegen/执行生成全链路 |
| 人工审核 / 批准 | ✅ 2026-11-11 | 32 条全部 approve；随后 2 条修正（TC-0029 增加短信 Mock 绑定、TC-0021 移除未使用绑定）逐条 approve；reviewer `JeffreyShouzhong2`；`caseset.json` v4，38 approved / 0 deprecated |
| 规则漂移 | ✅ 已清零 | `rules status`：`rule_drift_stale_cases` 0；BR 覆盖 40/67（59.7%） |
| 脚本生成 | ✅ 已生成 | `.astsight/runs/20261111-155348-795-cases/generated/`：2 个接口测试文件 + `conftest.py`；38 tests collected；codegen 0 error |
| C5b 造数计划 | ✅ 已生成 | `.astsight/data/data_plan.json`：4 个 plan（TC-0006/0008/0012/0013）；0 rejected / 0 warning |
| 示例服务 V1.1 | ✅ 已实现 | `app.py` / `schema.sql` / `db.py` / `downstreams.py` / `openapi.json`：验证码时效与 5 次作废、短信 503、CITY_LIST 503、协议元数据、幂等请求体比较、并发重查、姓名/身份证规范化 |
| C6/C7 执行 | ✅ 2026-11-11 | 运行 `20261111-155348-795-cases`：38/38 通过（100%），门禁 PASS；Mock 26 次调用全部 ok，0 unbound |
| 模型配置文件 | ✅ 新增 | `engine/llm.example.json` 模板；`llm.json` 自动发现（cwd → `~/.astsight/llm.json`）或 `--llm-config` / `ASTSIGHT_LLM_CONFIG`；环境变量覆盖文件 |
| 用户文档 | ✅ 已整理 | 根 `README.md` 重写为面向使用者的当前版本说明；新增《快速上手指南》；删除 4 份历史调研文档，保留设计/架构/决策文档给维护者 |
| 造数方言 | ✅ PostgreSQL/MySQL | `data/dialects.py`；SQL 占位符/标识符按方言渲染；`psycopg` / `PyMySQL` 驱动按需导入；计划方言与 `--db-url` 不一致时 `dialect_mismatch`；`data catalog --dialect`、`agent run --db-dialect` |

## 本轮完成（示例服务 V1.1 与 C6/C7 执行）

1. **示例服务升级到设计 V1.1**：
   - `app.py`：验证码按手机号签发，5 分钟有效、累计 5 次失败作废、重发重置；
     短信通道失败返回 503 `SMS_CHANNEL_UNAVAILABLE`；`CITY_LIST` 配置为空/非法时
     返回 503 `CITY_LIST_UNAVAILABLE`；幂等重试比较
     `name/mobile/id_card(大写)/city/agreement_version`，不一致返回
     409 `IDEMPOTENCY_KEY_CONFLICT`；身份证统一大写；姓名按设计字符集与长度校验；
     记录 `agreement_checked_at` / `agreement_ip` / `agreement_user_agent`；
     唯一约束冲突后按业务唯一键重查。
   - `schema.sql` / `db.py`：`partner` 新增 3 个协议元数据列；`init_db` 对旧库做
     幂等补列；`insert_partner` 写入元数据；`/_debug/state` 不返回 IP/UA。
   - `downstreams.py`：`SMS_GATEWAY_URL` 未配置时按示例模拟通道返回成功，配置后
     真实调用；实名认证/核心服务需求未定义响应语义，默认不调用，显式配置才发起。
   - `openapi.json`：重新生成，补充 503 响应与版本 1.1.0。
2. **用例修正（人工批准）**：
   - `prop-610371d8b1981ac7`：TC-0029 的步骤会调用 `sms/send`，补充短信网关
     `success` Mock 绑定；
   - `prop-44d9f59e45cdf922`：TC-0021 移除实名认证/核心服务的未使用 Mock 绑定。
   两条均逐项 `approve`，reviewer `JeffreyShouzhong2`；`caseset.json` 升至 v4。
3. **执行**：以外部内嵌 Mock（3 路由，短信为 `success` + `rule_BR-42`）启动示例
   服务，执行 `run --mode cases`：
   - 38 生成 / 38 执行 / 38 通过（100%），门禁 **PASS**；
   - Mock 26 次调用全部 `ok`，0 阻断；4 个数据计划全部应用；
   - 运行目录 `.astsight/runs/20261111-155348-795-cases/`。
4. **残留 warning（不阻塞）**：4 个旧待审 proposal、OpenAPI 快照变化（新增 503
   响应）、27 条 BR 未覆盖。

## 上一轮完成（按 v3 规则重建 A4）

1. **上下文重建**：用 V1.1 `requirements.md` + `design.md`、`openapi.json`、数据目录和
   新 Mock 语义生成 `.astsight/casegen/9bc8d1cc…`（2 docs / 3 endpoints / 0 warning）。
2. **草案重建**：`cases.draft.json` 从旧 31 条扩展为 38 条；旧用例按 `rule_id` 重映射
   到 v3 `rule_key`，并修正多处因设计 V1.1 改写而失效的 `requirement_refs` 原文引用。
3. **新增 7 条用例**：
   - `sms_channel_unavailable`（BR-42，Mock `rule_BR-42` → 503）；
   - `sms_code_attempts_exceeded`（BR-37/38，7 步：发送 + 5 次错误 + 正确码）；
   - `register_name_outer_spaces_allowed`（BR-48/02）；
   - `register_name_fullwidth_rejected`（BR-49/02）；
   - `register_name_spaces_only`（BR-50/02）；
   - `register_id_card_lowercase_normalized`（BR-51/18/19，2 步大小写归一）；
   - `register_idempotency_key_conflict`（BR-54/55/19，2 步同键不同体）。
4. **断言增强**：短信成功用例增加 `remaining_today=9`、`captcha_required=false`，
   覆盖 BR-45/46 的首次请求公式。
5. **覆盖声明**：40/67 条 BR 被草案引用（59.7%）；27 条不可观测/依赖时钟/并发/配置的
   BR 写入 `uncovered_requirements`，没有静默丢弃。
6. **导入结果**：提案 `prop-9c638f7d5dba6e19`，16 new / 16 update / 6 unchanged，
   0 error / 0 warning / 0 unaccounted；32 条待审项全部 decision=`pending`，未批准。

## 前一轮完成（多请求用例模型）

1. **契约扩展**：`TestCase` / `DraftCase` 新增 `steps[]`（`CaseStep`），每步有独立
   endpoint、path_params、query、headers、body、asserts；顶层请求/断言必须为空，
   顶层 endpoint 必须出现在步骤中。
2. **跨步骤引用**：`{"$ref": "<step>.<field>"}` 只允许引用更早步骤，可用于 body/
   query/path_params/断言 expect，支持嵌套字段；header ref 明确拒绝。
3. **校验**：步骤名 `^[a-z][a-z0-9_]{1,63}$` 且用例内唯一；最多 50 步/单步 50 断言；
   每步至少一条 `status_code`；每步按自己的接口画像校验参数、字段与错误码。
4. **指纹兼容**：`steps` 为空时不进入 fingerprint，既有单请求用例不会因模型扩展重审。
5. **代码生成**：顺序生成 `resp_<step>` / `body_<step>`，新增 `step_tools.refs()` 与
   `step_tools.path()`；整个用例共享 `client`、Mock 会话与前置状态。
6. **提示词/文档**：A4 提示词移除“执行器只发一次请求”的旧约束，补充 `steps` 规则与
   可计数请求示例；`engine/README.md`、使用文档、示例 README 同步。
7. **示例用例**：合伙人草案新增 4 条频控多请求用例（2/3/11/21 步），覆盖 BR-05/06/
   07/08/09/10；导入待审提案 `prop-d30172d8dcd6ca3a`（31 new / 0 error / 18 warning）。
8. **验证**：`engine/tests` 全量 267 passed；4 条频控用例编译后对示例服务执行 4/4
   通过（冷却、第三次验证码、每日 11 次、单 IP 21 次）。

## 关键产物

- 规则库 v3：`ruleset_id=rs-1be1cd3c8400d052`，hash `0ff93539…`；67 BR / 10 CR，
  0 冲突 / 4 缺口 / 5 待确认
- 规则上下文（v3）：`.astsight/rules/76b0d382a671f10807059953c5a703d523424cc81c35cbd60cd00a7ac5c2af94/`
- 数据目录：`.astsight/data/data_catalog.json`（catalog hash `9628a7c1…`，3 表 / 2 状态；
  尚未包含协议元数据新列）
- Mock 语义：`.astsight/mock_semantics.json`（3 deps / 5 branches / 1 gap；
  短信 `rule_BR-42` 分支可用于 503）
- A4 上下文（v3）：`.astsight/casegen/9bc8d1ccd52f00f89015d331ac6a6e9fd772791808346ad717d2a3a11f324a5c/`
- A4 草案（v3）：`engine/examples/partner_onboarding/cases.draft.json`（38 cases / 31 uncovered；
  其中 7 条多请求用例）
- 用户快速上手：`doc/AstSight-快速上手指南.md`（demo → 模型配置 → 三条使用路线 →
  产物位置 → 常见报错）
- V1.1 接口画像：`.astsight/runs/20261111-183042-853-full/profiles.json` + `routes.json`
  （5 接口 / 3 下游；`/_reset`、`/_debug/state` 不在 OpenAPI，需 `--allow-unmapped` 生成）
- 模型配置模板：`engine/llm.example.json`（`schema_version` / `provider` / `model` /
  `base_url` / `auth_style` / `api_key` / `api_key_env` / 调用参数）；`engine/llm.json`
  已 gitignore，配置文件按 `--llm-config` → `ASTSIGHT_LLM_CONFIG` → `./llm.json` →
  `~/.astsight/llm.json` 查找，环境变量 `ASTSIGHT_LLM_*` 覆盖文件值
- 已批准用例库：`engine/examples/partner_onboarding/caseset.json` v4
  （store hash `164b2593…`；38 approved；批准人 `JeffreyShouzhong2`，2026-11-11）
- 已批准文本版（按接口）：`engine/examples/partner_onboarding/cases.approved.md`
  （TC-0001～TC-0038，按 endpoint 分组：sms 9 / register 29；`GET /health` 缺口单列；
  已同步 v4 的 TC-0021 / TC-0029 Mock 绑定变化）
- 草案文本版（按接口，保留作设计记录）：`engine/examples/partner_onboarding/cases.draft.md`
- 已批准用例脑图：`engine/examples/partner_onboarding/cases.approved.mindmap.{md,svg,png}`
  —— 仍是 22 条旧快照（2026-11-09），未随 v4 重新生成，阅读以 `cases.approved.md` 为准
- 已批准提案：`.astsight/cases/proposals/prop-9c638f7d5dba6e19/`（16 new / 16 update / 6 unchanged）、
  `prop-610371d8b1981ac7/`（TC-0029 1 update）、`prop-44d9f59e45cdf922/`（TC-0021 1 update）
- C5b 造数计划：`engine/examples/partner_onboarding/.astsight/data/data_plan.json`
  （4 plans / 0 rejected；TC-0006、TC-0008、TC-0012、TC-0013）
- C6 生成脚本：`.astsight/runs/20261111-155348-795-cases/generated/`
  （`test_post__api_partner_sms_send.py` 9 条、`test_post__api_partner_register.py` 29 条、
  `conftest.py` + `data_plan.json`；`pytest --collect-only` 收集 38 条）
- C6/C7 执行报告：`.astsight/runs/20261111-155348-795-cases/report.md`
  （38/38 通过，门禁 PASS；Mock 26 calls / 0 blocking）
- 旧 A4 上下文（已取代）：`4ff385a6…`、`2b990b11…`、`d0a6b6f2…`、`03d17b69…`
- 旧提案（勿批准）：`prop-d30172d8dcd6ca3a`、`prop-4dee9aa420157005`、
  `prop-a9b6f61c027f3e02`、`prop-02bc6b8d2997fda5`、`prop-f31bb79d8dca21a4`

## 阻塞项

1. **27 条 BR 明确未覆盖**：BR-01、BR-26/27、BR-31/32、BR-34～36、BR-39～41、
   BR-43/44、BR-47、BR-52/53、BR-57～67。原因是运行时时钟/并发/配置不可控、
   协议元数据与日志不在画像、调试接口不在 OpenAPI、性能需压测环境。
2. **频控恢复语义仍不可测**：`steps` 只能验证连续请求的即时 429/400；等待 60 秒后的
   恢复、24h/60min 滚动窗口重置仍依赖时钟注入或可重置计数接口。
3. **CAPTCHA_INVALID 分支不可达**：图形验证码由外部服务签发，示例接受任意非空 token；
   BR-40/41 未形成可执行用例。
4. **Mock 语义存在一条误映射**：`mock_semantics.json` 把 BR-53（`CITY_LIST_UNAVAILABLE`）
   误挂到 partner-core 下游；当前用例未绑定该分支，但应在下次生成 Mock 语义时修正。
5. **存在 4 个旧待审 proposal**：`prop-d30172d8dcd6ca3a` 等旧提案运行时会产生
   degraded 警告；新提案 `prop-9c638f7d5dba6e19`、`prop-610371d8b1981ac7`、
   `prop-44d9f59e45cdf922` 已批准。
6. **OpenAPI 快照漂移警告**：本次为 V1.1 补充了 503 响应并重新生成 `openapi.json`，
   已批准用例仍基于旧快照；运行报告提示 “OpenAPI 契约已变化”，属预期 warning。
7. **未提交**：工作区有大量修改，`engine/examples/partner_onboarding/` 部分仍为 untracked。

## 下一步

已完成（本轮）：

```bash
cd engine

# 1. 用 V1.1 文档重建 A4 上下文
PYTHONPATH=. python -m astsight cases prepare \
  --project examples/partner_onboarding \
  --requirements examples/partner_onboarding/docs \
  --openapi examples/partner_onboarding/openapi.json \
  --data-catalog examples/partner_onboarding/.astsight/data/data_catalog.json \
  --mock-semantics examples/partner_onboarding/.astsight/mock_semantics.json

# 2. 按 prompts/a4_case_design.md 重写 cases.draft.json 并导入待审
PYTHONPATH=. python -m astsight cases import \
  --project examples/partner_onboarding \
  --context examples/partner_onboarding/.astsight/casegen/9bc8d1ccd52f00f89015d331ac6a6e9fd772791808346ad717d2a3a11f324a5c \
  --draft examples/partner_onboarding/cases.draft.json \
  --ruleset examples/partner_onboarding/ruleset.json \
  --data-catalog examples/partner_onboarding/.astsight/data/data_catalog.json \
  --mock-semantics examples/partner_onboarding/.astsight/mock_semantics.json
```

已完成（人工审核与批准）：

```bash
# 3. 32 条待审项全部由人决定为 approve
PYTHONPATH=. python -m astsight cases review \
  --proposal examples/partner_onboarding/.astsight/cases/proposals/prop-9c638f7d5dba6e19 \
  --pending

# 4. 人工提供 reviewer 后批准；结果 caseset v2 / 38 approved
PYTHONPATH=. python -m astsight cases approve \
  --project examples/partner_onboarding \
  --proposal examples/partner_onboarding/.astsight/cases/proposals/prop-9c638f7d5dba6e19 \
  --reviewer JeffreyShouzhong2
```

审核决定由人给出：`approve` 32 / `reject` 0；未自动批准。

已完成（C5b 造数与 C6 脚本生成）：

```bash
# 6. 成功生成 4 个 plan
PYTHONPATH=. python -m astsight data plan --project examples/partner_onboarding \
  --catalog examples/partner_onboarding/.astsight/data/data_catalog.json \
  --cases examples/partner_onboarding/caseset.json \
  --ruleset examples/partner_onboarding/ruleset.json \
  --out .astsight/data

# 7. 只生成不执行；产物在 .astsight/runs/20261111-151821-961-cases/generated/
PYTHONPATH=. python -m astsight run --project examples/partner_onboarding \
  --mode cases --skip-execute \
  --data-plan examples/partner_onboarding/.astsight/data/data_plan.json \
  --out .astsight/runs
```

已完成（V1.1 示例服务 + Mock 执行，2026-11-11）：

```bash
# 8. 启动内嵌 Mock（3 路由：sms success / rule_BR-42、id-verify、partner-core），
#    再以短信网关指向 Mock 的方式启动示例服务：
cd engine/examples/partner_onboarding
SMS_GATEWAY_URL="http://127.0.0.1:36003/_dep/re957cffc768e" \
  python -m uvicorn app:app --host 127.0.0.1 --port 8030

# 9. 执行 38 条用例（--reset-url 必须写完整 URL，Git Bash 会篡改 / 开头的参数）
cd engine
PYTHONPATH=. python -m astsight run --project examples/partner_onboarding \
  --mode cases --base-url http://127.0.0.1:8030 \
  --reset-url http://127.0.0.1:8030/_reset \
  --data-plan examples/partner_onboarding/.astsight/data/data_plan.json \
  --db-url sqlite:///examples/partner_onboarding/partner.db
# 结果：38/38 通过，门禁 PASS（run 20261111-155348-795-cases）
```

注：`examples/partner_onboarding/caseset.json` 已是 v4，38 条 approved；生成脚本和
`data_plan.json` 都基于该版本。旧运行目录 `20261109-180859-840-cases/` 是 22 条
旧快照，不要与 v4 混用。

注：实名认证与核心服务在需求/设计中未定义响应语义，示例服务默认不调用（`ID_VERIFY_URL` /
`PARTNER_CORE_URL` 未配置即跳过）；对应 Mock 分支仍在计划中，但不产生调用，
因此不会出现 unbound 阻断。仅短信网关通过 `SMS_GATEWAY_URL` 指向 Mock。

待决策：
- 频控的时间窗口恢复/并发语义是否需要落库、提供时钟注入或可重置计数接口；
- `/_reset`、`/_debug/state` 是补进 OpenAPI，还是继续从画像范围排除。

说明：v3 规则库中的 Q-01/Q-02/Q-03/D-01/D-02 与产品具体显现无关，按用户意见
不需要回答，不作为 A4/用例重建的阻塞项；规则库中暂保留为信息项。

## 更新记录

- **2026-11-12**：C5 造数扩展到 PostgreSQL/MySQL：
  - 新增 `engine/astsight/data/dialects.py`：URL 解析（含 `postgresql+psycopg://`、
    `mysql+pymysql://` 别名与密码脱敏）、方言归一化、占位符与标识符引用。
  - `render.py` 按 `catalog.dialect` 生成 SQL：SQLite `?`/双引号，MySQL `%s`/反引号，
    PostgreSQL `%s`/双引号；布尔值仅在 SQLite 转 1/0。
  - `DataPlan` 增加 `dialect`；`apply.py` 支持三种连接（stdlib `sqlite3`、`psycopg`、
    `PyMySQL`），保留事务回滚与表存在性检查，计划/数据库方言不一致直接拒绝。
  - 生成物 `conftest.py` 的 `data_state` 支持三种驱动，方言不一致标
    `data_unavailable: data_dialect_mismatch`。
  - CLI：`data catalog --dialect sqlite|postgres|mysql`、
    `agent run --db-dialect`（缺省从 `--db-url` 推断）；`requirements.txt` 增加
    `psycopg[binary]==3.3.8`、`PyMySQL==1.2.4`。
  - 新增 `test_data_dialects.py`（URL/脱敏、三种方言 SQL、DDL、方言拒绝），
    生成物新增方言不匹配用例；新增 `test_data_apply_live.py`（真实库联调，
    未配置环境变量时 SKIP：建临时表 → apply → teardown → drop）。
    全量 **286 passed, 2 skipped**。
  - 文档同步：根 README、使用文档、快速上手指南、engine README、设计/架构文档与
    决策记录（第 14 节）。
  - 真实 MySQL 联调已通过：MySQL 26.7，临时库 `astsight_test`，
    `test_data_apply_live.py` 的 MySQL 用例 **1 passed**（建表 → apply →
    teardown → drop）。本机无 PostgreSQL 实例，对应参数仍为 SKIP。
- **2026-11-12**：清理全仓 TestHub 内部引用，明确 AstSight 与 TestHub 无耦合：
  `engine/README.md` 删除 `testhub-proto/` 历史原型隔离说明；
  `doc/framework-capability-design.md` 的“旁挂对接/交给 TestHub 类平台/
  C7 报告适配器（TestHub REST 推送）”改为通用“测试平台”；
  `doc/CodeGraph-集成设计.md` 删除本机 `D:\testhub_platform\...` 路径；
  `engine/tests/test_codegraph_embedded.py` 删除 `D:/testhub_platform/...`
  的模块根候选，仅保留 `ASTSIGHT_CODEGRAPH_MODULE_ROOT`、仓库内
  `node_modules` 与环境中的 Node。除本记录外，全仓已无 TestHub 字样。
- **2026-11-12**：根 `README.md` 重写为面向使用者的当前版本说明：以“需求/变更 →
  规则 → 审核 → 用例 → 执行”的能力与三条使用路线为主线，补充环境要求、安装、
  `llm.json` 配置、产物位置与当前边界；移除设计基线版本号、命名边界表和按阶段
  展开的设计论证。删除 4 份历史调研文档（`implementation-feasibility.md`、
  `api-test-platform-feasibility.md`、`similar-articles.md`、
  `第三方方案理论依据分析-腾讯云API智能测试平台.md`）；保留
  `framework-capability-design.md`、`agent-architecture.md`、`CodeGraph-集成设计.md`、
  `决策记录.md` 作为维护者文档，并在 README 中单列入口。
- **2026-11-11**：新增面向用户的《AstSight 快速上手指南》
  （`doc/AstSight-快速上手指南.md`）：30 分钟跑 demo、`llm.json`/环境变量配置、
  三条使用路线（`agent run` / 分步 rules→cases→审核→执行 / delta）、产物位置与
  常见报错；README 文档索引与 `engine/README.md` 增加入口。`demo_agent_flow.py`
  改为读取 `llm.json`（`load_config`），与 CLI 行为一致。
- **2026-11-11**：新增模型配置文件支持：`engine/llm.example.json` 模板；
  `load_config()` 按 `--llm-config` / `ASTSIGHT_LLM_CONFIG` → `./llm.json` →
  `~/.astsight/llm.json` 查找，环境变量 `ASTSIGHT_LLM_*` 覆盖文件值；支持
  `api_key_env` 间接读取密钥、未知字段报错、JSON/类型/版本校验。`rules extract`、
  `data semantics --llm`、`mock semantics --llm`、`agent run` 均接受 `--llm-config`；
  `llm.json` 加入 `.gitignore`。新增 7 个配置测试，全量 274 passed。
- **2026-11-11**：示例服务升级到设计 V1.1 并完成 C6/C7 执行：
  `app.py` 实现验证码 5 分钟时效 / 5 次失败作废 / 重发重置、短信通道 503
  `SMS_CHANNEL_UNAVAILABLE`、`CITY_LIST` 配置失败 503、幂等请求体比较
  （409 `IDEMPOTENCY_KEY_CONFLICT`）、身份证大写归一、姓名字符集校验、
  协议元数据落库、唯一冲突后重查；`schema.sql` / `db.py` 新增 3 个协议元数据列并
  对旧库幂等迁移；`downstreams.py` 中实名认证/核心服务改为未配置即不调用；
  `openapi.json` 重新生成（1.1.0 + 503）。人工逐条批准 `prop-610371d8b1981ac7`
  （TC-0029 补短信 Mock 绑定）与 `prop-44d9f59e45cdf922`（TC-0021 移除未使用绑定），
  `caseset.json` 升至 v4（store hash `164b2593…`）。以 Mock 执行 38 条用例：
  **38/38 通过（100%），门禁 PASS**，Mock 26 次调用全部 ok、0 unbound、0 blocked，
  4 个数据计划全部应用；报告见 `.astsight/runs/20261111-155348-795-cases/report.md`。
  报告中的 4 个旧待审 proposal、OpenAPI 快照变化、27 条未覆盖 BR 均为非阻塞 warning。
- **2026-11-11**：按 v3 规则重建 A4 并生成待审 proposal `prop-9c638f7d5dba6e19`：
  上下文 `9bc8d1cc…`，草案 38 条（16 new / 16 update / 6 unchanged），0 error /
  0 warning / 0 unaccounted；新增短信通道 503、验证码 5 次作废、姓名规范化
  （首尾空格/全角/纯空格）、身份证大小写归一、同幂等键不同请求体 409 等 7 条用例；
  40/67 条 BR 被引用，27 条不可观测/依赖时钟/并发/配置的 BR 写入 uncovered。
- **2026-11-11**：人工审核通过并批准 proposal `prop-9c638f7d5dba6e19`：
  32 条待审项全部 `approve`、0 `reject`，reviewer `JeffreyShouzhong2`；
  `caseset.json` 升至 v2（store hash `4f35d558…`），38 approved / 0 deprecated /
  16 history；`rules status` 的 `rule_drift_stale_cases` 清零，BR 覆盖 40/67。
  同步按接口重写 `cases.approved.md`（TC-0001～TC-0038，sms 9 / register 29）。
- **2026-11-11**：生成 C5b 数据计划与 C6 pytest 脚本：
  `data_plan.json` 覆盖 4 条有前置状态用例（TC-0006/0008/0012/0013），0 rejected；
  `run --mode cases --skip-execute` 生成 2 个接口测试文件，codegen 0 error，
  `pytest --collect-only` 收集 38 条；因示例服务未升级 V1.1，尚未真实执行。
- **2026-11-11**：新增按接口组织的文本版用例文档
  `engine/examples/partner_onboarding/cases.draft.md`：按 endpoint 分组 38 条用例
  （sms 9 / register 29），含接口摘要、用例清单、请求/步骤/断言、规则索引与未覆盖规则；
  `GET /health` 无业务规则引用，单列为缺口，未编造 smoke 用例。
- **2026-11-11**：按用户意见，规则库中的 Q-01/Q-02/Q-03/D-01/D-02 与产品具体显现
  无关，不需要回答；不再作为 A4/用例重建的阻塞项，规则库中保留为信息项。
- **2026-11-10**：按设计 V1.1 重新抽取规则并导入 v3：上下文
  `76b0d382…`，67 条 BR + 10 条 CR，0 错误 / 0 warning；新增 38 条（含 5 条替换旧
  口径：BR-06/07/19/22/30），废弃 5 条旧规则，0 冲突 / 4 缺口 / 5 待确认。
  新增覆盖验证码时效与 5 次作废、CAPTCHA_INVALID、短信通道 5 秒超时/503、
  CITY_LIST 配置失败 503、姓名规范化、幂等载荷冲突、协议元数据、并发重查、
  滚动窗口公式与性能基线。`rules status` 显示 77 条 active、覆盖率 17/67，
  13 条旧用例因 rule_key 漂移标记 stale；A4 上下文与用例待重建。
- **2026-11-10**：按 `ruleset.md` 修订设计文档至 V1.1：
  CF-01 城市列表改为 `CITY_LIST` 配置下发（空/失败返回 503）；CF-02 补充验证码
  5 分钟有效、错误 5 次作废及错误码；CF-03 主档新增协议勾选时间/IP/UA 字段；
  CF-04 删除不可达的 `INVALID_CITY` 分支。同时补齐 GAP-01～08 与 AS-01～07：
  短信通道 5 秒超时/503、图形验证码契约、同幂等键不同请求体 409、姓名规范化、
  频控滚动窗口、并发冲突重查、IP/UA 留存、性能基线、协议版本更新策略等；新增
  7.3「待业务确认」（Q-01/Q-02/Q-03 与窗口/SLA 默认口径）。`ruleset.json`、
  上下文与已批准用例尚未按 V1.1 重抽取。
- **2026-11-09**：新增已批准用例脑图：`cases.approved.mindmap.md`（Mermaid）、
  `cases.approved.mindmap.svg` / `.png`（自包含树图）；按 9 个业务主题分组 22 条用例。
- **2026-11-09**：新增已批准用例文本版 `engine/examples/partner_onboarding/cases.approved.md`
  （22 条，含索引、请求/步骤、断言、前置状态、需求来源与审核记录）。
- **2026-11-09**：人工批准 `prop-d30172d8dcd6ca3a`（reviewer `JeffreyShouzhong2`）：
  22 approve 入库（`store_version=1`，`caseset.json` 已生成），9 reject 未入库。
  运行 `20261109-180859-840-cases` 生成 pytest 脚本（1 测试文件 + conftest，
  22 条可收集）；未执行，覆盖率 10/34。
- **2026-11-09**：新增 `cases review --proposal <dir> --pending` 交互审核命令：
  逐条展示规则/需求/请求/步骤/断言/告警，`a/r/s/q` 写回 `cases.review.json`；
  只记录人工决定，不自动批准；空输入跳过，`q` 保存退出。
- **2026-11-09**：审核文件 `cases.review.md` 增加审核导航：按规则冲突 / 多步骤 /
  前置状态 / 常规分组，冲突 topic 列出受影响用例，多请求用例逐步渲染；文档同步
  “先读导航、再改 `cases.review.json`”的审核顺序。
- **2026-11-09**：实现多请求用例模型（`steps[]`、跨步骤 `$ref`、逐步骤校验、指纹兼容、
  pytest 顺序生成）；合伙人草案新增 4 条频控用例，覆盖 BR-05/06/07/08/09/10；提案更新为
  `prop-d30172d8dcd6ca3a`（31 new / 0 error / 18 warning）；全量测试 267 passed，
  4 条频控用例实跑 4/4 通过。
- **2026-11-09**：新增 `mock semantics` 独立阶段与 `data semantics --llm`；补齐合伙人
  示例的 21 条字段语义、2 个业务态、3 个下游 Mock 语义；修复 dep token 子串误匹配与
  顶层 `gaps` 丢失；上下文重建为 `4ff385a6…`，提案更新为
  `prop-4dee9aa420157005`（27 new / 0 error / 18 warning）。
- **2026-11-09**：完成四类缺口修复（提示词 7 维清单、上下文保留真实路由/下游并打包
  catalog/mock、全响应码字段与 `error_codes`、`rule_keys` + `rule.uncovered`）；
  重新生成上下文 `2b990b11…` 与提案 `prop-a9b6f61c027f3e02`（23 new / 0 error /
  18 warning）；28 条响应字段 warning 消除。
- **2026-11-09**：按 ruleset v2 + 上下文 `d0a6b6f2…` 重写 A4 草案（23 条 business
  用例）；重新导入生成待审提案 `prop-02bc6b8d2997fda5`（46 warning）。
- **2026-11-09**：建立本进度文件，记录当前状态与阻塞项。
- **2026-11-04**：完成 ruleset v2；生成 A4 上下文；为合伙人示例新增 3 个模拟下游
  调用并验证画像 / Mock 计划；导入 17 条用例草案生成待审提案；新增项目使用文档与
  C1 使用文档。
