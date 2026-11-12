# AstSight Engine · 正式实现

> 项目名：**AstSight**（AST + Sight）｜ CLI：`astsight` ｜ Maven：`io.astsight:astsight-core`
> 正式实现根目录。
> 设计依据：`framework-capability-design.md`（能力设计）+ `agent-architecture.md`（Agent 架构）。
> **运行环境基线：Linux（服务器 / CI 容器）**；Windows 仅用于开发机本地调试，不作为部署与验收环境。
>
> 第一次使用见 [AstSight 快速上手指南](../doc/AstSight-快速上手指南.md)；
> 全项目安装、配置、命令与排错见 [AstSight 使用文档](../doc/AstSight-使用文档.md)。

## 当前进度：M1 确定性闭环 + 规则驱动用例

M1 的目标不是“功能齐全”，而是**一条可演示、可验证、结论诚实的链路**：

```
A. 代码变更驱动
git diff → 变更符号 → 影响接口 → OpenAPI 画像 → 模板用例
         → pytest 编码 → collect 自检 → 执行 → 确定性归因 → 报告 + 门禁

B. 需求驱动（规则驱动，canonical）
需求/设计文档（+ 可选契约源）→ rules extract / rules c1（唯一入口）
                        → 确定性 context/prompt/schema
                        → A1 LLM 抽取 BR（a1_requirement_analysis）+ CR 确定性提取
                        → 校验通过写 ruleset.json（rule_key / authority / diff）+ ruleset.md
                        → C3.5.1 mock semantics（契约 success + 规则分支）
                        → C5a data catalog（有 DDL 时；确定性结构 + LLM 语义）
                        → A4 LLM 设计业务/代码两级草案（规则 + 画像 + Mock 语义 + 造数目录）
                        → cases import（校验 + 与已批准库 diff）
                        → 人工审核 cases.review.json
                        → cases approve（写版本化 caseset.json）
                        → C5b data plan（有 catalog 时）→ run --mode cases → pytest → 报告
```

两条入口共用同一套编码、执行与报告；文档驱动不会伪造 git 变更。
**审核前草案不进入用例库，也不会被编码执行。** 规则层、两级用例、C3.5.1 Mock 语义、
C5a/C5b 造数（SQLite/PostgreSQL/MySQL）、事务化 apply/teardown 与规则驱动的
`agent run` 已落地；旧
“语义分析 → 用例草案”链路只保留兼容模块（`prompts/legacy/`），不再是主流程。
HTTP Mock 运行时核心（C3.5.5）、C3 的 Python 下游 HTTP 调用点自动识别，以及
`agent run` 的“先起 Mock → 计算注入 → 用注入环境启动 SUT”已实现；
`run --mock-plan` 仍只负责起 Mock 并产出注入清单，由调用方决定 SUT 启动方式。
本 README 的 ✅ 仅代表已交付部分，设计目标与实现状态分开写。
v4.2 的形态范围：HTTP、gRPC（仅 unary）、Dubbo（仅 2.7+，走独立 Java sidecar 泛化调用）；
Thrift 不考虑。C2 只感知被测仓库的**提供方**端点（HTTP 客户端/`@DubboReference`/gRPC stub
属消费方），并做 base/target 契约 diff 覆盖纯契约变更；同一实现多协议暴露时保留全部端点，
默认 primary 全量 + alias 契约冒烟。`unsupported` 端点显式列出，不静默降级；调用 SUT 不使用
Java agent；v4.3 起 Mock 覆盖 SUT → HTTP 与 Dubbo 2.7+ 下游（Dubbo 走 agent + ByteBuddy +
Filter，定位为低侵入；SUT → gRPC 下游 v4.3 仍 `mock="unsupported"`，v4.4 决策为 gripmock
假服务器 + target 重定向，尚未实现）。v4.6 起删除 Feign 特殊支持：HTTP 客户端（含 Feign）
一律按普通出站调用处理，通用机制覆盖不了的下游直接标 `mock="unsupported"` / `unidentified`。
v4.6.1 起 HTTP Mock 首版只做明文 HTTP，HTTPS 标 `tls_not_supported`；下游 `dep_id` 为
`http:{downstream_key} {METHOD} {path}`，运行期按 `route_key` 路由。运行时核心已在
`astsight/mock/` 落地：`dep_id`/`route_key` 构建、Host/路径路由、会话分支、`mock_calls.jsonl`
与内嵌 uvicorn；执行链路已接入：生成物按用例创建/绑定会话，先 drain 再关闭，runner 透传
Mock 管理地址与 token，晚到调用归因到已关闭用例。`run --mock-plan` 已能按计划启动/停止内嵌
服务并产出注入清单；`agent run` 消费同一份清单，在启动 SUT 时写入注入环境变量。
v1 注入只承诺下游专属 `url_override` 与显式 `http_proxy`；`discovery_override`/透明代理条件支持，
共享 base URL（`mock_config_ambiguous`）、同 JVM bean mock、动态 URL、请求匹配/序列/流式不支持。

| 模块 | 状态 | 说明 |
|---|---|---|
| `contracts/` 契约层 | ✅ | 核心实体 + 用例库 `CaseSet/CaseEntry/CaseReview` + 规则 `RuleSet/ContractRule` + Mock 语义 + 造数 `DataCatalog/DataPlan` |
| `orchestration/` 编排骨架 | ✅ | LangGraph 骨架：入口分流 + A4.5 审核门（M1 CLI 走确定性直连） |
| `codegraph/` 接入层 | ✅ | 代码嵌入客户端 + 路由前缀重建 + InMemory 替身 |
| `astsight/changes/` C2 | ✅ | git 两点/三点 diff、AST 变更符号、直接命中 + 图谱反向追溯 |
| `astsight/profile/` C3 | ✅ | OpenAPI 参数/请求体/响应字段/约束 → 画像；无契约标 `contract_incomplete`；Python 源码 AST 识别 `requests`/`httpx`/`urllib` 出站调用并生成 `dep_id` |
| `astsight/cases/context.py` | ✅ | 需求 section 解析 + 接口目录 → 上下文包（prompt/schema）；有 OpenAPI 时取契约字段，没有时从代码扫描路由并标 `contract_incomplete` |
| `astsight/cases/validate.py` | ✅ | 草案结构、端点/字段/断言、需求 quote 反幻觉、路径与大小限制 |
| `astsight/cases/proposal.py` | ✅ | new/update/revive/deprecate/unchanged/kept_out_of_scope 六路 diff |
| `astsight/cases/store.py` | ✅ | 项目级 `caseset.json`：版本、history、review_log、原子写与备份 |
| `astsight/cases/templates.py` | ✅ | delta/full 的确定性模板用例（不参与审核用例编号） |
| `astsight/codegen/` C6 | ✅ | 每接口一个 pytest 文件 + conftest，语法自检 + collect 自检 |
| `astsight/execute/` C7 | ✅ | JUnit XML、确定性归因、只对环境抖动做有界重试 |
| `astsight/report/` C7.4 | ✅ | JSON + Markdown；用例库版本、待审、需求覆盖、drift 单列 |
| `astsight/mock/` C3.5.5 | ◐ 核心+执行链路+语义+计划+启动注入 | `dep_id` 唯一性/`route_key`、Host/路径路由、会话分支、drain、晚到调用归因、会话历史、`mock_not_used`、`mock_calls.jsonl`、内嵌 uvicorn；`mock semantics` 生成契约 `success` + 规则分支（可选 `--draft`/`--llm` 候选）；`run --mock-plan` 管理生命周期、写 `run_manifest.json`/`mock_injection.json`；`mock plan` 从 C3 调用点生成计划草案；`agent run` 用注入清单启动 SUT |
| `astsight/llm/` | ✅ | OpenAI 兼容/Anthropic 协议、JSON 提取、超时/大小/重试上限、密钥脱敏；无新增 SDK 依赖 |
| `astsight/rules/` | ✅ | C1/A1 规则库：context、LLM 草案校验、`rule_key`/fingerprint、CR 确定性提取、diff/漂移、冲突记录、原子写 `ruleset.json` |
| `astsight/mock/semantic.py` C3.5.1 | ✅ | 契约层 `success` 确定性生成 + 规则映射分支；LLM 只补有依据的候选 |
| `astsight/data/` C5 | ✅（SQLite/PostgreSQL/MySQL） | DDL/seed → `data_catalog.json`；批准后 → `data_plan.json`；参数化 SQL、FK 拓扑、事务化 apply/teardown；方言决定占位符与标识符引用，plan 与数据库不一致时拒执行 |
| `astsight/agent/` | ✅ 首版 | A1 规则适配、C3.5.1 Mock 语义适配、C5a/C5b 造数适配、A4 用例设计、MockPlan、SUT 启动注入、A5/A6 合并诊断修复（含 Mock 分支热更新）与端到端编排 |
| `examples/order_service` | ✅ | 18 接口 FastAPI 示例 + 可重置种子数据 + 示例草案 |
| `examples/downstream_service` | ✅ | 上游报价 → 下游价格服务的 HTTP Mock 示例（环境变量注入） |
| `examples/demo_delta.py` | ✅ | 一条命令跑真实 git / 真实服务 / 真实 pytest |
| `examples/demo_case_review.py` | ✅ | 一条命令跑需求 → 草案 → 审核 → 版本化 → pytest |
| `examples/demo_agent_flow.py` | ✅ | 一条命令跑 变更 → 语义 → 用例 → Mock → 执行 → 诊断 → 修复（需模型 key） |

### 当前边界（不假装）

- **LLM 是可选入口，不是默认执行层**：`rules prepare/import/status`、
  `cases import/approve`、`data catalog/plan/apply` 的确定性部分与 `run` 保持零模型；
  `rules extract`、`agent run` 或显式调用适配层时才请求模型，且模型输出必须通过
  endpoint/quote/dep_id/branch/rule_key/FK 拓扑/断言的全套校验。密钥只从环境变量或
  未提交的本地 `llm.json` 读取，不落盘到运行产物、不进报告。`rules extract`（别名 `rules c1`）是需求/设计文档入口（框架内调用模型）；
  `rules prepare` + 外部模型 + `rules import` 是同一流程的两段式拆分，
  供 Claude Code Skill、人工、模型网关或离线批处理使用。
- **初始用例审核不能自动化**：`cases approve` 必须显式给 `--reviewer`；`agent run`
  也只在提供 `--approve-as <reviewer>` 时批准 new/update/revive，`deprecate` 永远 reject。
  不提供 `--approve-as` 时流程停在待审 proposal。
- **运行期修复是 Agent 动作，不是人工审核**：A5/A6 合并为一个节点，诊断后可直接改
  用例请求/断言/绑定和 Mock 分支；用例补丁生成新 proposal 后以
  `astsight-agent:repair` 身份自动批准，Mock 补丁热更新到运行中的 Mock 服务。
  修复不得删除/削弱断言，身份字段不可改；`deprecate` 仍然只能人工批准。
- **多请求限于单用例内串行**：`steps[]` 按顺序共享同一个 `client`/Mock 会话执行，
  支持 `{"$ref": "<step>.<field>"}` 读取更早步骤的响应（body/query/path_params/断言
  expect/`db_row.where`，不支持 header；`body_schema` 不解析引用）。不生成跨用例捕获、
  鉴权链路、并发或重试编排；需要数据库前置状态的用例仍用 `preconditions[].state_key`
  引用 `data_catalog`，由 C5b 生成单用例 fixture。无 catalog/plan 或未配置
  `ASTSIGHT_DB_URL` 时标 `blocked`，不伪造可执行用例。
- **断言覆盖到结构与持久化**：支持 `status_code`、`body_field`、`header`（含 `contains`）、
  OpenAPI `body_schema`（本地 `$ref` 内联）和只读 `db_row`（含 `count_eq`）。失败场景可断言
  `db_row/absent` 验证无副作用。`assert_strength` 按内容推导：L1 状态/存在/结构、L2 确定值、
  L3 `$ref` 关系、L4 引用两个及以上步骤；声明不一致会 warning，导入以推导值为准。
  `db_row` 需要 `ASTSIGHT_DB_URL`，未配置时显式 skip `data_unavailable`。
- **自动“矫正”仅两件事**：生成后 `compile` 自检 + 执行前 `pytest --collect-only`；执行后只对
  连接/超时类环境抖动重试（默认 1 次，上限 3），**不修改业务代码，不猜失败原因**。
- **造数方言限于 SQLite/PostgreSQL/MySQL**：C5a/C5b 生成目录与逐用例计划，三者共用
  同一套 fixture 校验，SQL 占位符/引用在计划生成时固化；计划方言与 `--db-url`
  不一致时标 `data_dialect_mismatch`，不静默改写 SQL。建库、迁移、连接池与跨库事务
  不在范围内。示例服务仍用 `/_reset` 保证可重复，不把示例重置当成通用造数。
- **Java/Spring 变更感知输出到 API 级**：CodeGraph 原生识别 Spring 路由与方法行区间，
  AstSight 做「diff 行 ∩ 符号区间 → 反向闭包 ∩ 路由」。方法只是中间证据，不是输出单位。
  Java 路由没有源码侧第二来源，证据等级只到 `inferred`；Spring 路由覆盖缺口会进
  `unsupported/unresolved`，不会被算作已覆盖。
- **非 Python 符号映射依赖 CodeGraph**：`--codegraph off` 时 `.java/.kt/.ts` 等文件进
  `unsupported_files`，框架不会用正则猜符号边界。
- **被删除的接口不会被列为 changed_api**：路由清单来自当前工作树，纯删除只能文件级
  兜底影响同文件现存路由；要覆盖“接口被删”需从 base 分支/OpenAPI 建第二份路由清单。
- **replay 模式**尚未实现，报告中不会把它算作已覆盖。

## 快速开始

目标运行环境是 Linux（服务器 / CI 容器）。下面这条只是开发机（Windows）本地调试用的解释器：

```
C:\Users\magic\.workbuddy\binaries\python\envs\default\Scripts\python.exe
```

若当前环境没有依赖，可用 `uv run --with-requirements requirements.txt python ...`。

```bash
cd engine
pip install -r requirements.txt

# 代码变更闭环：自动建临时 git 仓库、起服务、跑 delta
python examples/demo_delta.py

# 注入 500 缺陷：验证门禁会 FAIL 并归因为 code_defect
python examples/demo_delta.py --defect

# 保留临时仓库和全部产物
python examples/demo_delta.py --keep

# 需求 → 审核用例 → 版本化 → 执行（示例自动批准仅演示用）
python examples/demo_case_review.py
python examples/demo_case_review.py --no-auto-approve  # 停在待审 proposal
python examples/demo_case_review.py --keep
```

直接对已有仓库运行：

```bash
cd engine
PYTHONPATH=. python -m astsight run \
  --project /path/to/service-repo \
  --mode delta --base main --target feature \
  --base-url http://127.0.0.1:8010 \
  --reset-url http://127.0.0.1:8010/_reset \
  --openapi /path/to/service-repo/openapi.json   # 可选；缺省自动发现/从服务拉取
```

- `--mode full`：扫全量路由（用于建基线；示例服务会因隐藏的 `/_reset` 报一个契约缺口）。
- `--allow-unmapped`：不把覆盖缺口当门禁阻塞；缺口仍逐条列出。
- 退出码：`0=pass`、`1=fail`、`2=not_run`（未执行）。
- Git Bash 会把以 `/` 开头的参数改写成 Windows 路径；`--reset-url` 建议传完整 URL，
  或设置 `MSYS_NO_PATHCONV=1`。

Java/Spring 的 API 级变更感知走同一条命令，但必须让 CodeGraph 可用。
执行过 `python -m astsight codegraph install` 后会自动使用用户级缓存，
`--codegraph-module-root` 只用于覆盖安装目录：

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/java-repo --mode delta --base main --target feature \
  --codegraph on \
  --full-index \
  --openapi /path/to/openapi.json
```

只想看第一层时加 `--skip-execute`（不请求被测服务），然后读
`.astsight/runs/<run_id>/changed_apis.json`：
`changed_apis[]` 是受影响接口，`evidence=verified` 表示方法级行映射，
`inferred` 表示类级/文件级兜底；`unresolved_symbols` 与 `unsupported_files`
是“没追到/没看到”的边界，必须一起看。`--target` 必须是当前检出的工作树，
因为 CodeGraph 索引的是工作树，不是任意 git 引用。

### 真实 Spring 仓库验证（CodeGraph 1.6.2）

| 仓库 | 路由声明方式 | 注解真值 | 图谱路由 | 结论 |
|---|---|---|---|---|
| `spring-petclinic` | 方法上 `@GetMapping/@PostMapping` | 17 | 17 | 一致 |
| `jhipster-sample-app` | 类级前缀 + 方法注解 | 37 条注解 / 38 个路径 | 38（Java） | 一致（含数组 path） |
| `spring-petclinic-rest` | 注解在 openapi-generator 生成的接口里，源码无方法注解 | 37（OpenAPI） | 1 | 清单由 OpenAPI 补齐；变更按 operationId 兜底 |

分支 diff 的实测行为（`spring-petclinic` / `jhipster-sample-app`）：

- **handler 自身改动** → `high/verified`，接口命中准确。
- **helper / Service 改动** → 沿调用链得到 `medium/inferred`；jhipster 改
  `UserService.updateUser(AdminUserDTO)` 精确命中两个 `PUT /api/admin/users*`。
- **`@ModelAttribute` 方法改动** → 0 接口 + `unresolved(no_route_reached)`。
  Spring 反射调用不在静态调用图里，这是已知盲区，但不会静默通过。
- **同名重载会串边**：改 `Owner.getPet(String, boolean)` 时，`addVisit` 里的
  `getPet(Integer)` 调用被归到该节点，多出 `POST /owners/{ownerId}/pets/{petId}/visits/new`
  一个误报。图谱路径的结果本来就是 `inferred`，仍需人工确认。

路由声明在生成接口/父接口、源码只剩 `@Override` 的项目（如
`spring-petclinic-rest`），CodeGraph 看不到 HTTP method。现在会把本地 OpenAPI
（含 Spring 常见的 `src/main/resources/openapi.yml`）的 operation 并入路由清单，
所以**接口清单**完整（`routes.json` 37/37）。

补入项没有源码 handler，直接命中和调用链都归因不到。为此提供 **operationId
兜底**：变更符号是 Controller 内的方法、方法名**唯一**匹配某个 OpenAPI
`operationId`、且前两种方式都没找到接口时，把变更归到该 operation，标
`low/inferred`，`attribution=openapi_operation_id`，trace 记
`operationId:<id>`。限制条件：

- 只匹配 OpenAPI 补入的无 handler 路由，不会碰普通路由；
- 变更文件必须是含 `@Controller/@RestController` 的 Java/Kotlin 文件，
  防止同名 Service 方法误归因；
- `operationId` 重复时视为歧义，不自动归因，进入 `unresolved`；
- 这是启发式：完整方法名、重载、跨 Controller 同名仍可能出错，必须人工复核。

在 `spring-petclinic-rest` 的 `master...feature/rest-change` 上，`listOwners`
现在会归到 `GET /owners`（`low/inferred`），不再是 0 个受影响接口。

路由清单健康度（`routes.json.audit.health`）会比较可归因路由数与 OpenAPI
operation 数、含 `@Controller/@RestController` 的源文件数；还会检查同一
`(method, path)` 是否对应多个不同 handler/owner、是否存在无 method 或未知
method 的路由（如 DRF 的 `VIEWSET`）。任一成立就标 `route_inventory_degraded`，
报告门禁给 WARN，并写明“受影响接口可能被少报或误报”。
`spring-petclinic-rest` 即使有 operationId 兜底，这个告警仍会保留：它描述的
是“清单里大部分路由没有源码 handler”，兜底不能替代完整归因。

### 真实 Python 仓库验证（CodeGraph 1.6.2）

| 仓库 | 路由声明方式 | 插件路由 | 分支 diff 结果 | 结论 |
|---|---|---|---|---|
| `flask-realworld-example-app` | Blueprint + `@bp.route` | 19/19 | 2 个接口，`high/verified` + `medium/inferred` | 与图谱完全一致 |
| `fastapi-realworld-example-app` | `APIRouter` + 多层 `include_router` | 19/19 | 5 个接口，3 个 `high/verified` + 2 个 `medium/inferred` | 前缀由 `settings.api_prefix` 字面量还原，全部标 `inferred` |
| `django-realworld-example-app` | `url()` + `APIView.as_view()` + DRF `router.register` | 19 个 operation | `ArticleViewSet.list` → `GET /api/articles/`（`high/verified`） | CRUD/as_view 动作已展开；函数视图 method 仍未知 |

Python 侧修复的具体行为：

- **FastAPI**：支持 `from pkg import module` + `module.router`、`import pkg as x` +
  `x.router`、相对导入与别名；`prefix=settings.api_prefix` 会从全项目唯一的
  字符串字面量设置还原。任何推导出的前缀都把路由标 `inferred`，不冒充 verified。
  当 CodeGraph 把前缀整个丢掉、只留 `/` 时，只有 handler 唯一对应一个源码路径才
  对齐，并标 `inferred / graph_path_missing`；多路径仍不猜。
- **Django/DRF**：支持 `path()` / `re_path()` / `url()`；正则路径规范化为
  `/articles/feed`、`/articles/{slug}/`；`APIView.as_view()` 按类里定义的
  `get/post/...` 或 DRF 通用视图基类展开；`router.register()` 按
  ViewSet/Mixin 约定展开 CRUD 与 `@action`，detail 路径使用 `lookup_field`。
  这些展开来自框架约定，统一标 `inferred`，并带 `handler_owner`，避免不同
  ViewSet 的同名 `list/create` 互相串接口。
- **仍不猜的部分**：Django 函数视图（`def feed(request)`）的 method 在函数体里
  按 `request.method` 分支，静态不可知，路由保留空 method 并由健康检查告警；
  完全动态注册、元类生成、运行时拼接的路由不生成。



## 规则 → 用例 → 审核 → 版本化

canonical 链路是规则驱动的；`cases prepare` 只生成端点/需求上下文包，不再承担 A1 语义分析。
完整自动编排见“Agent 端到端”，下面是逐步手工命令：

```bash
cd engine
PROJECT=examples/order_service

# 1. 需求/设计文档 → 规则库（C1 唯一入口；设计、OpenAPI 均可选）
#    确定性 context/prompt/schema + 模型抽 BR + 校验/降级/冲突/CR/版本化，
#    一次产出 ruleset.json 与人工评审用 ruleset.md；校验失败不写规则库。
#    `rules c1` 是 `rules extract` 的别名。
PYTHONPATH=. python -m astsight rules c1 \
  --project $PROJECT \
  --requirements $PROJECT/docs/requirements.md \
  --design $PROJECT/docs/api-design.md \
  --openapi $PROJECT/openapi.json

# 产物：$PROJECT/.astsight/rules/<analysis_id>/
#   context.json  prompt.md  rules.schema.json  rules.draft.json  README.md
#       $PROJECT/ruleset.json  $PROJECT/ruleset.md
# 需求修复后重跑同一条命令：每次都是完整快照，旧 questions/gaps/conflicts 不残留。

# 2.（可选）模型在框架外运行时的两段式：prepare 只写上下文，外部模型产出
#    rules.draft.json，再 import 做同一套确定性校验与写库。
PYTHONPATH=. python -m astsight rules prepare \
  --project $PROJECT --requirements $PROJECT/docs/requirements.md \
  --openapi $PROJECT/openapi.json
PYTHONPATH=. python -m astsight rules import \
  --project $PROJECT \
  --context $PROJECT/.astsight/rules/<analysis_id> \
  --draft $PROJECT/rules.draft.json \
  --rule-base ruleset.json

# 3. 生成端点上下文包（A4 草案的 endpoint/quote 事实源）
#    有 OpenAPI 也会扫代码，用真实 handler 补 downstream_deps；
#    --data-catalog/--mock-semantics 可把造数状态和下游分支一起打包。
PYTHONPATH=. python -m astsight cases prepare \
  --project $PROJECT \
  --requirements $PROJECT/docs/api-design.md \
  --requirements $PROJECT/docs/requirements.md \
  --data-catalog $PROJECT/.astsight/data/data_catalog.json \
  --base-url http://127.0.0.1:8010

# 产物：$PROJECT/.astsight/casegen/<context_id>/
#   context.json  prompt.md  cases.schema.json  README.md

# 4. 按 prompts/a4_case_design.md 写出 cases.draft.json：
#    level/rule_refs/requirement_refs/change_refs/preconditions/expect
#    mock_bindings 是 {dep_id: branch}
#    连续请求规则用 steps[]；跨步骤取值用 {"$ref": "<step>.<field>"}

# 5. 校验并与已批准用例库 diff（只写 proposal，不改 caseset.json）
PYTHONPATH=. python -m astsight cases import \
  --project $PROJECT \
  --context $PROJECT/.astsight/casegen/<context_id> \
  --draft $PROJECT/case_samples/cases.draft.json \
  --ruleset ruleset.json

# 6. 先读 cases.review.md 的审核导航，再逐条填决定：
#    PYTHONPATH=. python -m astsight cases review \
#      --proposal $PROJECT/.astsight/cases/proposals/<proposal_id> --pending
#    a/r/s/q 交互；也可手工编辑 cases.review.json
#    （reviewer 由 approve 命令指定，item 内 reviewer 不参与批准）

# 7. 应用审核结果
PYTHONPATH=. python -m astsight cases approve \
  --project $PROJECT \
  --proposal $PROJECT/.astsight/cases/proposals/<proposal_id> \
  --reviewer alice

# 8. 查看规则库/用例库/待审/覆盖/漂移
PYTHONPATH=. python -m astsight rules status --project $PROJECT --json
PYTHONPATH=. python -m astsight cases status --project $PROJECT --json

# 8.1 import 已自动写出 ruleset.md；需要重渲染时（同一份 ruleset 的只读视图，不调用模型）
PYTHONPATH=. python -m astsight rules render --project $PROJECT --out ruleset.md

# 9. 有 DDL 时：C5a 目录 →（批准后）C5b 计划 → 执行
PYTHONPATH=. python -m astsight data catalog --project $PROJECT \
  --ddl $PROJECT/schema.sql --out .astsight/data
PYTHONPATH=. python -m astsight data plan --project $PROJECT \
  --catalog .astsight/data/data_catalog.json --cases caseset.json --out .astsight/data

# 10. 只执行已批准用例
PYTHONPATH=. python -m astsight run --project $PROJECT --mode cases \
  --base-url http://127.0.0.1:8010 \
  --reset-url http://127.0.0.1:8010/_reset \
  --ruleset ruleset.json --data-plan .astsight/data/data_plan.json
```

`rules` 命令参数：

- `extract --requirements PATH [--openapi PATH] [--rule-base PATH] [--dry-run]`：
  **需求文档入口**。生成确定性上下文 → 调用模型抽 BR → 校验/降级/冲突/CR/版本化 →
  写 `ruleset.json` + `ruleset.md`。`--dry-run` 只校验不写库；`--max-rules` 默认 500。
- `prepare --requirements PATH`：可重复；只生成 `context.json`、`prompt.md`、
  `rules.schema.json`，供模型在框架外运行（Skill / 人工 / 模型网关 / 离线批处理）；
  `context.json` 的 section 同时有稳定 `path` 与可读 `number`。
- `import --context DIR --draft PATH [--rule-base PATH]`：框架外模型的第二阶段；逐项校验
  quote/authority，框架重算 `rule_key`/fingerprint，确定性合并 CR，原子写 `ruleset.json`；
  失败不改旧库。同时把同一份规则库渲染成 `ruleset.md`（只读评审视图）。
- `status --json`：版本、BR/CR 数量、未确认规则、questions/gaps/conflicts、drift/orphaned。
  冲突只记录并展示；规则阶段没有人工审核/裁决节点，冲突在用例审核时确认。
- `render --out PATH`：手动重渲染评审文档；内容完全由 `ruleset.json` 派生，不调用模型。

**规则闭环**：`ruleset.md` 中列出的冲突/缺口/待确认问题回写需求文档 → 重新
`rules extract`（或框架外模式下重新 `prepare` + `import`）。每次抽取都是完整快照，
上一版已修复的问题不会残留；`ruleset.json` 只能由 `rules extract/import` 写入，
不支持手工编辑规则——要改规则就改需求原文后重抽，否则规则库会变成第二个人工维护的真相源。

`data` 命令参数：

- `catalog --ddl PATH [--seed PATH] [--context DIR]`：确定性解析 DDL/FK/拓扑，不调用模型；
  无 seed 时 `states` 为空，字段语义按命名/类型推断，无法确定的进 `gaps`。
- `semantics --catalog PATH (--draft PATH | --llm) [--context DIR] [--ruleset PATH]`：
  合并字段业务语义与候选业务态。`--draft` 读框架外模型产物（`prompts/c5_data_catalog.md`
  的输出），`--llm` 直接调用模型；两条路径都逐项校验表/列/枚举/规则引用，非法候选整体拒绝，
  通过后写回 `data_catalog.json`（`states` 可供 A4 的 `preconditions.state_key` 引用）。
- `plan --catalog PATH --cases PATH [--fixtures PATH]`：只用目录中存在的表/列/状态；
  校验 FK 闭包、类型/枚举、preconditions 覆盖与 SQL 标识符白名单。
- `apply --db-url sqlite:///... --plan PATH [--keep-data]`：事务内 apply，失败回滚。
  `--db-url` 也接受 `postgresql://`（psycopg）与 `mysql://`（PyMySQL）；
  计划方言与数据库不一致时标 `dialect_mismatch`。
- `catalog --ddl PATH --dialect sqlite|postgres|mysql`：显式声明造数方言（默认 sqlite）。
  `agent run --db-dialect` 可在没有 `--db-url` 时指定方言，提供 `--db-url` 时自动推断。

`cases` 命令参数：

- `prepare --requirements PATH`：可重复；目录会递归读取 `.md/.markdown/.txt`。
  OpenAPI 依次从 `--openapi`、项目内自动发现、`--base-url` 获取。有 OpenAPI 时也会
  扫描代码路由，用真实 handler/file 补 `downstream_deps`，但只保留 OpenAPI 端点；
  代码独有的内部路由不进入 A4 上下文。没有 OpenAPI 时画像标
  `contract_incomplete=true`、`contract_incomplete_reason=openapi_missing`，
  `context.json` 的 `warnings` 记录 `openapi_missing` / `routes_missing`；
  扫描失败不阻断，记录 `route_scan_failed:*`；找不到字段就留空，不猜测。
  `--no-scan-routes` 关闭扫描，`--codegraph auto|on|off` 控制图谱模式；
  `--data-catalog` / `--mock-semantics` 可把造数与下游分支随上下文打包。
- `import --context PATH --draft PATH`：校验失败退出非 0，且**不修改 store**；
  `--dry-run` 只输出校验与 diff。
- `approve --proposal PATH --reviewer NAME`：必须显式 reviewer；重新校验草案、重新 diff、
  重算 fingerprint，不信任审核文件里的旧字段。store 版本/hash 已变化时拒绝批准并要求重新 import。
- `status --json`：输出 store 版本、approved/deprecated 数量、待审 proposal、需求覆盖。
- Mock 会话：草案可填 `mock_bindings: {dep_id: branch}`；生成的 conftest 在用例开始时
  创建会话、结束时先 drain 再关闭，晚到调用归因到刚关闭的用例。运行 `run --mode cases`
  时用 `--mock-admin-url` / `--mock-token` / `--mock-namespace` 接入已启动的 Mock 服务
  （也可用同名 `ASTSIGHT_MOCK_*` 环境变量）；未配置时 fixture 为 no-op。运行结束读取
  `mock_calls.jsonl` 汇总到报告的“下游 Mock”章节：阻断类调用进 blocker，晚到/过期/损坏
  证据进 warning 并标 `degraded`。证据路径可用 `--mock-evidence` 指定，缺省从
  `GET /_admin/server` 获取。会话历史从 `GET /_admin/sessions?run_scope=<run_id>` 读取
  （未显式设置 `--mock-run-scope` 时自动用本次 `run_id`），逐会话核对绑定 `dep_id` 是否
  至少命中一次；零调用的绑定报 `mock_not_used` 并阻塞，避免“用例通过但没走到 Mock”的假绿。
  只给 `--mock-evidence` 而未提供 `--mock-admin-url` 时无法核对会话，报告标
  “会话绑定未校验”并 `degraded`；证据里的会话不在历史中同样降级。

### Mock 运行计划（P0）

`run --mock-plan plan.json` 会在执行前启动内嵌 `MockHttpServer`，自动把 admin 地址、随机
token、namespace 和 `mock_calls.jsonl` 路径传给 pytest；运行结束（含异常）必定停止服务。
它不能与 `--mock-admin-url` / `--mock-token` / `--mock-namespace` / `--mock-evidence`
同时使用。

最小计划示例：

```json
{
  "schema_version": 1,
  "sut_id": "order-service",
  "port": 0,
  "routes": [
    {
      "dep_id": "http:svc:pay-service POST /gateway/pay",
      "method": "POST",
      "path_key": "/gateway/pay",
      "host": "pay-service",
      "target": "http://pay-service/gateway/pay",
      "branches": {
        "success": {"status": 200, "body": {"code": 0}},
        "insufficient_balance": {"status": 400, "body": {"code": "40001"}}
      }
    }
  ],
  "injections": [
    {
      "dep_id": "http:svc:pay-service POST /gateway/pay",
      "strategy": "url_override",
      "kind": "base_url",
      "property": "PAY_BASE_URL",
      "original": "http://pay-service"
    }
  ]
}
```

- `dep_id` 按 C3.5.5 生成，必须与用例 `mock_bindings` 的键一致；`route_key` 由 `dep_id`
  确定性计算，填写时必须与计算结果一致。
- `port=0` 时由内核分配；外部启动的 SUT 需要固定端口时显式配置 `port`，并在启动 SUT 前
  读取计划中的 `routes[].dep_id` / `injection` 信息，或先启动一次取 `mock_injection.json`。
  `agent run` 会自动完成这个顺序。
- `url_override` 必须提供 `property` 与 `original`；仅静态识别到配置键、不知道原值时，
  可省略 `original` 并写 `reason="original_unknown"`（会产出 warning，SUT 侧按 `url_override`
  覆盖该环境变量）。HTTPS 目标直接以 `tls_not_supported` 阻断；`unsupported` 必须给 `reason`。
- 运行目录新增：
  - `run_manifest.json`：实际 endpoint/port、route、injection、token 指纹；**不含 token**。
  - `mock_injection.json`：供 SUT 启动器消费的注入清单；**不含 token**。
  - `mock_calls.jsonl`：调用证据，参与 `mock_not_used` 校验。
- `run --mock-plan` 不启动 SUT，也不改写 SUT 配置，只负责“起 Mock + 产出注入事实”；
  需要一条命令完成注入与启动时使用 `agent run`（`astsight/agent/sut.py`）。

### 下游调用点识别与计划草案（P1）

`build_profiles(..., project_root=...)` 会在画像阶段从路由 handler 出发做**有界 Python
AST 扫描**（只读源码，不 import/执行被测项目），把出站 HTTP 调用变成 C3 下游依赖：

- 识别 `requests`/`httpx` 的模块函数、`Session`/`Client` 实例、`.request(method, url)`、
  `urllib.request.urlopen`；支持 `import ... as` / `from ... import ... as`。
- URL 证据：完整字面量 URL、环境变量/配置键拼接（`os.environ[...]`、`os.getenv(...)`、
  `settings.xxx`）、f-string、`str.format`、`urljoin`；变量赋值在同一函数/模块内解析。
- 调用链：先沿本地函数调用，再沿 CodeGraph `callees` 扩展；有上限（40 个符号、深度 3、
  200 个调用点、2000 个文件/单文件 1 MB），超限只告警。
- `dep_id` 统一由 `build_dep_id_plan` 生成：目标可静态确定时使用 `host:` 证据；只有路径
  可确定时进入 `call:` 组并标 `inferred`。`svc:`/`cfg:` 需要解析服务发现或配置值，
  当前不做静态猜测。
- 无法解析的调用点进入 `downstream_unsupported`（`target_dynamic`、`target_unresolved`、
  `tls_not_supported`、`http_method_missing`），不进入 Mock 计划。

生成草案：

```bash
python -m astsight mock plan --project examples/order_service --codegraph off \
  --out .astsight/mock-plan.json
```

- 输出是**草案**：`routes[].branches` 一律为空，语义分支必须人工补；
- 有配置键但不知原值的 dep，注入标 `unsupported` + `reason=injection_original_unknown`，
  `property` 给出候选键，不伪造 `original`；
- 有确定目标的 dep 默认使用 `http_proxy`（不依赖 SUT 配置键）；
- 未识别到任何下游、或输出已存在且未加 `--force` 时，命令失败且不写文件；
- 计划可由 `run --mock-plan` 直接使用；`agent run` 会生成同等计划，并把注入写入
  SUT 启动环境后执行。

### Mock 语义（C3.5.1）

`mock plan` 只给路由骨架；`mock semantics` 把“每个下游有哪些分支、每个分支返回什么”
变成可校验的契约层，供 A4 的 `mock_bindings: {dep_id: branch}` 引用：

```bash
python -m astsight mock semantics --project $PROJECT \
  --context $PROJECT/.astsight/casegen/<context_id> \
  --ruleset ruleset.json --out .astsight/mock_semantics.json
```

- **确定性层**：每个 dep 固定生成 `success` 分支（`source.kind=contract`，`status=200`）；
  只有 active 的 `error_handling` / `state_transition` / `data_consistency` / `validation`
  规则、且文本含失败提示与 dep token 时，才生成 `rule_<RULE_ID>` 分支（`source.kind=rule`，
  `source.rule_keys` 指向具体规则）。dep token 按词匹配，`sms` 不会命中 `SMS_COOLDOWN`。
- **语义层（可选）**：`--draft PATH` 读取框架外模型产物，`--llm` 直接调用模型
  （`prompts/c35_mock_semantics.md`）。候选分支必须带 `source.rule_keys` 且规则存在；
  未知 dep/规则、重复分支、无依据分支整体报错，不写入。模型标 `verified` 的分支降级为
  `inferred`。顶层 `gaps` 与 per-dep `gaps` 都会合并，未知 dep 的 gap 报错。
- 无法从需求/规则得到依据的失败分支不会被编造，而是写进 `gaps` 并附 `reason`；
  这些缺口应回到 A1 补规则，而不是在 Mock 层猜响应。
- 校验通过后原子写 `mock_semantics.json`，`context_id` 记录来源上下文，便于与 A4 对齐。

### 用例身份、版本与 diff

- 用例身份是 `(endpoint, case_key)`；`case_key` 由草案给出，格式 `^[a-z][a-z0-9_]{2,63}$`。
- `TC-NNNN` **只在批准写库时分配**，从 `cases + history` 的最大编号递增，
  `sequence` 只增不减，**永不复用**。
- 同一用例更新/恢复沿用原 `TC` 编号并 `version + 1`；旧版本进入 `history` 并标 `superseded`，
  因此唯一键是 `(id, version)`。

`cases import` 的差异分类：

| 情况 | 处理 |
|---|---|
| 锚点存在且 fingerprint 相同 | `unchanged`：自动沿用，不进审核文件 |
| 锚点存在但 fingerprint 不同 | `update`：待审；旧批准版本继续生效 |
| 锚点只存在于 deprecated 用例 | `revive`：待审恢复 |
| 新锚点 | `new`：待审新增 |
| 已批准用例在本次 scope 内、草案中消失 | `deprecate`：必须显式批准，不批量废弃 |
| 用例的文档不在本次 scope | `kept_out_of_scope`：原样保留 |
| 草案为空但 store 非空 | 拒绝导入，防止一次空草案清空用例库 |
| 全部 unchanged | 不生成 proposal，报告“无变更” |

- **fingerprint** 只覆盖会改变执行行为或需求归属的内容：endpoint、case_key、category、
  priority、method/path、参数、header、body、asserts、`steps`、requirement_refs 的
  `(doc, section, quote)`。`id/source/title/note/rationale/文档 hash/审批信息/时间戳` 不参与。
  `steps` 为空时不进入指纹，既有单请求用例的指纹保持不变。
- 文档 hash 变化但 section/quote 未变时不触发重审，只产生 `doc_version_drift` 警告。
- 需求覆盖按 Markdown 叶子 section 计算；父标题（通常只是导航/介绍）不进入分母。
- 元数据标题（版本、变更记录、目录、附录、术语、背景、范围等）不计入覆盖率，但在 context 中可见。

### 待审 proposal 与运行语义

- `caseset.json` **只保存已批准用例**；待审草案在 `.astsight/cases/proposals/<proposal_id>/`。
- 默认 `run --mode cases` 只读 `status=approved` 的用例；待审 proposal 产生 warning，
  报告标 `degraded`，不阻塞（本次运行仍使用上一版批准的用例）。
- `--fail-on-pending-proposals` 可把待审升级为 blocker。
- `--min-requirement-coverage 0.8` 可把需求覆盖设为门禁；默认不设阈值。
- OpenAPI 漂移：approved case 的 endpoint 缺失默认 blocker；`--allow-stale-cases`
  可降级为 warning 并标 `degraded`。找不到 OpenAPI 时显式 warning “contract drift 未校验”。
- 需求文档 hash 漂移产生 warning；文档缺失只 warning，不自动删除用例。

> 当前没有跨进程写锁。审核/批准应串行执行；CI 中不要并发 `cases approve`。
> 用例库路径相对 `--project` 解析，绝对路径也支持。
> 单进程 JSON + canonical hash + 原子替换 + 备份（保留最近 10 份）保证不会写半份文件。

## Agent 端到端（可选，接真实模型）

`astsight agent run` 把下面这条链路串起来：

```
需求文档 + 代码变更（OpenAPI 可选，有则补字段/必填/枚举/错误码）
  → 路由/影响接口（full 或 delta；无 OpenAPI 时只用代码路由）
  → A1 规则：LLM 抽取 BR + 确定性 CR → 校验/版本化 → ruleset.json
  → C3.5.1 Mock 语义：契约 success 确定性 + 规则分支 + 有依据的 LLM 候选
  → C5a 数据目录（提供 --ddl 时）：DDL/FK 确定性 + LLM 语义 → data_catalog.json
  → A4 用例设计（模型读规则 + 画像 + Mock 语义 + 造数目录，走 validate_draft）
  → proposal（只生成待审；--approve-as 显式批准 new/update/revive）
  → C5b 造数计划（批准后，有 catalog 时）→ MockPlan → 启动内嵌 Mock
  → 注入环境变量启动 SUT → pytest 执行 → 确定性归因 → A5/A6 诊断+修复（同一节点）
  → 用例补丁走新 proposal（`astsight-agent:repair` 自动批准）/ Mock 补丁热更新
  → 有界重跑（默认最多 3 轮）
```

模型配置可以写配置文件，也可以用环境变量。推荐把模板复制成不含密钥的本地配置：

```bash
cd engine
cp llm.example.json llm.json
```

`llm.json` 字段（`llm.example.json` 是可直接使用的模板）：

```json
{
  "schema_version": 1,
  "provider": "anthropic",
  "model": "claude-sonnet-5-5",
  "base_url": "https://api.anthropic.com",
  "auth_style": "x-api-key",
  "api_key": "",
  "api_key_env": "ASTSIGHT_LLM_API_KEY",
  "timeout": 120,
  "max_tokens": 16384,
  "temperature": 0.0,
  "max_retries": 1
}
```

| 字段 | 说明 |
|---|---|
| `provider` | `openai`（OpenAI 兼容接口）或 `anthropic`（Messages API） |
| `model` | 服务端 model id |
| `base_url` | 网关地址；Anthropic 缺省 `https://api.anthropic.com`，OpenAI 缺省 `https://api.openai.com/v1` |
| `auth_style` | Anthropic 鉴权头：`x-api-key`（SDK 风格）或 `bearer`（兼容网关） |
| `api_key` / `api_key_env` | 直接写 key，或指向保存 key 的环境变量名（推荐后者） |
| `timeout` / `max_tokens` / `temperature` / `max_retries` | 调用参数；推理型模型建议保持 `max_tokens` ≥ 16384 |

- **不要把 key 写进仓库**：`llm.json` 已加入 `.gitignore`；建议保持 `api_key` 为空，
  用 `api_key_env` 指向环境变量（例如 `export ASTSIGHT_LLM_API_KEY=...`）。
- 查找顺序：`--llm-config PATH` / `ASTSIGHT_LLM_CONFIG` → `./llm.json` →
  `~/.astsight/llm.json`。显式路径不存在时直接报错，不会静默回退。
- 环境变量 `ASTSIGHT_LLM_*` 始终覆盖文件值，CI 可以只注入环境变量；
  `ASTSIGHT_LLM_AUTH_STYLE` 也可覆盖文件里的 `auth_style`。
- 未配置 `api_key_env` 时仍兼容 Claude Code / Anthropic 兼容端点的通用变量：
  `ANTHROPIC_AUTH_TOKEN`（映射为 Bearer）/ `ANTHROPIC_API_KEY`、
  `ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`。

OpenAI 兼容网关（DeepSeek 等）的配置示例：

```json
{
  "schema_version": 1,
  "provider": "openai",
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com/v1",
  "api_key_env": "DEEPSEEK_API_KEY",
  "max_tokens": 16384
}
```

推荐先单独跑 `rules extract` 验证连通与输出质量，再进入 `agent run`。
推理型模型（如 `deepseek-flash`）会先输出 thinking，默认 `max_tokens=16384`
已为此留出预算；若仍报 `llm_response_truncated`，提高 `ASTSIGHT_LLM_MAX_TOKENS`。

下游 Mock 示例（报价服务 → 价格服务；`PRICING_BASE_URL` 会被自动覆盖）：

```bash
PYTHONPATH=. python -m astsight agent run \
  --project examples/downstream_service \
  --requirements docs/requirements.md \
  --openapi examples/downstream_service/openapi.json \
  --mode full --codegraph off \
  --base-url http://127.0.0.1:8020 \
  --sut-command "python -m uvicorn app:app --host 127.0.0.1 --port 8020 --log-level warning" \
  --approve-as demo
```

也可以在临时目录里跑（不会把 `caseset.json` 写进示例目录；`--keep` 保留产物）：

```bash
python examples/demo_agent_flow.py
python examples/demo_agent_flow.py --keep
python examples/demo_agent_flow.py --keep --max-repair-rounds 0   # 关闭自动修复
python examples/demo_agent_flow.py --keep --no-auto-approve       # 停在待审 proposal
```

边界：

- OpenAPI 不是前置条件：没有本地契约时用代码路由构建画像，CR 契约规则为空，
  字段格式/必填类用例无法生成；分析阶段 SUT 还没启动，因此不能从 `--base-url` 拉取契约。
  一个可用接口都扫不到时，仍会完成 A1 规则抽取并落盘 `ruleset.json`，然后以
  `not_run` 结束并给出 `routes_missing`，不生成猜测性用例；
- C5 造数在 `agent run` 中只有提供 `--ddl` 才生成目录；提供 `--db-url` 才执行 apply/teardown。
  没有 plan/DB 时带 `preconditions` 的用例标 `blocked`，不会静默跳过；
- A5/A6 默认最多 3 轮（`--max-repair-rounds`，上限 5；0 关闭自动修复）。可修
  `path_params/query/headers/body/asserts/mock_bindings` 与实际绑定的 Mock 分支
  `status/headers/body/semantics`；不改用例身份、规则引用、前置状态，数据/环境错误不触发修复。
  5xx 只有在归因为 `mock_error` 时才允许修 Mock，避免把真实代码缺陷“修绿”；
- 启动期注入只支持环境变量形式的 `url_override` 与显式 `http_proxy`；配置文件、注册中心、
  DNS 改写会显式告警为 unsupported；
- 下游自动识别目前只覆盖 Python 的 `requests`/`httpx`/`urllib` 调用点；
- 每轮对模型输出都有条数/长度上限，模型输出按不可信输入处理。

## 产物与门禁

每次运行落盘到 `<project>/.astsight/runs/<run_id>/`：

```
run_meta.json  routes.json  changes.json  changed_apis.json
profiles.json  ruleset.json  mock_semantics.json  data_catalog.json  data_plan.json
caseset.json  requirement_coverage.json  pending_proposals.json
cases.json     codegen.json  generated/
results.xml    execution.json  report.json  report.md
diagnosis.json  diagnosis_meta.json  repair.json
repair-N.draft.json  repair-N.mock_semantics.json   # 第 N 轮修复产生的补丁
```

`diagnosis.json` 是合并后的 A5/A6 输出（归因 + 可选补丁），`repair.json` 汇总应用/拒绝
记录；`repair-N.*` 是每轮实际落盘的用例草案与 Mock 语义快照，便于复盘。

用例库相关产物：

- `<project>/caseset.json`：项目级版本化用例库；含 `cases`（approved/deprecated）、
  `history`（superseded 旧版本）、`review_log`、`sequence`、`doc_versions`、`requirements`。
  它是普通 JSON，建议纳入项目版本管理；用例变更可通过 diff 审阅，`review_log` 记录批准人。
- 运行目录 `caseset.json`：本轮使用的 store 快照，便于单次运行追溯。
- `requirement_coverage.json`：按叶子 section 的覆盖率与未覆盖原因。
- `pending_proposals.json`：运行前扫描到的未应用 proposal。
- `cases.json`：本轮进入 codegen 的 approved 用例。

门禁口径（与设计文档 C7.4 对齐）：

- **阻塞**：真实缺陷（`code_defect`）、生成代码语法错误、未闭合的 `unmapped`（非 case 模式）、
  case 模式没有 approved 用例、`--fail-on-pending-proposals` 且有待审、
  `--min-requirement-coverage` 未达标、approved endpoint 在 OpenAPI 中缺失且未
  `--allow-stale-cases`。
- **警告**：待审 proposal、OpenAPI/需求文档漂移、需求未覆盖、存在 deprecated 用例、
  未执行、通过率低、contract drift 未校验。
- **单列、不算质量结论**：重试恢复（`recovered_by_retry`）、未执行用例、契约缺失接口、
  文档未覆盖需求。
- 任何“没看到/没判到”的范围必须出现在报告里；**静默通过是最大风险**。

## 关键设计对齐

- **CodeGraph 客户端抽象**：`impact`/`callers`/`callees`/`find_routes` + 扩展的
  `search`/`stats`/`context`/`graph_health`/`symbols_in_file`。S2/S3 只依赖接口，底层可替换。
- **接入方式：代码嵌入，不走 MCP**。常驻 Node worker 直接 `import @colbymchenry/codegraph`，
  经 stdio JSON 调用；MCP 输出面向模型阅读，不能作为结构化事实源。
- **evidence 三级标记**：`verified/inferred/unmapped`。双源一致 → verified，单源 → inferred，
  冲突 → unmapped。CodeGraph 1.6.2 本身不提供诚实标记。
- **路由不直接采信图谱**：CodeGraph 1.6.2 实测存在静默缺陷和缺口 —— Flask
  `url_prefix`、Django `include()` 前缀不拼接；`@app.get/@app.post` 不识别；
  Django 无 method；FastAPI 多层 `include_router` 前缀整个丢失；DRF 只给出
  `VIEWSET /path` 这种伪 method，`url()`、`as_view()`、`router.register` 都不展开。
  `route_repair.py` 用 AST 重建 + 后缀交叉校验，冲突标 `unmapped`；框架约定
  推导出的 method/路径一律标 `inferred`，并用 `handler_owner` 区分同名动作。
- **M1 影响分析三条路径**：变更符号就是 handler（含 ViewSet 类内动作，按
  `handler_owner` 匹配）→ high/verified；否则沿调用链反向闭包 ∩ 路由表 →
  medium/inferred，深度打满降级为 low/unmapped；契约优先项目在两者都失败时
  按 `operationId` 兜底 → low/inferred。
- **非 Python 符号映射不自己造图谱**：Java/Kotlin/TS 的行区间→符号由 CodeGraph
  `getNodesInFile` 提供（worker 方法 `nodes.infile`），AstSight 只做区间相交；
  方法内变更 `verified`，类级/文件级/纯删除兜底 `inferred`。节点 id 一路传给
  `impact(node_id=...)`，避免 Java 同名重载被合并。
- **API 级，不是方法级**：方法只是追溯证据，最终只输出 `changed_apis.json` 的接口列表；
  文件级兜底宁可多报，也不把「追不到」静默当成「没影响」。
- **需求文本是数据，不是指令**：`prompt.md` 明确要求忽略文档中改变角色/输出格式的内容；
  `quote` 必须是 section 原文子串，用于反幻觉校验，但仍需人工确认 oracle 是否正确。
- **审核前不落库**：proposal/review/validation 与原始 draft/context 快照放在 proposal 目录，
  `caseset.json` 只由 `cases approve` 写入。
- **可重放与可审计**：每次运行携带本轮 store 快照；store 的 `review_log` 与 `history`
  记录谁在何时批准了什么。

## 目录

```
engine/
├── astsight/             M1 确定性流水线
│   ├── pipeline.py       路由 → diff → 影响 → 画像 → 用例 → 编码 → 执行 → 报告
│   ├── cli.py            `python -m astsight run|cases|mock ...`
│   ├── artifacts.py      运行产物目录与落盘
│   ├── inventory.py      路由/OpenAPI 发现（run 与 cases prepare 共用）
│   ├── persistence.py    canonical JSON / sha256 / 原子写
│   ├── changes/          git_diff / symbol_diff / impact
│   ├── profile/          OpenAPI 解析与画像构建
│   ├── cases/            需求上下文 / 草案校验 / proposal / case store / 模板用例
│   ├── codegen/          TestCase → pytest
│   ├── execute/          pytest 执行、JUnit 解析、归因
│   └── report/           门禁与 JSON/Markdown 报告
├── contracts/            契约层（全模块共享）
├── codegraph/            CodeGraph 接入层（代码嵌入 + 路由重建）
│   └── bridge/codegraph_worker.mjs
├── orchestration/        LangGraph 编排骨架
├── examples/
│   ├── order_service/    18 接口 FastAPI 示例（含 README、docs/、case_samples/）
│   ├── downstream_service/  报价 → 价格服务 HTTP Mock 示例
│   ├── demo_delta.py     一条命令演示 delta 闭环
│   ├── demo_case_review.py  一条命令演示需求→审核用例→执行
│   └── demo_agent_flow.py   规则→Mock 语义→造数→用例→审核→计划→执行
└── tests/                单元 + 端到端验证
```

## 测试

```bash
cd engine
python -m pytest -q
```

覆盖：

- `test_m1_units.py`：diff 区间、FastAPI 前缀重建（含子模块 router 与 settings
  字面量）、Django `url()`/`as_view()`/DRF router 展开、符号去重、路由清单健康度
  （重复 endpoint、空/未知 method）、OpenAPI 解析、模板用例、生成物 collect
  自检、归因、门禁。
- `test_m1_e2e.py`：真实 git 仓库 + uvicorn + pytest；delta 正常改动 PASS、
  注入 500 后 FAIL 且归因 `code_defect`、full 模式 39 条用例全通过。
- `test_case_context_validation.py`：section 解析与元数据排除、context 确定性、
  草案端点/字段/断言/quote 校验、未覆盖需求、路径穿越与大小限制、文档过期。
- `test_case_assertions_advanced.py`：OpenAPI 响应 schema 内联、`body_schema` 回填与
  运行时校验、`db_row` 目录校验与 SQLite 运行时、header `contains`、断言强度推导与
  `assert_strength.mismatch` warning。
- `test_case_store.py`：fingerprint 口径、版本/编号不变量、六路 diff、批准与拒绝、
  history/superseded、幂等 import、乐观锁、空草案保护。
- `test_case_review_e2e.py`：真实服务上的 prepare → import → approve → run 全链路，
  待审 proposal 的 warning/degraded/blocker 行为。
- `test_case_orchestration.py`：A4.5 审核门前置阻断与 code-only 旁路。
- `test_rules_store.py` / `test_rules_import_diff.py`：`rule_key`/fingerprint 重算、
  A→B 降级、quote 校验、冲突记录、CR 确定性提取、六路规则 diff 与漂移/orphan。
- `test_mock_semantics.py`：契约 `success` 确定性生成、规则映射分支、无依据分支拒绝、
  未知 dep/rule 拒绝、success stub 覆写的证据降级。
- `test_data_plan.py` / `test_data_apply_sqlite.py`：FK 拓扑、状态展开、未知表/列拒绝、
  参数化 SQL、事务回滚。
- `test_data_dialects.py`：多方言 URL 解析与脱敏、SQLite/PostgreSQL/MySQL 的标识符
  引用与占位符、方言不匹配拒绝、MySQL/PostgreSQL DDL 解析。
- `test_data_apply_live.py`：真实 PostgreSQL/MySQL 联调，默认 SKIP；设置
  `ASTSIGHT_TEST_POSTGRES_URL` / `ASTSIGHT_TEST_MYSQL_URL` 后运行。测试会创建
  随机临时表、走 apply/teardown，并在结束时删除，**不要指向生产库**。
- `test_case_fingerprint_v2.py` / `test_agent_rules_flow.py`：fingerprint v2 口径、
  待审阶段不产生 caseset/data_plan/execution、规则与目录快照。
- `test_codegraph_embedded.py`：CodeGraph 嵌入、已知路由缺陷的修复、证据等级，
  以及 Java/Spring 的真实分支 diff → 受影响 API 闭环（合成 Maven 项目）。
  需要 Node >= 22.5 与 `@colbymchenry/codegraph`；先执行
  `python -m astsight codegraph install`，或用 `ASTSIGHT_CODEGRAPH_MODULE_ROOT`
  指定 node_modules，环境不具备时显式 SKIP。

## 下一步

- A1/A4/A5+A6 已接：`prompts/a1_requirement_analysis.md`、`prompts/a4_case_design.md`、
  `prompts/a5_diagnose_repair.md`；下一步是真实需求文档上的规则抽取质量与覆盖评估。
- C5 已打通 SQLite/PostgreSQL/MySQL：下一步补 DB 反射/DBML、跨库 schema 限定名与更复杂的
  跨表业务态；方言只覆盖用例级 fixture，不承担建库/迁移。
- 关系断言：L3/L4 目前需要草案显式写 `$ref`；下一步可基于规则表达式自动生成跨字段/
  跨步骤不变量，并补异步落库的 `db_row` 轮询。
- 文档接入扩展：PDF/Word、跨文档冲突识别。
- 用例库并发写保护与数据库后端（当前为单进程 JSON + 原子替换）。
- 路由插件扩到 Spring（当前 Spring 路由只有 CodeGraph 单源、证据 `inferred`）。
- 仍静态不可知的 Python 路由：Django 函数视图 method（函数体内按
  `request.method` 分支）、运行时动态注册/元类生成的路由、FastAPI 前缀来自
  环境变量或函数返回值的场景。当前策略是保留空 method/不生成并让
  `route_inventory_health` 告警，不猜。
- 类辅助方法的调用链召回：Django `ViewSet.get_queryset`、序列化器方法即使被
  handler 调用，也可能停在 `no_route_reached`；需要把类内辅助方法纳入反向闭包。
- C2 低置信度隔离：设计口径是 `low` 不进入自动用例生成；当前 delta 仍把全部
  `changed_apis` 交给画像/用例层，需补阈值与报告分流。
- C2 召回率/误报率基准：用 20 个真实变更分支人工标注，分别统计 Python 与 Java。
- 增量索引后已删除路由的残留验证（CodeGraph 集成设计未知项 #9）。
- replay 模式：复用历史基线 + 今日已审核用例重判。
