# AstSight 项目进度

> 最后更新：2026-11-17
> 分支：`feat/postgres-mysql-data`（本轮改动已提交并推送，见最近十五个提交）
> 当前阶段：Java 下游契约提取、Feign/服务发现注入与 DTO 继承合并完成；真实
> `spring-cloud-samples/feign-eureka` 工程端到端验证通过；Java HTTP Mock 的
> `agent_rewrite` 已打通 OkHttp / Feign / RestTemplate / Apache HC4 / HC5 +
> HTTPS + 心跳 + 证据，SUT 启动可用 `JAVA_TOOL_OPTIONS` 自动注入 agent；普通
> JVM、HC4+HC5、Spring Boot 3.5.6 fat jar 均联调通过；版本矩阵
> JDK 8/11/17/21/26 × 16 个客户端版本共 80 组合全部通过；Mock 路由在同 host
> 字面量/模板路径并存时字面量优先；Dubbo 消费端 Filter 首版已打通（会话取桩、
> 失败关闭、零真实调用）；Dubbo 画像识别、`dubbo_routes` 计划、自动注入与心跳
> 门禁已打通，`dep_id` 带参数签名支持重载；实测 Dubbo 2.7.22 与 3.2.19 ×
> JDK 8/11/17/21/26 共 10 个组合全部通过；真实 `ReferenceConfig` 消费端代理在
> 普通 classpath 与 Spring Boot fat jar 下都验证了桩返回与零建连；`agent run`
> 的 Dubbo 全链路（HTTP 入口 → Dubbo 下游）已打通：画像 → `dubbo_routes` →
> 自动注入 → 执行 → 报告一次跑完，目标端口零建连；全量测试与 agent e2e
> 结果见“本轮完成”
>
> 2026-11-17 起优先做断言质量：已支持同响应字段关系、受限聚合/算术表达式、
> `db_row.wait_ms` 落库轮询与跨步骤规则关系；规则关系已结构化，并由框架按
> A4 选择的 `relation_key`（含 `$step` 步骤绑定）自动物化为断言。数据目录的
> 结构来源已扩展到 DBML 与真实库（SQLite/PostgreSQL/MySQL）只读反射，
> 并会从注释、DBML Enum note、规则常量、Java/Python/Go/TS 枚举与
> Java `static final`/Go `const`/TS `as const` 常量组确定性地给出取值语义候选
> （每条带 `source`）。`--rule-states` 还能把同一条件下的多表规则约束组合成
> 跨表状态骨架，并把 `transition` 约束和 `from_*/to_*` 成对字段生成
> “迁移前 → 迁移后”的 `target_state` 候选；占位值逐条写入 gaps。公式抽取已有 20 条离线评估集与脚本，v10 基线
> 14/14 严格命中、P/R/表达式准确率均为 1.00；下一步用评估结果决定哪些公式值得
> 进规则层。

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
| 断言深度 | ✅ 2026-11-12 | `header/contains`、`body_schema`（OpenAPI 响应 schema）、只读 `db_row`；`assert_strength` 按内容推导（L1–L4） |
| A5/A6 诊断修复 | ✅ 2026-11-12 | A5/A6 合并为单次 LLM 调用（`schema_version=2`）；默认最多 3 轮；用例补丁以 `astsight-agent:repair` 自动批准；断言不得删除/降强；Mock 结构失配可修并热更新路由表 |
| 多语言下游 Mock | ✅ 2026-11-12 | Java/Go/Node 下游 HTTP 调用点扫描（`profile/langscan.py`）与非 Python 路由扫描（`profile/langroutes.py`）；Spring/Gin/Echo/Chi/Fiber/Express/Fastify/Koa/NestJS；路由 `line` 贯通画像，内联回调按行定位 |
| Java 下游契约 | ✅ 2026-11-13 | `langscan` 抽取 RestTemplate/WebClient 响应类型并识别 Feign 字段调用；`javadeps`/`javaclass`/`javatypes` 读 pom/gradle、本地 jar、class 与泛型签名；画像 dep 增加 `response_types` / `response_contract`，Mock `success` body 按响应类型生成，用例绑定校验会对照契约告警字段漂移；DTO 继承链的父类字段递归合并；服务发现型下游改用 `discovery_override`（Spring Cloud 静态实例），已在真实 `spring-cloud-samples/feign-eureka` 工程跑通端到端 |
| Java HTTP Mock 方案 | ✅ 设计已确认 | 决策记录 §17 / 能力设计 C3.5.5：新增 `agent_rewrite`（配置注入优先、agent 兜底）；agent 只改写 URL 到 `/_dep/<route_key>`，复用内嵌 Mock 的会话/分支/证据；计划内主机未命中一律 502、零透传；HTTPS 仅在 agent 激活时放开；首版不做进程内响应合成 |
| Java agent 版本矩阵 | ✅ 80 组合 | JDK 8/11/17/21/26 × OkHttp 3.14.9/4.12.0/5.5.1、Feign 10.12/11.10/12.5/13.5、HC4 4.3.6/4.4.1/4.5.14、HC5 5.0/5.1/5.2.3/5.3.1/5.4.1/5.6.5；Spring Boot 3.5.6 在 JDK 17/21/26 通过；入口 `test_java_agent_matrix_e2e.py` |
| Java agent 分发闭环 | ✅ 2026-11-16 | `tools/build_agent_wheel.py` → `dist/astsight_java_agent-0.1.0.dev0-py3-none-any.whl`（jar + `manifest.json` + sha256 + RECORD + sidecar）；引擎按 env → wheel → repo 解析并校验版本/校验和/协议号，失败给 `injection_agent_unavailable:*`；`test_agent_jar.py` 12 条 |
| `agent run` 全链路 e2e | ✅ 2026-11-16 | `test_java_agent_flow_e2e.py`：Spring Boot fat jar 经 `run_flow` 完成画像 → 计划 → `JAVA_TOOL_OPTIONS` 注入 → 执行 → 报告；证据 `adapter=okhttp`/`mode=agent_rewrite`/`original_host`，零透传，token 不进命令行；修复 Java HTTPS dep 在画像阶段被丢弃的问题 |
| Mock 路由精确优先 | ✅ 2026-11-16 | 同 host+method 下字面量路径（`/orders/7`）优先于 `{}` 模板（`/orders/{}`）；Python `RouteTable` 与 Java `RouteTableSnapshot` 同规则；多字面量/多模板仍 `mock_route_ambiguous` 失败关闭；全链路 e2e 同时覆盖 OkHttp 字面量与 RestTemplate 模板 |
| HTTP/2 prior-knowledge 负例 | ✅ 2026-11-16 | OkHttp `H2_PRIOR_KNOWLEDGE` 改写后无法与 HTTP/1.1 Mock 完成交换：SUT 显式失败（非 0）、真实下游 0 连接、心跳 `http_rewrites>=1`/`http_passthrough=0`；`test_java_agent_h2_prior_knowledge_e2e.py` |
| Dubbo Filter 首版 | ✅ 2026-11-16 | `java/astsight-agent-dubbo`：消费端 Filter 由 `@Activate(group="consumer")` 进链；Filter 字节在 `ExtensionLoader` 首次加载时用 `ClassLoader#defineClass` 注入应用类加载器，不增强 Dubbo 内部类；取桩走 `POST /_admin/dubbo/invoke`（按 `dep_id` 命中会话绑定），返回体按接口返回类型经 Jackson 构造；同步/异步/声明 future/oneway/泛化都已处理；计划内取桩失败抛 `RpcException`、真实调用 0 次，计划外 dep 放行且无证据；心跳新增 `dubbo` 块 |
| Dubbo 端到端闭环 | ✅ 2026-11-16 | 画像只读源码识别 `@DubboReference`/`@Reference` 与接口签名，无法证明即 `unsupported`；dep 进 `downstream_deps`，计划生成 `dubbo_routes` 并固定 `agent_rewrite` 注入；`agent run` 自动加 `-javaagent` 与 token 环境变量；执行门禁读心跳，缺心跳/Filter 未进链阻塞 `filter_not_in_chain`，`serialization_verified=false` 告警；`dep_id` 带参数签名支持重载；矩阵 Dubbo 2.7.22/3.2.19 × JDK 8/11/17/21/26 共 10 组合通过 |
| Dubbo 真实代理 e2e | ✅ 2026-11-16 | `java/samples/dubbo-consumer` 用 `ReferenceConfig` 直连 URL（`check=false`、`lazy=true`，不启注册中心/provider），经真实消费端代理调用；Python 计数端口断言 accept 与收到字节数均为 0；普通 classpath 与 Spring Boot fat jar 均返回桩、心跳 `filter_instantiated>0`；修复 fat jar 下 `java.lang` 模块打开范围与 bridge 跨加载器可见性 |
| Dubbo 全链路 e2e | ✅ 2026-11-16 | `java/samples/spring-boot-dubbo`（HTTP `/orders/{id}` → `@DubboReference` 字段 → `OrderApi`）+ `test_java_agent_dubbo_flow_e2e.py`：`run_flow` 完成画像 → `dubbo_routes` → `agent_rewrite` 注入（`JAVA_TOOL_OPTIONS` + `token_env`）→ 执行 → 报告；HTTP 响应体为桩数据，证据 `adapter=dubbo-filter`/`mode=agent_filter`，目标端口零建连零字节，门禁 pass（`filter_not_in_chain` 不阻断，`serialization_verified=false` 告警）；注解字段供画像读取，运行期由 `ReferenceConfig` 填同一字段，dep_id 一致 |
| Java HTTP agent P1/P1.5 | ✅ 五客户端已通 | `java/astsight-agent-core` + `java/astsight-agent-http`：premain、admin 路由表、心跳；ASM 织入 OkHttp `newCall`、Feign `SynchronousMethodHandler#targetRequest`、RestTemplate `doExecute`、Apache HC4/HC5 `InternalHttpClient#doExecute`（反射 bridge，规避 fat jar 类加载问题）；Mock 新增 `/_admin/agent/routes`、heartbeat、`/_agent/unmatched`；证据补 `adapter/mode/original_host`；HTTPS 目标经 `allow_https` 校验后改写为明文；计划内改写失败抛 `AgentRewriteException` 失败关闭；`injection_env` 支持 `JAVA_TOOL_OPTIONS` + `ASTSIGHT_AGENT_TOKEN`；普通 JVM + OkHttp、HC 4.5.3 + 5.4.1 同 JVM、Spring Boot 3.5.6 fat jar 三客户端联调均通过 |
| 同响应关系断言 | ✅ 2026-11-17 | `body_field.expect` 新增 `$field` 与受限 `$op`（`len/sum/add/sub/mul/div`）；表达式经白名单 AST 校验，生成器只执行固定运算；同响应关系推导为 L3，两个及以上步骤引用仍为 L4 |
| 落库轮询 | ✅ 2026-11-17 | `db_row.wait_ms`（0–60000ms）显式轮询；默认 0 保持单次查询；每轮结束只读事务，兼容 MySQL `REPEATABLE READ`；延迟写库 SQLite 测试通过 |
| 规则关系模型 | ✅ 2026-11-17 | `Rule.relations[]` / A1 `relations[]`：`$response/$request/$op` 受限配方；`relation_key` 框架计算；`evidence` 必须是规则 quote 连续原文；字面量纳入 grounding |
| 关系自动物化 | ✅ 2026-11-17 | A4 只填 `relation_refs`，框架生成带 `relation_ref` 的 `body_field/db_row` 断言；支持响应、请求、算术、DB where、`wait_ms` 组合 |
| 关系覆盖门禁 | ✅ 2026-11-17 | A/B 规则关系未物化且未列入 `uncovered_requirements[].relation_keys` 时导入阻断；未知键、跨规则误引、目标响应字段未声明均显式报错 |
| 跨步骤规则关系 | ✅ 2026-11-17 | `RuleRelation` 新增 `$step` 语义引用（`"<别名>.<字段>"`）；用例 `relation_bindings` 把别名绑定到更早步骤，框架生成 `$ref` 断言挂到最后一步；缺绑定、绑定最后一步/不存在步骤、单请求用例使用 `$step` 均阻断导入 |
| DB 反射与 DBML | ✅ 2026-11-17 | `data catalog` 支持 `--dbml` 与 `--db-url`（SQLite/PostgreSQL/MySQL 只读反射），与 `--ddl` 三选一；`--schema` 支持多 schema，表名用 `schema.table`；`--include/--exclude` glob 过滤；DBML 支持 Table/Enum/Ref/行内 ref/indexes |
| 受控聚合运算 | ✅ 2026-11-17 | `$op` 新增 `avg/min/max/count_distinct/pluck`，断言与规则关系共用 `OP_ARITY`；`pluck` 只接受单个字段名；空列表/缺字段/类型错误运行期显式失败；`assert_expression_ops` 由白名单生成；A1 提示词升至 9 |
| 公式抽取评估 | ✅ 2026-11-17 | `engine/eval/relation_formulas.json` 20 条标注（14 条公式 + 6 条负例）；`tools/eval_relation_extraction.py` 按目标/运算/来源/字段路径打分；v10 基线 P=1.00 / R=1.00 / expr=1.00（14/14 严格命中、0 误抽、6 条负例全部没补公式）；打分逻辑 6 条单测 |
| 数据目录语义候选 | ✅ 2026-11-17 | `data/annotations.py`：注释关键词补 `semantic_type`；注释「值=含义」、DBML Enum note、`CHECK IN`/`ENUM`、规则 `eq/in` 常量进入 `value_semantics`；表注释写入 `purpose`；冲突候选丢弃并告警；`data catalog --ruleset` |
| 代码枚举常量 | ✅ 2026-11-17 | `data/codeconst.py`：静态扫描 Java/Python/TS 枚举（Python 用 ast，不执行代码）；按数据库枚举值、注释点名、表名+状态/类型命名三种路径映射；`ValueSemantics.source` 记录来源；`data catalog --code`；冲突/多候选/类型不符均告警，不猜 |
| 非枚举常量组 | ✅ 2026-11-17 | 同一扫描器新增 Java `static final`（含 interface 隐式常量）、Go `const`（含 `iota`/简单 `iota` 运算）与 TS `as const` 对象；常量组要求组名以状态/类型/编码等后缀结尾；重复定义取值一致时稳定选一份；`.go` 纳入扫描；测试 473 passed / 37 skipped |
| 跨表状态候选 | ✅ 2026-11-17 | `data/states.py`：同一条件下覆盖 ≥2 张表的 `eq/in` 约束 → `DataState` 骨架；`in` 按组合展开（单组上限 20）；主键/外键/父表/NOT NULL 用确定性占位值补齐，逐条写 `state_placeholder` gap；复用 `validate_plan_fixtures`，不合法候选丢弃并告警；`data catalog --rule-states` |
| 状态转移候选 | ✅ 2026-11-17 | `operator="transition"` 按 `[迁移前, 迁移后]` 生成 `transition_*` 状态：迁移前进 `table_values`、迁移后进 `target_state`；target 同样校验表/列/类型/枚举/NOT NULL；可与同条件另一张表的 `eq/in` 组合；A1 提示词升至 11 |
| 示例端到端验收 | ✅ 2026-11-17 | partner 示例跑通 `catalog --ruleset --rule-states --code` → `data semantics --ruleset` → `data plan`：4/4 plan 生成、0 rejected；修复示例语义草稿的过期 `rule_key`；新增示例资产一致性测试 |
| 同组字段消歧 | ✅ 2026-11-17 | `resolve_field_group`：同一 condition 内用唯一字段收窄同名字段，仍不唯一才告警放弃；规则折叠与状态候选共用；示例 `audit_step` 正确落到 `partner`，`semantic_values` 10 → 12 |
| 成对字段迁移 | ✅ 2026-11-17 | `from_*/to_*`、`old_*/new_*`、`before_*/after_*` 生成 `transition_*` 候选；行内保存两列、`target_state` 声明 to 值；`NULL` 转 null；父表被引用列非主键时补占位值；示例 BR-27 生成 1 条转移候选 |
| 状态造数闭环 | ✅ 2026-11-17 | e2e：规则生成的跨表状态/成对字段状态作为 `preconditions.state_key` → `data plan` → SQLite `apply`/`teardown`；修复 `apply._run` 二次反转 delete 导致外键失败；新增 2 条 e2e 测试 |
| 状态审查命令 | ✅ 2026-11-17 | `data states`：列出 state_key、规则 key、各表行数、target_state、占位 gap 与 fixture 校验错误；`--state` 过滤、`--json`；分类计数 rule/transition/seed |
| 多步迁移路径 | ✅ 2026-11-17 | `DataCatalog.state_paths` + `StatePath/StatePathStep`：同一 (table,column) 的 transition 边按 before=上一步 after 连成路径，最多 5 步/50 条；transition 与 from/to 成对字段都贡献边；`data states` 展示路径；只读描述，不生成 fixtures |
| 迁移后断言 | ✅ 2026-11-17 | `DataPlan.post_assertions`：正例 + `transition_*` + 单表单列 + 非成对字段 + 单列主键且单行时生成只读 `db_row` eq 断言；生成物在用例结束后、teardown 前执行；负例/成对字段/多行/复合主键不生成；测试 503 passed / 37 skipped |

## 未完成事项（截至 2026-11-17）

### 主线相关

- **多步迁移路径仍是描述性产物**：`state_paths` 不能展开成多行 fixtures，
  也不能断言“走到第几步”；需要先定义多行插入/状态更新语义。
- **迁移后断言只覆盖最窄的一类**：正例、`operator=transition`、实体表单列、
  单列主键、单行。未做负例“状态保持不变”、异步落库 `wait_ms`、`from_*/to_*`
  成对字段/审计表、多行状态、复合主键、多步路径中间状态。
- **公式抽取评估未收尾**：只比较目标/运算/来源/字段路径，不比较操作数顺序；
  复杂条件与多分支公式覆盖少；提示词已到 v11，但基线仍是 v10，未用 v11 重跑。
- **README 首屏定位**：把“为什么用 AstSight 而不是 pytest + LLM”搬到 README
  第一屏，仍是路线图短期项。

### 数据与造数

- **PostgreSQL 真库未验证**：按用户要求跳过；MySQL 已实测，PG 反射与造数仍属
  “代码路径完成、未真库验证”。
- **多行/跨表业务态**：每个表只生成一行，没有多行组合、父子多行或整条路径的
  fixtures。
- **接口造数**：只有 SQL 直插，没有“通过被测接口造数”的通道。
- **非枚举代码常量**：Python 模块级常量、Go 单行 `const`、无 `as const` 的 TS
  对象、Java 注解/常量表达式、跨文件常量都未扫描。
- **语义候选仍是启发式**：代码常量映射、注释/DBML/规则冲突处理已做，但复杂
  跨表业务语义仍靠人工。

### Agent / 下游 Mock

- **gRPC 仅 unary**：流式与 server reflection 未做。
- **Dubbo**：2.7.22/3.2.19 × JDK 8/11/17/21/26 已验证；classpath 形态明确不支持，
  更多版本与 sidecar 方案未做。
- **Java agent 未实测清单**：改写后 `Host`/签名/pinning/连接池复用的副作用；
  异步/重试/重定向与二次改写；HTTPS 客户端拒绝 scheme 变化的实际比例；
  `/_agent/unmatched` 的延迟与证据归因。
- **时间窗口测试**：频控窗口、跨月零点、超时仍用“连续请求立即命中”近似，
  没有时钟注入或专用测试接口。
- **replay 模式**：流量回放生成用例未做。
- **被删除接口的变更感知**：未从 base 分支/OpenAPI 建第二份路由清单。

### 长期 / 非目标

- 长期：断言类型注册表、自定义断言插件；更多 Web 框架路由、文档格式、数据库
  方言；报告适配器插件。
- 明确不做：用户管理、用例管理 UI、任务调度；初始用例自动批准。

### 验证缺口

- 全量 **503 passed / 37 skipped**；跳过项主要是真实数据库联调（PostgreSQL）、
  Java/Dubbo 矩阵 e2e（需要本地 JDK/Maven/样例工程）和其它外部服务集成测试。

## 本轮完成（迁移后断言，2026-11-17）

1. **受限生成**：`build_data_plan` 从 `transition_*` 状态的 `target_state` 生成
   `post_assertions`，条件全部满足才生成：
   - 用例是正例；
   - 目标是单表、单列；
   - 目标列不是 `to_*/new_*/after_*` 成对字段；
   - 目标表有单列主键，fixture 恰好一行，能取到主键值；
   - 目标为 null 时列必须可空；
   - 用例已有相同 `target`/`where` 的 `db_row` 断言时不重复添加。
2. **执行**：生成的 `conftest.py` 在 `data_state` fixture 正常结束后（测试体通过）、
   teardown 之前执行 `post_assertions`，用只读 `_DbTools.check`；断言失败先让用例
   失败，然后照常 teardown，不吞掉失败。
3. **可见性**：`data plan` 输出增加 `post_assertions` 计数，计划 JSON 保留完整断言；
   负例、成对字段、多行状态和复合主键不自动断言，`target_state` 仍作为线索。
4. **验证**：新增 4 条测试（正例生成、负例不生成、成对字段不生成、SQLite 真实执行）；
   全量 Python **503 passed, 37 skipped**（上轮 499/37，新增 4 条通过）。
5. **边界**：只支持 `db_row` eq 与单行定位；不处理异步落库（`wait_ms` 固定 0）、
   多行状态、复合主键或负例“状态保持不变”的断言。

## 本轮完成（多步迁移路径，2026-11-17）

1. **契约**：新增 `StatePathStep` / `StatePath`，`DataCatalog.state_paths` 保存
   `path_key`、`table`、`column`、`steps`、`source`、`evidence`；旧目录缺省为空。
2. **建边**：`operator="transition"` 生成 `(table, column, before → after)`；
   `from_*/to_*`、`old_*/new_*`、`before_*/after_*` 成对字段以去掉前缀的 base 名
   作为 column。同一 condition 内先用唯一字段消歧再建边。
3. **连路径**：同一 `(table, column)` 内按 `before == 上一步 after` 连边，最多 5 步；
   按边签名去重，只记录无法继续延伸的路径；起点是没有入边的 before，环形图从
   任意边起步并靠边去重收敛；最多 50 条，超出写 `state_path_limit`。
4. **产物边界**：路径只描述规则推导出的状态流转，不生成 SQL、不展开成多行
   fixtures，也不自动生成断言；C5a 提示词标记为“理解用、不是可执行状态”。
5. **展示**：`data states` 输出 `paths` 和每条路径的 `from → to`、规则 key、
   condition；`--json` 包含 `state_paths`。
6. **验证**：新增 3 条测试（transition 多步路径、成对字段路径、CLI 展示）；
   全量 Python **499 passed, 37 skipped**（上轮 496/37，新增 3 条通过）。

## 本轮完成（状态审查命令，2026-11-17）

1. **命令**：新增 `data states --project ... --catalog ... [--state KEY] [--json]`，
   直接读取目录，不需要重新生成。
2. **内容**：每个状态输出 `state_key`、`rule_keys`、各表行数、`target_state`、
   关联的 `state_placeholder` / `state_value_combinations_truncated` gap，以及
   `validate_plan_fixtures` 的校验错误；顶部汇总 rule/transition/seed 数量。
3. **边界**：只读展示，不修改目录；`--state` 不存在时显式失败；`--json` 便于接平台
   或脚本。占位值原样展示，不隐藏、不包装成已确认数据。
4. **验证**：新增 2 条 CLI 测试（正常列出、未知 state）；全量 Python
   **496 passed, 37 skipped**（上轮 494/37，新增 2 条通过）。

## 本轮完成（状态造数闭环，2026-11-17）

1. **e2e 覆盖**：新增 `tests/test_data_rule_state_apply.py`，两种状态都从规则生成：
   - 跨表 `rule_*` 状态：orders + order_items，含 FK 闭包和 NOT NULL 占位；
   - 成对字段 `transition_*` 状态：events 的 from_status/to_status。
   用例以 `preconditions.state_key` 引用状态，执行 `build_data_plan` →
   `apply_plan` → `teardown_plan`，在真实 SQLite 建表上验证行值、`target_state`
   和清理结果。
2. **发现并修复清理顺序 bug**：`render_teardown` 已按逆拓扑顺序生成 delete（先子表），
   `apply._run` 又 `reversed(deletes)` 一次，导致有外键时先删父表，报
   `FOREIGN KEY constraint failed`。执行层改为按给定顺序删除，并加注释说明契约。
3. **验证**：新增 2 条 e2e；全量 Python **494 passed, 37 skipped**（上轮 492/37，
   新增 2 条通过）。
4. **边界**：`target_state` 仍是描述性的，不会自动生成断言；占位值仍需人工确认；
   不做多步路径或多行组合。

## 本轮完成（成对字段迁移，2026-11-17）

1. **成对字段**：同一张表上的 `from_*/to_*`、`old_*/new_*`、`before_*/after_*`
   约束按 base 名配对，生成 `transition_*` 候选。行内同时保存两列（表示一次迁移
   事件），`target_state` 声明期望的 to 值；`NULL` 转成 null，允许迁移前为空。
2. **FK 父表补全**：被引用父列不是主键时（例如 `partner_audit_log.partner_id`
   引用 `partner.partner_id`，而 `partner` 的主键是 `seq`），也补确定性占位值并写
   `state_placeholder`，不再误报 `fk_parent_value_missing`。
3. **CLI 计数**：`data catalog` 的 `rule_states` 改为读 `sources.rule_states.proposed`；
   之前只统计 `rule_` 前缀，`transition_*` 会被漏算。
4. **示例效果**：partner 示例 BR-27 现在生成 1 条转移候选：
   `partner_audit_log.from_status=null`、`to_status=pending`、`action=submit`，
   `target_state={partner_audit_log.to_status=pending}`；父表 partner 与被引用列
   由占位值补全，所有占位值逐条写入 gaps。
5. **验证**：新增 4 条测试（from/to 成对、NULL 迁移前、FK 父表拉起、枚举不符）；
   全量 Python **492 passed, 37 skipped**（上轮 488/37，新增 4 条通过）。
6. **边界**：只支持单字段成对与单列外键；不做多步路径、多行组合或跨表 from/to
   配对；`from_*` 与 `to_*` 必须落在同一张表，且值只取约束的第一个取值。

## 本轮完成（同组字段消歧，2026-11-17）

1. **消歧规则**：同一 `condition` 内先解析所有约束字段；能唯一映射的字段给出表提示，
   同名跨表字段只在提示表里取候选；收窄到一张表才使用，否则继续视为歧义。
   只做表级收窄，不在候选列之间猜。
2. **规则折叠**：`fold_rule_constants` 按 condition 分组后使用同一消歧逻辑；
   `eq/in` 字段仍多候选才写 `constant_fold_ambiguous`，`between/min/max` 等不折叠
   的字段只参与表提示，不再制造无关告警。
3. **状态候选**：`_propose_one` 与 `_propose_transitions` 同样使用组内消歧；
   收窄后仍多候选才写 `rule_state_field_ambiguous`。
4. **示例效果**：partner 示例的 `audit_step` 同时存在于 `partner` 与
   `partner_audit_log`，但同组 `status`/`audit_status` 唯一指向 `partner`，现在
   正确折叠到 `partner`；目录 `semantic_values` 从 10 增至 12，`audit_step`
   歧义告警消失。
5. **验证**：新增 2 条测试（规则折叠消歧、跨表状态消歧）；全量 Python
   **488 passed, 37 skipped**（上轮 486/37，新增 2 条通过）。
6. **边界**：消歧只依据同 condition 内的字段，不做跨规则推断；同名候选仍多时
   继续告警放弃，不按字段顺序或表名排序硬选。

## 本轮完成（示例端到端验收，2026-11-17）

1. **验收链路**：对 `engine/examples/partner_onboarding` 依次执行
   `data catalog --ruleset --rule-states --code` → `data semantics --draft --ruleset`
   → `data plan --ruleset`。
   - 目录：3 表 / 1 外键 / 10 条规则取值候选 / 1 个代码枚举 / 0 个规则状态；
   - 语义：21 个字段、2 个业务态；
   - 计划：4/4 生成，0 rejected、0 warning。
2. **发现 1（预期行为）**：示例规则的状态约束都落在单表，且 `audit_step` 在
   `partner` 与 `partner_audit_log` 同名，`--rule-states` 正确写
   `rule_state_field_ambiguous:audit_step:2` 并放弃，不猜表。
3. **发现 2（真实问题）**：直接用新目录跑 `data plan` 会把 4 条有 `preconditions`
   的用例标 `state_unknown`。原因是状态来自语义 draft，必须先执行
   `data semantics` 写回目录。使用文档已补充这个顺序。
4. **发现 3（真实问题）**：示例 `data_semantics.draft.json` 的
   `state_partner_registered` 仍引用旧规则 key `RK-dc186a55a422`，带
   `--ruleset` 合并时报 `state_rule_unknown`。已替换为当前 BR-24 的
   `RK-21a1b8ccab98`，语义合并恢复通过。
5. **防回归**：新增 `tests/test_example_assets.py`，校验示例语义草稿引用的所有
   `rule_keys` 都存在于当前示例 `ruleset.json`。
6. **验证**：全量 Python **486 passed, 37 skipped**（上轮 485/37，新增 1 条通过）。

## 本轮完成（状态转移候选，2026-11-17）

1. **转移约束**：识别 `RuleOperator.TRANSITION`，值必须给两个，按 `[迁移前, 迁移后]`
   解释；迁移前值进入 `table_values`，迁移后值进入 `target_state`，生成
   `transition_<rule>_<hash>` 状态。允许单表，也可和同条件下另一张表的 `eq/in`
   约束合并成跨表状态。
2. **target 校验**：`_validate_target` 检查表/列是否存在、值是否标量、类型是否匹配、
   枚举是否闭合；NOT NULL 列不允许写成 null；不通过就丢弃并写 `rule_state_invalid`。
3. **冲突与歧义**：同一字段多个迁移值冲突、字段多表歧义、枚举不符都显式告警并放弃；
   不因为“看起来像状态机”就放宽。
4. **提示词**：A1 明确 `transition` 两个值的顺序；提示词版本 10 → 11。公式抽取基线
   仍以 v10 为准，v11 只影响转移约束的写法，不影响关系抽取。
5. **验证**：新增 4 条测试（单表转移、与另一张表 eq 组合、枚举不符、target 类型不符）；
   全量 Python **485 passed, 37 skipped**（上轮 481/37，新增 4 条通过）。
6. **边界**：不做多步状态机路径、跨状态转移链、多行组合；`from_*/to_*` 两列表达仍按
   普通 `eq` 约束处理，不会自动拼成迁移。

## 本轮完成（跨表状态候选，2026-11-17）

1. **候选生成**：新增 `data/states.py`。按 `(规则, condition)` 分组 `eq/in` 约束；
   字段必须唯一映射到目录列，且同组覆盖至少两张表才生成 `DataState`。
   `in` 按取值组合展开，每条组合一个候选态；单组最多 20 个组合，超出部分写入
   `state_value_combinations_truncated` gap。
2. **确定性补全**：单列主键自动生成（整数 1、文本 `ast_<table>_1`）；单列外键从
   父表行取值，父表不在状态中且外键非空时自动补父表行；其余 NOT NULL 列按类型/
   数据库枚举补占位值。所有占位值逐条写 `state_placeholder` gap，明确不是业务事实。
3. **失败关闭**：复合主键/外键、循环依赖、字段歧义、枚举不符、类型不符、FK 闭包
   不完整都丢弃候选并写 `rule_state_invalid` / 对应 warning，不把不合法状态写进目录。
4. **接入**：`build_catalog` 新增 `rule_states` 参数；CLI `data catalog --rule-states`
   （必须同时给 `--ruleset`）；输出 `rule_states` 计数，目录 `sources.rule_states`
   记录候选数与占位数；C5a 提示词说明 `rule_*` 状态是骨架，占位值不是业务事实。
5. **多值组合**：`in` 约束按笛卡尔积展开，每条组合一个候选态；单组上限 20，
   超出写 `state_value_combinations_truncated` gap，不再只取第一个值。
6. **验证**：新增 8 条测试（跨表状态与 FK 闭包、单表不生成、字段歧义、枚举不符、
   非法候选可见、CLI、多值组合展开、组合上限）；全量 Python
   **481 passed, 37 skipped**（上轮 473/37，新增 8 条通过）。
7. **边界**：不做状态机路径、多行组合、跨状态转移；只支持单列主键/外键；
   占位值必须人工确认或替换。

## 本轮完成（非枚举常量组，2026-11-17）

1. **Java `static final`**：解析类/接口中的字面量常量，支持行尾注释作为含义；
   `1L`、`LoggerFactory.getLogger(...)` 这类无法静态求值的声明直接跳过，不猜。
   interface 字段即使不写 `static final` 也按常量处理，但要求常量名全大写。
2. **Go `const` 块**：支持类型标注、无初始化继承上一表达式、`iota`、`iota ± N`、
   `iota × N`、`N × iota`；常量组按文件 stem 命名（`order_status.go` → `OrderStatus`），
   同一文件多个 const 块合并；多名字/复杂表达式（如 `1 << iota`）跳过并保持 iota 序号。
3. **TS `as const` 对象**：解析扁平的 `NAME: 字面量` 属性与行尾注释；没有 `as const`
   的普通对象不当成常量组；嵌套对象、计算属性跳过。
4. **信噪比控制**：常量组只有组名以 `status/state/type/kind/code/enum/category/level/
   flag` 或其复数结尾时才参与映射，避免超时、日志、序列化等配置常量被当成业务取值。
5. **重复定义**：同一列命中多个代码来源、取值集合完全一致时，按路径稳定选一份，
   不再报歧义；取值不一致才 `code_enum_ambiguous` 放弃。
6. **验证**：新增 3 条测试（三类常量组扫描、Go 常量组折叠、重复定义不误报歧义）；
   全量 Python **473 passed, 37 skipped**（上轮 470/37，新增 3 条通过）。
7. **未做**：不执行代码，不解析 `A = B + 1`、`1 << iota`、注解或跨文件常量引用；
   Python 模块级常量未扫描；这些继续保持为边界，不用正则硬猜。

## 本轮完成（公式抽取评估 v10，2026-11-17）

1. **提示词纠偏**：A1 关系约束明确区分 `$request`（本次请求字段）与 `$step`
   （另一个动作的**响应**字段），并写清当前表达式没有“上一个动作的请求字段”节点；
   遇到“创建请求里的字段”这类表述必须写 `gap`/`question`，不能用 `$step` 冒充。
   增加百分比、混合运算、条件公式的处理边界：条件能折叠成常量才输出关系，
   “VIP 打九折”这类没有具体字段的规则写 gap，不自补系数。A1 提示词版本 9 → 10。
2. **评估集扩展**：从 10 条扩到 20 条，新增条件公式（优惠券减免）、百分比佣金、
   `price * quantity - discount` 混合运算、跨步骤减请求字段、请求字段相乘、
   两个列表求和，以及 4 条新负例（上一个动作请求字段、VIP 折扣、按地区运费、
   满减无字段）。原有“响应与创建请求一致”改写为“与本次请求一致”，避免语义歧义。
3. **打分更严**：关系签名新增 `paths`，比较 `response:/request:/step:` 字段路径；
   `$step` 只比较字段部分、忽略 A4 才绑定的步骤别名，避免惩罚等价别名。
   现在会抓“来源类型对但字段引错”的情况。
4. **基线结果**：DeepSeek + A1 提示词 v10 跑 20 条，**14/14 关系严格命中，
   P=1.00 / R=1.00 / expression_accuracy=1.00，0 误抽，6 条负例全部没有补公式**。
   原始结果在 `engine/eval/baselines/2026-11-17-deepseek-flash-prompt-v10.json`；
   上一版 10 条 v9 基线保留在 `2026-11-17-deepseek-flash.json` 便于对比。
5. **验证**：评估打分测试 6 条通过（新增字段路径错例）；全量 Python
   **470 passed, 37 skipped**。文档同步 C1 使用文档、决策记录（v4.31）与 ROADMAP。
6. **边界**：评估仍不比较表达式操作数顺序（如 `sub(a,b)` 与 `sub(b,a)`），只比较
   目标、关系运算、运算集合、来源类型与字段路径；操作数顺序要靠规则原文人工审核。

## 本轮完成（代码枚举常量，2026-11-17）

1. **扫描范围**：新增 `data/codeconst.py`，静态读取 Java `enum`、Python
   `Enum/IntEnum/StrEnum/Flag` 与 TypeScript `enum`。Java 取构造参数首个字面量，
   无参时用常量名；Python 用 `ast`，不执行代码；TS 支持字符串/数字初始化，
   无初始化按 TS 规则顺延编号。支持枚举项行尾注释作为含义（如 `PENDING(0), // 待支付`）。
2. **扫描边界**：默认跳过 `node_modules`、`target`、`build`、`dist`、`.venv`、
   `__pycache__` 等目录；单文件 1MB、5000 文件、2000 枚举为上限；注释或字符串里的
   `enum` 不会被误认成声明；无法解析的文件/路径进 `warnings`。
3. **映射规则**：只有三种可解释路径——数据库枚举值全部能在代码枚举中找到（最强）、
   列注释明确写出枚举类型名、或「表名 + status/type/code」命名唯一对应。
   多候选先按取值类型与列类型亲和度排序（如 TEXT 列优先字符串枚举），仍并列则
   `code_enum_ambiguous` 放弃，不猜表。
4. **取值与失败关闭**：数据库枚举存在时只给已有值补含义，缺的值写
   `code_enum_value_mismatch`；数值编码按列类型转换，无法转换写
   `code_enum_value_unsupported`；代码枚举只补含义，不覆盖注释/DBML/规则的既有含义。
5. **来源可追溯**：`ValueSemantics` 新增可选 `source`（`comment` / `dbml` / `enum` /
   `rule` / `llm` / `code:<相对路径>`），旧目录缺省为空串；模型补全只能沿用已知来源
   或标 `llm`，不能伪造 `code:` 来源；目录 `sources.code_enums` 记录扫描文件、枚举、
   实际使用与折叠条数。
6. **CLI 与提示词**：`data catalog --code`（可重复），输出增加 `code_enums` /
   `code_files` 计数；C5a 提示词输入包含 `source`，并说明代码枚举候选可修正但
   不得与数据库枚举冲突。
7. **验证**：新增 8 条测试（三种语言扫描、路径缺失、按名/按值/冲突/歧义映射、CLI、
   模型伪造来源被降为 `llm`）；全量 Python **470 passed, 37 skipped**（上轮 462/37，
   新增 8 条通过）。
8. **未做**：不执行代码，不解析注解/常量表达式（如 `A = B + 1`），不做跨语言枚举
   合并；非枚举的类常量（`static final`）与 Go 常量暂不扫描，已写入 ROADMAP。

## 本轮完成（受控聚合与语义候选，2026-11-17）

1. **受控聚合与投影**：`cases/expressions.py` 的 `OP_ARITY` 新增
   `avg/min/max/count_distinct/pluck`；`pluck` 把列表元素的单个字段投影成列表，
   再交给聚合运算（如「总价 = sum(items.price)」「均分 = avg(reviews.score)」）。
   `pluck` 第二参数只接受字段名；表达式、路径、动态节点均报
   `expression.pluck_field_invalid` / `relation.pluck_field_invalid`。
2. **执行与失败关闭**：生成的 `conftest.py` 只执行固定运算；空列表、非列表、
   `pluck` 缺字段、类型错误都抛 `AssertionError`，不静默跳过。
   `count_distinct` 按类型区分（`True` 与 `1` 不同），字典/列表按 JSON 内容归一。
   强度推导不变：同响应关系 L3，引用两个及以上步骤 L4。
3. **可发现性**：`context.assert_expression_ops` 改为直接由 `OP_ARITY` 生成，避免
   文档与实现漂移；A1/A4 提示词补充聚合与 `pluck` 示例，并明确“文档没给字段就写
   gap，不要猜”；A1 提示词版本 8 → 9。
4. **公式抽取评估**：新增 10 条中文公式标注（求和、均价、最大、去重数量、长度、
   除法、请求回显、跨步骤一致，以及 2 条“没有公式”的负例）。
   `tools/eval_relation_extraction.py` 逐条跑 A1，按「目标字段 + 关系运算 +
   表达式运算集合 + 取值来源（response/request/step）」打分，输出
   precision/recall/expression_accuracy 与漏抽、误抽、表达式选错。
   评估真实调用模型，不随测试自动执行。2026-11-17 用当前 DeepSeek 配置跑基线：
   **8/8 关系全部召回，precision 1.00 / recall 1.00 / expression_accuracy 0.90**，
   0 误抽；唯一不严格项是 `echo_request_order_id` 把“创建请求里的字段”写成了
   `$step` 而不是 `$request`。基线原始结果在
   `engine/eval/baselines/2026-11-17-deepseek-flash.json`（含模型参数与逐条明细）。
5. **C5a 确定性语义候选**：新增 `data/annotations.py`。注释关键词补结构语义；
   注释里的 `0=待支付`、`PAID=已支付`、`待支付(0)` 等解析为取值语义；DBML
   `Enum` 值上的 `[note: '...']` 同步进入 `value_semantics`；`CHECK IN`/`ENUM`
   与规则 `eq/in` 常量折叠为列候选，保留 `rule_keys`。表注释写入 `purpose`。
6. **冲突与边界**：注释/规则取值与数据库枚举冲突时丢弃并写入
   `comment_value_unknown` / `constant_fold_enum_mismatch`；同名列跨表出现时放弃
   规则折叠并告警 `constant_fold_ambiguous`，不猜表。所有候选证据固定为
   `inferred`，不自动升级；业务含义仍以需求原文和人工审核为准。
7. **CLI 与提示词输入**：`data catalog` 新增 `--ruleset`，输出增加
   `semantic_values` 计数；C5a 提示词输入补 `value_semantics` 与 `purpose`，并说明
   候选可修正但不得与数据库枚举冲突。
8. **验证**：新增 7 条标注/目录测试、3 条断言测试、1 条 CLI 测试、6 条评估打分测试；
   全量 Python **462 passed, 37 skipped**（上轮 445/37，新增 17 条通过）。
   文档同步 README、使用文档、C1 文档、能力设计、决策记录（v4.29）与 ROADMAP。
9. **过程说明**：评估脚本会真实调用模型。首次试跑时按当前环境的 `ANTHROPIC_*`
   配置触发过一次非预期调用，已立即终止；随后显式运行评估集得到上述基线。
   评估结果只代表当前模型与提示词版本，不写成通用结论。

## 本轮完成（DB 反射与 DBML，2026-11-17）

1. **结构来源三选一**：`data catalog` 新增 `--dbml` 与 `--db-url`；三条入口都转成
   `ParsedDDL`，复用同一套 catalog 构建、外键拓扑与校验。
2. **真实库反射**：`data/reflect.py` 只读 `sqlite_master`+PRAGMA / `pg_catalog` /
   `information_schema`，提取表、列、主键、唯一约束（含唯一索引）、外键、注释与枚举；
   不导出业务数据。SQLite 的 `REFERENCES parent`（省略列名）会回退到父表主键。
3. **跨 schema**：`--schema` 逗号分隔；多 schema 时表名限定为 `schema.table`，
   `render.py` 的标识符引用、`apply.py` 的语句白名单与表存在性检查同步支持限定名。
4. **过滤**：`--include/--exclude` 按表名 glob 过滤，过滤后同步丢弃悬空外键并告警；
   复合唯一约束与表达式索引不展开到单列，显式 `unique_composite_ignored` warning。
5. **DBML**：新增 `data/dbml.py`，支持 `Table`/`Enum`/`Ref:`/行内 `[ref: > t.c]`/
   `indexes`（单列唯一与复合 PK/唯一）/`Note:`；未知表/列/块形式只告警不猜。
6. **连接复用**：抽出 `data/connection.py`，反射与造数共用同一套连接与驱动错误
   （`db_driver_missing`）。
7. **验证**：新增 DBML、SQLite 反射、CLI 入口测试与 PG/MySQL 真库测试（默认 skip）；
   聚焦 8 条通过；全量 Python **445 passed, 37 skipped**（新增 8 条通过、
   真库 2 条默认跳过），与上轮基线 437/35 的差值正好对应本轮新增。
8. **真库实测**：本机 MySQL 26.7.0（`astsight_test` 库）跑通
   `test_data_reflect_live.py` 的 MySQL 用例；CLI `data catalog --db-url` 对真实库
   反射出 2 表 / 1 外键，主键、唯一约束与可空性正确，父表排在子表前；catalog
   `sources.display` 已脱敏为 `root:***`，不导出业务数据，临时表用后即删。
   PostgreSQL 无实例，仍为未验证。

## 本轮完成（跨步骤规则关系，2026-11-17）

1. **规则侧**：`relations[].expect/where` 新增 `{"$step": "create.order_id"}`，别名只表达
   语义，不绑定具体步骤；别名必须是 `[a-z][a-z0-9_]{1,63}`，字段路径复用现有校验。
   `$step` 不需要原文 grounding 字面量，`relation_key` 仍由内容计算。
2. **绑定与物化**：用例新增 `relation_bindings: {relation_key: {别名: 步骤名}}`；
   框架校验别名完整、无多余别名、绑定步骤存在且严格早于承载断言的最后一步，
   再把 `$step` 转成 `{"$ref": "步骤.字段"}`。跨步骤断言固定挂在用例最后一步，
   强度按内容推导为 L3（多个步骤引用为 L4）。
3. **失败关闭**：缺少绑定、绑定到最后一步、绑定不存在步骤、给不含 `$step` 的关系写绑定、
   绑定未在 `relation_refs` 中选择的关系、单请求用例引用 `$step`，全部阻断导入并给出定位。
4. **提示词与文档**：A1 提示词升至 8，说明何时用 `$step`；A4 提示词说明
   `relation_bindings` 与“查询步骤放最后”的约束；使用文档、能力设计、
   决策记录（v4.27）、路线图同步。
5. **验证**：新增规则侧 `$step` 校验、跨步骤物化与代码生成、绑定错误与单请求用例负例；
   全量 Python **437 passed, 35 skipped**；示例规则库仍可加载（67 BR / 10 CR），
   示例 caseset 仍为 38 approved。

## 本轮完成（规则关系自动物化，2026-11-17）

1. **规则契约**：新增 `RuleRelation`，A1 可输出 `relations[]`；关系 identity 为
   `RR-<sha256>`，`assertion_kind` 支持 `body_field/db_row`，表达式只允许
   `$response/$request/$op`，运算继续复用 `len/sum/add/sub/mul/div` 白名单。
2. **原文证据**：`relations[].evidence` 必须能在同一 `source.quote` 中逐字找到；
   表达式和 where 中的字面量进入规则 grounding，不在原文中的值继续被视为编造。
3. **自动生成断言**：A4 不再手写关系表达式，只在用例 `relation_refs` 中选择规则关系；
   框架确定性生成断言并写入 `relation_ref`。`$response` 转当前响应 `$field`，
   `$request` 在生成代码中解析为请求字段，DB 关系可同时引用响应与请求。
4. **覆盖对账**：A/B 规则关系必须至少被一条用例物化，或按 `relation_key` 列入
   `uncovered_requirements` 并说明原因；漏列、未知键和把已覆盖关系重复声明为未覆盖均可见。
5. **A4 输入与审核**：A4 prompt 输入包含规则 relations；A1 提示词版本升至 7；
   `ruleset.md` 新增关系断言表；A4 提示词、使用文档和能力设计同步。
6. **验证**：新增规则关系、自动物化、请求引用、DB 一致性关系和未覆盖门禁测试；
   全量 Python **432 passed, 35 skipped**。

## 本轮完成（断言关系表达式与落库轮询，2026-11-17）

1. **关系表达式**：新增 `cases/expressions.py`，支持当前响应字段 `$field`、前序步骤
   `$ref` 和受限运算 `len/sum/add/sub/mul/div`；拒绝任意 Python、未知 op、错误参数个数、
   深层/超大表达式，以及请求体、query、path 参数和 header 中的动态节点。
2. **代码生成**：`body_field.expect` 中的关系式生成 `step_tools.expr(...)`；纯 `$ref`
   保持原生成路径，避免既有用例资产形态漂移。运行时缺失字段/运算类型错误显式失败。
3. **强度推导**：同响应字段关系和受限运算计入 L3；表达式同时引用两个及以上不同步骤
   仍推导为 L4。
4. **落库轮询**：`Assertion.wait_ms` 持久化进 draft/caseset；`db_row` 在等待期内重查，
   超时报错带实际等待时长。`wait_ms` 用于非 `db_row` 断言时校验失败。
5. **A4 可发现性**：上下文新增 `assert_expression_ops`，A4 提示词、使用文档、能力设计
   与路线图同步新增语法、边界和异步落库说明。
6. **验证**：聚焦断言测试 16 passed；全量 Python **428 passed, 35 skipped**
   （1 个既有 PytestCollectionWarning，无失败）；示例 `caseset.json` v4 可加载
   （38 approved / 0 deprecated / 18 history），未因新增字段改变指纹。

## 本轮完成（`agent run` Dubbo 全链路 e2e，2026-11-16）

1. **SUT 样例**：新增 `java/samples/spring-boot-dubbo`（Spring Boot 3.5.6 +
   Dubbo 2.7.22）。HTTP 入口 `GET /orders/{id}` 调用 `@DubboReference` 字段
   声明的 `OrderApi#create(String)`；`--dubbo.url` 指向测试侧计数端口。
   注解字段承担画像读取，运行期由 `ReferenceConfig` 直连 URL 填同一个字段
   （Dubbo 2.7.x 的注解处理依赖 Spring 4/5 集成，与 Spring Boot 3 不兼容），
   接口/group/version 与注解一致，dep_id 与画像产出相同。
2. **e2e**：新增 `engine/tests/integration/test_java_agent_dubbo_flow_e2e.py`。
   脚本化模型响应驱动 `run_flow`，一次完成规则 → Mock 语义 → 用例草案 →
   `dubbo_routes` 计划 → Mock 启动 → SUT 启动（`JAVA_TOOL_OPTIONS` +
   `token_env=ASTSIGHT_AGENT_TOKEN`）→ pytest → 报告。
3. **断言**：注入为 `agent_rewrite`（原因
   `dubbo_filter_requires_java_agent`）；`mock_plan.json` 的 `dubbo_routes`
   与画像 dep_id 一致；HTTP 响应体断言 `orderId`/`amount` 来自桩；证据
   `adapter=dubbo-filter`/`mode=agent_filter`/`original_path=dubbo:...#create`；
   目标 Dubbo 端口 accept 与字节数均为 0；心跳 `installed` / `version` /
   `filter_instantiated>0` / `serialization_verified=false`；门禁 pass，
   `filter_not_in_chain` 不在阻塞项，`serialization_verified=false` 在告警中；
   `sut.log` 有 `Picked up JAVA_TOOL_OPTIONS` 与 `token_env=`，无 `token=`。
4. **心跳节拍**：默认 5s 一拍，报告读到的 `filter_hits` 可能落后于本次调用，
   因此 e2e 不用它判定“本次命中”；调用凭据以 `mock_calls.jsonl` 证据行为准，
   门禁本身只要求 `filter_instantiated>0`。
5. **门禁复核窗口**：`_fetch_agent_heartbeat` 增加
   `wait_for_dubbo_filter`：计划含 Dubbo 路由且心跳显示 `installed=true`、
   `filter_instantiated=0` 时，最多轮询 6s 再下结论。消除“Filter 刚进链、
   还没到下一拍心跳”被误判为 `filter_not_in_chain` 的竞态；超时仍按阻断
   处理，不放宽门禁。新增 4 条单测（等待到新值、无 Dubbo 计划不等待、
   agent 消失即返回、模块未装不等待），`tests/test_dubbo_gates.py`。
6. **测试结果**：新增 e2e 1 条在 JDK 17 与 JDK 21 上均通过；样例
   `mvn package` 构建通过（fat jar）；全量 Python **423 passed, 35 skipped**。
7. **文档**：`java/README.md`、`engine/README.md`、根 `README.md`、
   `doc/决策记录.md`（新增 v4.24）、`doc/framework-capability-design.md`
   同步全链路验证范围、样例说明与门禁复核窗口。

## 本轮完成（Dubbo 真实消费端代理 e2e，2026-11-16）

1. **真实代理样例**：新增 `java/samples/dubbo-consumer`（`OrderApi` /
   `OrderView` / `DubboConsumerSample` + 独立 pom）。用 `ReferenceConfig`
   直连 `dubbo://127.0.0.1:<计数端口>`，`check=false`、`lazy=true`，不启
   注册中心与 provider；`mvn package` 同时产出普通 classpath 依赖清单
   `target/consumer-classpath.txt` 和 Spring Boot fat jar。样例打印 `loader=`
   与 `filter_loader=`，便于核对 Filter 由哪个加载器定义。
2. **e2e**：新增 `engine/tests/integration/test_java_agent_dubbo_consumer_e2e.py`
   2 条（普通 classpath / Spring Boot fat jar）。Python 侧在目标端口监听，
   断言 accept 次数与收到的字节数都是 0；两条都返回桩 `O-1,10`、心跳
   `filter_instantiated>0`、证据 `adapter=dubbo-filter`，并核对 Filter 与
   应用类加载器一致。默认跳过，启用条件与既有 Dubbo e2e 相同。
3. **修复 fat jar 类加载**：`DubboClassInjector` 在 JDK 9+ 除了把 `java.lang`
   打开给 agent 自己，还打开给应用加载器的 unnamed module。修复前 fat jar 下
   Dubbo 2.7.22 的 javassist 编译直接失败（`InaccessibleObjectException`）。
4. **修复跨加载器访问**：`DubboBridge.filterInstantiated` / `intercept` 改为
   `public`。Filter 由 `LaunchedClassLoader` 定义、bridge 在 agent 加载器，
   两者运行时包不同，包级可见会抛 `IllegalAccessError`。
5. **测试结果**：Dubbo e2e 7 条通过（新增 2 + 原有 5）；新增的 2 条在 JDK 17
   与 JDK 21 上各跑一次均通过；Dubbo 版本矩阵 2.7.22/3.2.19 × JDK
   8/11/17/21/26 共 10 组合重跑通过；Java 单测通过；全量 Python
   **419 passed, 34 skipped**（新增 2 条纳入默认跳过）。
6. **文档**：`java/README.md`、`engine/README.md`、`doc/决策记录.md`（新增
   v4.23）、`doc/agent-architecture.md`、`doc/framework-capability-design.md`
   同步真实代理验证、模块打开范围与 bridge 可见性约束。

## 本轮完成（Dubbo 画像/计划/门禁/重载/矩阵，2026-11-16）

1. **画像识别**：新增 `engine/astsight/profile/dubbo.py`，只读源码扫描
   `@DubboReference` / Dubbo `@Reference` 字段；接口源码必须在仓库内，方法
   参数类型要能解析到 JVM 名（`java.lang.String`、`int`、`java.lang.String[]`）。
   接口只在依赖 jar、方法找不到、同名重载无法区分、参数解析失败时，分别显式
   记 `dubbo_interface_in_jar` / `dubbo_method_unresolved` /
   `dubbo_overload_ambiguous` / `dubbo_parameter_invalid`；XML 与 Kotlin 引用
   分别记 `dubbo_xml_reference_unsupported` / `dubbo_kotlin_reference_unsupported`，
   不猜。扫描结果由 `DownstreamScanner.scan_dubbo()` 按项目缓存，并附到每个
   画像的 `downstream_deps`。
2. **计划与注入**：`MockPlan` 新增 `dubbo_routes` 与 `MockPlanDubboRoute`；
   计划校验要求 `dep_id` 是带参数签名的规范形态、同一路由不重复；Dubbo 依赖的
   注入固定为 `agent_rewrite`（原因 `dubbo_filter_requires_java_agent`），
   `agent run` 据此把 `JAVA_TOOL_OPTIONS` 与 `ASTSIGHT_AGENT_TOKEN` 注入 SUT
   环境，admin token 不出现在命令行。运行期 `add_dubbo_dep` 与语义热更新同步
   维护 `dubbo_routes`。
3. **执行门禁**：`pipeline` 在执行结束后取 `GET /_admin/agent/heartbeat`，写入
   `mock_evidence.json` 的 `agent` 块；`report/merge.py` 在计划含 Dubbo 路由时
   检查心跳，缺心跳、`dubbo.installed=false` 或 `filter_instantiated=0` 记为
   阻塞项 `filter_not_in_chain`，`serialization_verified=false` 记为告警；
   `mock_not_used` 继续按会话绑定与实际证据比对；报告新增 `## Dubbo Filter`
   段落。
4. **重载与 `dep_id`**：`dep_id` 改成
   `dubbo:<接口>#<方法>(<参数类型,...>)?group=<g>&version=<v>`，参数类型用
   JVM 名、无参数写 `()`。Java `DubboBridge` 产出签名（泛化调用取 `args[2]`），
   Python `identity.build_dubbo_dep_id` / `parse_dubbo_dep_id` 统一校验，Mock
   server 与计划校验拒绝无签名的旧形态；样例新增 `create(String, int)` 重载与
   `overload` 模式。
5. **版本矩阵**：新增 `engine/tests/integration/test_java_agent_dubbo_matrix_e2e.py`，
   默认跳过；启用后对每个 JDK × Dubbo 版本编译并运行 `DubboFilterSample`，
   核对 `filter=...AstSightDubboFilter`、桩结果、真实调用 0 次、心跳版本与
   `serialization_verified=false`。实测 Dubbo 2.7.22 与 3.2.19 分别在
   Temurin JDK 8.0.512 / 11.0.33 / 17.0.21 / 21.0.13 与本机 OpenJDK 26 上
   通过，共 **10 个组合**。首次运行经 Maven 下载依赖，缓存在
   `~/.astsight/java-agent-dubbo-matrix/`。
6. **Java 构建与测试**：重新执行
   `mvn -pl astsight-agent -am -DskipTests clean package`；`DubboBridgeTest`
   修正一处方法名写错并验证参数签名/重载区分；Java 单测通过。
7. **Python 测试**：新增 `tests/test_profile_dubbo.py`（6 条）与
   `tests/test_dubbo_gates.py`（4 条）；`tests/test_mock_identity.py` 与
   `tests/test_mock_dubbo.py` 补参数签名与重载用例；Dubbo e2e 5 条通过；
   矩阵 10 条通过；全量 **419 passed, 32 skipped**（默认跳过的 agent e2e 与
   矩阵不计入 passed）。
8. **文档**：`java/README.md`、`engine/README.md`、`doc/决策记录.md`
   （新增 v4.22）、`doc/framework-capability-design.md`、
   `doc/agent-architecture.md` 同步新的 `dep_id` 形态、画像/计划/门禁与矩阵
   结论；矩阵外版本仍按未验证处理。

## 本轮完成（Dubbo 消费端 Filter 首版，2026-11-16）

1. **模块**：新增 Maven 模块 `java/astsight-agent-dubbo`，`dubbo-rpc-api:2.7.22`
   为 `provided`（运行期由 SUT 提供，不随包分发）；`provider` 侧不做任何事。
   Filter 通过 `META-INF/dubbo/org.apache.dubbo.rpc.Filter`
   （`astSightMock=io.astsight.agent.dubbo.AstSightDubboFilter`）注册，
   `@Activate(group = "consumer")` 激活。
2. **类加载**：agent jar 挂在系统类加载器，Filter 必须在应用类加载器里才认得出
   Dubbo 类型。实现为监听 `org/apache/dubbo/common/extension/ExtensionLoader`
   的加载，反射调用该加载器的 `ClassLoader#defineClass` 定义 Filter（JDK 9+ 先
   `Instrumentation.redefineModule` 打开 `java.lang`）。没有用
   `URLClassLoader#addURL`（父优先会选到系统类加载器）、动态代理（无法经 SPI
   注册）或增强 Dubbo 内部类（设计禁止）。
3. **桥接**：注入的 Filter 只引用 Dubbo 类型和系统类加载器里的 `DubboBridge`；
   bridge 对 `Invoker`/`Invocation`/`URL`/`Result`/`AsyncRpcResult` 全走反射，
   避免 `NoClassDefFoundError`。
4. **取桩入口**：Mock 新增 `POST /_admin/dubbo/invoke`，请求
   `{sut_id, dep_id, service, method, parameter_types, generic}`，响应
   `{schema_version, route_key, dep_id, branch, body, delay_ms}`；错误码沿用
   `mock_dep_unplanned`（404）、`mock_unexpected_dep`、`mock_branch_not_found`、
   `mock_session_missing/expired/late_call`、`mock_response_invalid`。
   `dep_id` 形如 `dubbo:接口#方法(参数类型,...)?group=&version=`（v4.22 起
   必带参数签名，无参数写 `()`）。返回体按接口
   方法的 `getGenericReturnType()` 用 Jackson 转换，转换失败即
   `mock_response_invalid`；泛化调用按 `Map`/`Object` 处理。
5. **调用形态**：同步、老式异步（`async=true` attachment）、声明
   `CompletableFuture<T>`、oneway（`return=false`）与泛化
   `$invoke`/`$invokeAsync` 都已处理。Filter 在 `AbstractInvoker` 之前短路，
   所以 bridge 自己补设 `RpcContext` future；声明 future 时补设
   `InvokeMode.FUTURE` 并按 `T` 构造桩体；oneway 返回空结果，但仍消费桩、
   写证据。
6. **放行边界**：路由快照已加载且该 dep 不在计划内 → 调用下一个 Invoker（真实
   调用，不产生 Mock 证据）；计划内任何取桩错误、admin 不可达 → `RpcException`，
   真实调用 0 次；agent 未激活 → `agent_not_active`，同样失败关闭。
7. **证据与心跳**：证据写 `mock_calls.jsonl`，`adapter=dubbo-filter`、
   `mode=agent_filter`、`method` 为 RPC 方法名、
   `original_path=dubbo:接口#方法`。心跳新增 `dubbo` 块：
   `installed / version / filter_instantiated / filter_hits / passthrough /
   errors / serialization_verified`（固定 `false`）。
8. **测试**：
   - `engine/tests/test_mock_dubbo.py`：7 条（成功体 + 证据 + `dubbo_deps`、
     空体为 null、未绑定、分支缺失、会话缺失、计划外 404、`raw_base64` 非法）。
   - `engine/tests/integration/test_java_agent_dubbo_e2e.py`：4 条，默认跳过，
     `ASTSIGHT_JAVA_AGENT_E2E=1` 启用；不启注册中心/provider，用 Dubbo SPI 取
     Filter + 计数假 Invoker，分别验证取桩成功（`result=O-1,10`、`REAL_CALL`
     不出现）、未绑定（`error=astSight:mock_unexpected_dep`、`real=0`）、计划外
     放行（`REAL_CALL`、`real=1`、无证据），以及异步/声明 future/oneway/
     泛化四种形态（各自命中桩、真实调用 0 次）。
   - Java 单测：`RouteTableSnapshotTest#dubboDepsAreParsedForPlannedChecks`、
     `DubboBridgeTest` 2 条（`dep_id` 参数顺序与空参数省略）。
   - 回归：全量 Python **404 passed, 29 skipped**；Dubbo e2e 4 条通过；
     Java 单测（core + dubbo 模块）通过。
9. **构建**：`mvn -pl astsight-agent -am -DskipTests clean package` 后
   `python tools/build_agent_wheel.py --skip-build` 刷新 sidecar/wheel。注意
   只跑 `mvn package` 时 shaded dist jar 可能仍是旧的，联调前先 clean。
10. **当时未验证**：JDK 8/11/17/21 与 Dubbo 3.x 组合（v4.22 已补实测，
    见上一节）。classpath + `dubbo.consumer.filter` 形态与本地心跳文件经评估
    **明确不做**：classpath 形态没有 `premain`，只能挂 Dubbo Filter、覆盖不了
    HTTP，收益不足以支撑额外支持面。矩阵外组合在画像阶段按 `unsupported`
    处理，不做运行期试探。

## 本轮完成（HTTP/2 prior-knowledge 负例，2026-11-16）

1. **负例样例**：新增 `java/samples/OkHttpH2PriorKnowledgeSample.java`，OkHttp
   只配置 `Protocol.H2_PRIOR_KNOWLEDGE`，对目标发明文 HTTP/2 preface。
2. **e2e**（`tests/integration/test_java_agent_h2_prior_knowledge_e2e.py`，
   `ASTSIGHT_JAVA_AGENT_E2E=1`）：计划内 host 指向一个只计数 TCP 连接的本地
   socket（代表真实下游），Mock 另起端口。实测：agent 仍完成改写
   （`http_rewrites >= 1`、`http_passthrough == 0`、adapter=okhttp），Mock 解析
   不了 h2c preface，SUT 打印 `failed=` 并以非 0 退出；计数 socket 0 连接，
   Mock 无 HTTP 层证据。
3. **行为契约**：h2c 不建设，不把协议降级为 HTTP/1.1（会扭曲 SUT 行为），
   失败必须显式且零透传，也不能当作用例通过。
4. **回归**：全量 **402 passed, 20 skipped**（启用 OkHttp / Apache HC / h2c /
   Spring Boot / 全链路五个 agent e2e；本轮只新增一个默认跳过的 e2e 与样例，
   未改运行时代码）。

## 本轮完成（Mock 路由精确优先，2026-11-16）

1. **匹配规则**：同一 host + method 下字面量路径与 `{}` 模板同时匹配时，字面量
   优先（`/orders/7` 不再因 `/orders/{}` 存在而歧义）；多个字面量或多个模板
   仍 `mock_route_ambiguous`，继续失败关闭、零透传。规则同时落在 Python
   `RouteTable.resolve`（`_prefer_exact`）与 Java `RouteTableSnapshot.resolve`
   （`preferExact`），两侧保持一致。
2. **测试**：新增 `engine/tests/test_mock_routes.py` 4 条（字面量优先×2、双模板
   歧义、双字面量歧义）；新增 Java 单测 `RouteTableSnapshotTest` 5 条
   （`mvn -pl astsight-agent-core test` 通过）。
3. **全链路 e2e 扩展**：`test_java_agent_flow_e2e.py` 恢复 RestTemplate 模板
   调用点，与 OkHttp 字面量落在同一 host+method：`/orders/7` 走字面量路由、
   `/orders/8` 回落模板路由；断言 2/2 passed、两个 adapter（okhttp +
   resttemplate）、无 `mock_route_ambiguous`/`mock_unexpected_dep`，token 仍
   不进命令行。
4. **回归**：全量 **401 passed, 20 skipped**（启用全部四个 agent e2e）。
5. **构建提醒**：只改 Java 源码后 `mvn -DskipTests package` 可能复用旧 core
   jar 导致 shade 产物滞后；本轮以 `mvn -pl astsight-agent -am clean package`
   重建，并重新生成 sidecar/wheel（`python tools/build_agent_wheel.py
   --skip-build`）。

## 本轮完成（agent 分发闭环与全链路 e2e，2026-11-16）

1. **wheel 分发**（`tools/build_agent_wheel.py`）：把 shaded jar 打成
   `astsight_java_agent-0.1.0.dev0-py3-none-any.whl`，内含
   `astsight_java_agent/agent/astsight-agent.jar`、`manifest.json`
   （`schema/artifact/version/jar_version/protocol/premain/sha256/size`）与标准
   `dist-info`（METADATA/WHEEL/RECORD）；构建固定时间戳与条目顺序，可复现。
   同时写 sidecar `astsight-agent.jar.manifest.json`，源码树构建也可校验。
   版本归一 `0.1.0-SNAPSHOT` → `0.1.0.dev0`，引擎常量
   `engine/astsight/agent/jar.py:AGENT_VERSION`，单测断言与 `java/pom.xml` 一致。
2. **引擎解析与校验**（`engine/astsight/agent/jar.py` + `sut.py`）：顺序为
   `ASTSIGHT_JAVA_AGENT_JAR`（显式覆盖、缺失不回退）→ 已安装 wheel（必须带
   manifest，校验 sha256/大小/版本/协议号）→ 仓库构建产物（有 sidecar 就校验，
   否则按开发构建接受但版本仍须匹配）。失败统一
   `injection_agent_unavailable:<dep>:<reason>`，新增
   `agent_jar_unreadable` / `agent_jar_version_mismatch` /
   `agent_jar_manifest_missing` / `agent_jar_manifest_invalid` /
   `agent_jar_checksum_mismatch` / `agent_jar_size_mismatch` /
   `agent_jar_protocol_mismatch`；期望版本可用
   `ASTSIGHT_JAVA_AGENT_REQUIRED_VERSION` 覆盖。实测从 wheel 解包目录解析为
   `source=wheel` 且 sha256 为真，仓库 jar 为 `source=repo`，篡改 jar 被拒。
3. **画像修正**：Java 的 HTTPS 调用点此前在画像阶段被 `tls_not_supported`
   丢弃，计划层看不到 dep、`agent_rewrite` 的 HTTPS 分支不可达。现
   `build_dep_id_plan` 增加 `https_call_sites`，只对 OkHttp / Feign /
   RestTemplate / Apache HC 调用点保留 HTTPS dep；其他语言与 WebClient /
   JDK HttpClient 继续排除。OkHttp `Request.Builder` 未写方法时按 GET 记录
   （与运行时默认一致）。
4. **`agent run` 全链路 e2e**（`tests/integration/test_java_agent_flow_e2e.py`，
   `ASTSIGHT_JAVA_AGENT_FLOW_E2E=1`）：Spring Boot fat jar 作 SUT，模型响应
   脚本化，经 `run_flow` 完成画像 → mock plan → `JAVA_TOOL_OPTIONS` 注入 →
   用例执行 → 报告。实测：注入策略 `agent_rewrite`；`execution.json` 1/1 passed、
   门禁 pass；`mock_calls.jsonl` 记录 `adapter=okhttp`、`mode=agent_rewrite`、
   `original_host=api.example.com`、`original_path=/orders/7`、`result=ok`，
   `mock_unexpected_dep` 零条；`sut.log` 含 `Picked up JAVA_TOOL_OPTIONS`，只有
   `token_env=ASTSIGHT_AGENT_TOKEN`，无 `token=` 明文。四个 agent e2e（OkHttp、
   HC4+HC5、Spring Boot、全链路）启用后 **4 passed**。
5. **测试**：新增 `tests/test_agent_jar.py`（12 条：版本归一/POM 对齐、wheel
   sha256、篡改、版本不符、缺 manifest、env 覆盖优先、缺失不回退、路径含空格、
   repo 回退、期望版本覆盖、wheel 布局与 RECORD）；`test_downstream_languages.py` 增加
   Java OkHttp HTTPS 默认 GET、WebClient HTTPS 与 Python HTTPS 仍拒绝 3 条；
   guard 测试改用带 manifest 的假 jar。全量 **393 passed, 24 skipped**。
6. **当轮已知边界**（下一轮已修复，见上节“Mock 路由精确优先”）：同 host +
   method 同时存在字面量路径与模板路径时，路由匹配 `mock_route_ambiguous` →
   `/_agent/unmatched` 502；agent 仍失败关闭、零透传。当轮全链路 e2e 因此只
   绑定单 dep，不把该缺口当通过。
7. **文档**：决策记录 v4.18/§17、能力设计 v4.18、Java README、engine README、
   `.gitignore`（`dist/`）同步。

## 已完成（Apache HttpClient 直连 adapter，2026-11-16）

1. **adapter**（`java/astsight-agent-http`）：新增 `ApacheHttpClientBridge`、
   `HttpClient4Transformer`、`HttpClient5Transformer` 与两个 Module；HC4 4.3+
   与 HC5 5.x classic 均织入 `InternalHttpClient#doExecute`，同时替换 `target`
   与请求 URI（`doExecute` 把 target 直接交给 `RoutePlanner`，不能置空），
   证据头与 OkHttp/Feign/RestTemplate 一致。
2. **版本差异**：HC4/HC5 共用反射 bridge，按类名前缀 `org.apache.hc.` 区分；
   HC5 参数描述符按 5.0–5.6 适配，Mock target 用 `HttpHost.create(URI)`，
   HC4 用 `HttpHost(host, port, scheme)`。
3. **计划期**（`agent/mockplan.py`）：langscan 的 `apache-httpclient` 由
   `unsupported` 改为 supported；JDK 11 `httpclient` 与 `webclient` 仍不支持。
4. **顺带覆盖**：内置未 shaded OkHttp / Apache HC 的第三方 SDK（如 AWS SDK
   v1/HC4、v2/HC5）会被对应 adapter 改写；计划期仍取决于画像能否解析 URL。
5. **测试**：新增 `test_java_agent_apache_httpclient_e2e.py`（HC 4.5.3 + 5.4.1
   同 JVM 直连 HTTPS，断言两个 adapter 共存、证据与心跳）；新增两条
   guard 测试（`apache-httpclient` → `agent_rewrite`、JDK `httpclient` →
   `unsupported`）；OkHttp e2e 的 adapter 断言改为集合包含。
6. **文档**：README、决策记录 v4.17/§17、能力设计 v4.17、使用文档、
   agent 架构同步。
7. **回归**：全量 **378 passed, 7 skipped**（新增 HC e2e 默认跳过）；启用三个
   agent e2e 后 **3 passed**（OkHttp、HC 4.5.3+5.4.1、Spring Boot fat jar）。
8. **版本矩阵**：新增 `test_java_agent_matrix_e2e.py` 与 Feign/HC4/HC5 独立样例；
   JDK 8/11/17/21/26 × 16 个客户端版本共 **80 组合全部通过**（OkHttp 3.14.9 /
   4.12.0 / 5.5.1、Feign 10.12 / 11.10 / 12.5 / 13.5、HC4 4.3.6 / 4.4.1 /
   4.5.14、HC5 5.0 / 5.1 / 5.2.3 / 5.3.1 / 5.4.1 / 5.6.5）；Spring Boot 3.5.6
   fat jar 在 JDK 17/21/26 通过；样例 javac 增加 `-encoding UTF-8`，兼容 JDK 8
   在中文 Windows 上的默认 GBK。

## 上一轮完成（Java HTTP agent P1：Feign / RestTemplate，2026-11-16）

1. **范围收敛**：只支持主流阻塞客户端 OkHttp / Feign / RestTemplate；WebClient、
   第三方 SDK、直接 Apache HC / JDK HttpClient / HttpURLConnection /
   async-http-client 不建 adapter，计划期标 `unsupported`，避免“看起来支持、
   实际漏流量”。
2. **计划选择**（`agent/mockplan.py`）：画像 dep 新增 `clients` / `languages`；
   Java 下游 HTTPS 只允许 `agent_rewrite`；配置键优先 `url_override`，服务名
   走 `discovery_override`，其余可识别客户端用 `agent_rewrite`；WebClient/未知
   客户端 `unsupported`（`client_unsupported` / `client_unknown`），每个注入
   dep 都带 `mock_injection_agent_rewrite:*` 告警。
3. **Feign adapter**：ASM 在 `SynchronousMethodHandler#targetRequest` 的
   `ARETURN` 注入，反射重建 `feign.Request`（按 Feign 10–13 的
   `Request.create` 重载动态选签名），统一覆盖默认 / OkHttp / Apache HC 传输；
   `X-AstSight-*` 证据头随请求发出。
4. **RestTemplate adapter**：ASM 织入 `RestTemplate#doExecute`（Spring 5 四参、
   Spring 6.2 五参均覆盖），替换 URI，并用动态代理包 `RequestCallback` 写入
   证据头；与底层 JDK / Apache / OkHttp RequestFactory 无关。
5. **失败关闭**：新增 `AgentRewriteException` 与 `AgentRuntime.isPlanned`；
   计划内请求改写失败时抛异常让本次调用失败，绝不返回原始请求透传到真实下游；
   计划外 host 原样放行。
6. **启动注入**（`agent/sut.py`）：`agent_rewrite` 通过 `JAVA_TOOL_OPTIONS`
   加载 jar；token 只走 `ASTSIGHT_AGENT_TOKEN` 环境变量（agent 选项
   `token_env`），不进入命令行；jar 路径取 `ASTSIGHT_JAVA_AGENT_JAR` 或仓库
   默认构建产物，缺失 / 无 token / 路径含空格都显式告警
   `injection_agent_unavailable:*`。
7. **证据兜底**（`mock/server.py`）：缺少证据头时从 `/_dep/<route_key>` 还原
   `original_path`，并补 `mode=agent_rewrite` 与 route host，避免证据把 Mock
   前缀记成下游路径。
8. **联调**：Spring Boot 3.5.6 fat jar 样例新增 `/feign/{id}`、`/rest/{id}`，
   与 `/call` 一起在 `LaunchedURLClassLoader` 下改写 HTTPS 到 Mock；e2e 不再
   手工拼 `-javaagent`，改为经 `start_sut` 走真实启动注入
   （`JAVA_TOOL_OPTIONS` + `token_env`），并断言 SUT 日志不含明文 token；
   三个 adapter 的 `http_adapters` 集合、`http_rewrites>=3`、三条证据的
   adapter/mode/host 均通过。普通 JVM OkHttp e2e 仍通过。全量
   **376 passed, 6 skipped**；启用两个 agent e2e 后 **3 passed**。

## 前一轮完成（Java 下游契约提取，2026-11-13 ~ 2026-11-16）

1. **响应类型抽取**（`profile/langscan.py`、`profile/callsite.py`）：RestTemplate 的
   `getForObject/getForEntity/postForObject/postForEntity/patchForObject/exchange`
   按参数位取 `.class` 或 `ParameterizedTypeReference<T>`；WebClient 在 `.uri(...)`
   之后 600 字符内找 `bodyToMono/bodyToFlux/bodyToValue` 的首参。变量名不当类型
   （如 `clazz` 直接丢弃），结果写入 `HttpCallSite.response_type`。
2. **依赖发现**（`profile/javadeps.py`）：解析 `pom.xml`（`<properties>`、parent
   版本、GAV 去重）与 `build.gradle(.kts)`（implementation/api/compileOnly/
   runtimeOnly/compile/kapt）；按 Maven 本地仓库、Gradle 缓存、项目 `libs` 定位
   jar；契约类 jar（api/client/sdk/dto/model/...）优先扫描。
3. **class 与类型解析**（`profile/javaclass.py`、`profile/javatypes.py`）：纯 Python
   解析常量池、字段、方法、类/方法注解与 `Signature` 属性；泛型签名按 JVMS
   4.7.9.1 递归处理嵌套泛型、上下界、数组与类型变量。`ContractIndex` 先查源码
   class/record/enum，再查依赖 jar；`List/Set` 归一为 `list`，`Page/Slice` 为
   `page`，`Optional/ResponseEntity/Mono` 等包装类型拆掉。另有
   `scan_feign_interfaces` 从源码与 jar 读取 `@FeignClient` 和映射注解。
4. **接入画像与 Mock**（`profile/downstream.py`、`mock/semantic.py`）：每个下游
   dep 增加 `response_types` 与 `response_contract`（`resolved`/`partial`/
   `missing` + 字段清单 + 未解析类型）；`success` 分支 body 按 A 的响应类型生成
   一层字段值，列表包成数组、分页包成 `content/totalElements/totalPages`。契约
   解析失败只记 warning，不阻断画像。
5. **Feign 调用点**（`langscan.py`、`downstream.py`）：画像扫描 Java 文件时把
   `scan_feign_interfaces` 的接口清单传进 `langscan`；按字段/构造参数的声明类型
   绑定变量，识别 `client.method(...)`，路径取 `@FeignClient(path)` + 方法映射，
   目标取 `url`，否则按服务名合成 `http://<service>`（与 `svc:` 身份约定一致）。
   服务类 jar 只按 `*Client/*Api/*Feign/*Remote/*Sdk` 类名读取，最多 4000 个 class，
   超限写 `feign_scan_truncated`。Feign 接口的返回类型同时进入 `response_types`。
6. **用例绑定契约校验**（`cases/validate.py`）：`validate_draft` 从画像收集每个
   dep 唯一的 `response_contract`；用例绑定 `success` 分支时，把分支 stub body
   （含列表/分页包装）与契约字段比对，缺字段记 warning
   `mock.body_contract_drift`，提示契约或 Mock 语义可能过期。类型不唯一时不校验，
   避免多响应类型造成误报；该检查只告警，不阻断导入。
7. **`discovery_override` 运行时**（`agent/mockplan.py`、`agent/sut.py`、
   `mock/plan.py`）：有 `service_name` 的 `svc:` 下游改选
   `discovery_override`（不再落到 `http_proxy`）；`injection_env` 把服务名指向
   Mock 地址，写入 `SPRING_APPLICATION_JSON` 的
   `spring.cloud.discovery.client.simple.instances.<service>[0].uri`，已有 JSON
   会深合并、不覆盖；路由仍按 method+path 兜底匹配。`injection_document()` 补上
   最小 `routes` 清单，并在注入里新增 `service_name`——`dep_id` 里的服务名被
   归一化为小写，而服务发现按注册名精确匹配，必须保留原始大小写（如
   `@FeignClient("HelloServer")`）。环境不支持时显式告警
   `discovery_not_supported` / `injection_discovery_applied:...:
   requires_spring_cloud_static_instances`，不假装成功。
8. **真实 Spring Cloud 工程联调**：用 GitHub 上的
   `spring-cloud-samples/feign-eureka`（Spring Boot 3.4.0 / Spring Cloud
   2024.0.0，JDK 26）做端到端验证，只读取其构建产物，不改源码：
   - Maven 构建 `client` 模块后，`DownstreamScanner` 识别出 Feign 调用点
     `http:svc:helloserver GET /`，`service_name` 保留 `HelloServer`，
     `response_types=["String"]`；`cases prepare` 能扫描出该路由并挂上下游。
   - 按真实扫描结果执行 `build_mock_plan → start_mock_plan → injection_document
     → start_sut`，SUT 启动环境包含 `SPRING_APPLICATION_JSON`；请求 SUT `/`
     返回 Mock 的 `pipeline-mocked`，`mock_calls.jsonl` 记录
     `route_match=method_path`、`branch=success`。
   - 反证：把服务名改成小写 `helloserver` 后 SUT 返回 500、Mock 无调用，确认
     Spring Cloud 的服务名匹配区分大小写，`service_name` 不能复用归一化后的
     `dep_id` 片段。
   - 联调中顺带修复：`@RequestMapping(..., method = GET)`（静态导入
     `RequestMethod.GET`）之前解析不出方法，Feign 接口与 Spring 路由两处都补上；
     方法级 `@RequestMapping` 不写 `method` 时按 Spring 语义展开为全部方法。
9. **验证**：新增 `tests/test_java_contracts.py`（24 条）、
   `tests/test_case_mock_contract.py`（4 条）与注入策略/路由测试，覆盖调用点
   响应类型、泛型签名、class 解析（含注解）、pom/gradle、本地仓库定位、源码/jar
   契约、jar 内 Feign、Feign 字段调用、画像挂载、Mock 语义、绑定漂移、静态
   实例注入与服务名大小写；真实 `logback-classic-1.5.38.jar` 解析 177 个 class
   全部成功。全量 **358 passed, 2 skipped**。
10. **标量响应契约**（`profile/javatypes.py`、`mock/semantic.py`）：JDK 标量
    （`String`/数字/布尔/`Object`/`Void`，含 `List<String>` 等容器）记为
    `kind="scalar"` 的已解析契约，不再落到 `missing`；Mock `success` body 按类型
    给 `""`/`0`/`false`，列表包一层数组。真实样例的 `String` 响应不再退化成
    `{}`。
11. **路由收敛**（`profile/langroutes.py`）：方法级裸 `@RequestMapping` 只展开
    `GET/POST/PUT/PATCH/DELETE`，`HEAD/OPTIONS/TRACE` 由框架派生，不生成用例
    端点；样例工程端点从 8 个收敛到 5 个。README 补充多语言下游契约与
    `discovery_override` 的适用边界。
12. **已知限制**：静态实例覆盖要求 SUT 使用 Spring Cloud LoadBalancer 并启用
    `SimpleDiscoveryClient`；其他发现机制（Nacos/Eureka 独占、Ribbon）或非
    Spring 应用仍无法覆盖，会在启动告警中体现。样例里 Eureka 客户端同时在
    classpath 上，未启动 Eureka 服务端时仍能走通静态实例，但这不是所有注册中心
    的通用保证。若两个服务共享同一 method+path，路由会报
    `mock_route_ambiguous`，不会静默选错分支。
13. **DTO 继承字段合并**（`profile/javatypes.py`）：`_contract_for` 递归合并父类
    字段，源码侧从 `extends` 解析父类 FQN（按 import / 同包 / 唯一类名解析，跳过
    泛型边界 `class X<T extends Base>`），jar 侧跟随 `ClassInfo.super_name`；
    子类同名字段（shadow）优先，超过 6 层停止并写
    `inherit_depth_exceeded:<fqn>`，继承环有 seen 集合防御，`java.lang.Object/
    Enum/Record` 不参与合并。真实 DTO 继承 `BaseEntity`/`PageResult` 时，
    `id`、`createdAt` 等父类字段不再从 Mock `success` body 中漏掉，绑定漂移
    告警的误报随之减少。
14. **真实联调固化为集成测试**（`tests/integration/test_java_feign_e2e.py`）：
    默认跳过，`ASTSIGHT_JAVA_E2E=1` 启用；样例路径 `ASTSIGHT_JAVA_E2E_PROJECT`，
    java 依次取 `ASTSIGHT_JAVA_E2E_JAVA` / PATH / `JAVA_HOME`，client 未构建时
    skip 并提示 Maven 命令。用例走真实 `build_mock_plan → start_mock_plan →
    injection_document → start_sut`：扫描 Feign 调用点、按用例创建 Mock 会话、
    启动 Spring Boot、再请求 `/`，断言返回 Mock body 且 `mock_calls.jsonl` 记录
    `route_match=method_path`、`branch=success`；反证把服务名改小写后 SUT 返回
    500 且 Mock 零调用。启用时 2 条约 16s，默认全量 363 passed, 4 skipped。

## 更早一轮完成（示例服务 V1.1 与 C6/C7 执行）

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

## 更早完成（按 v3 规则重建 A4）

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

## 历史完成（多请求用例模型）

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
7. **工作区已清**：2026-11-16 的 Dubbo、2026-11-17 的关系断言与 DB 反射改动
   均已提交并推送（`8393cbe`）；分支 `feat/postgres-mysql-data` 与远端同步。

## 下一步

2026-11-17 优先级调整：断言质量优先于协议扩展。

1. **跨步骤关系**：✅ 已完成（2026-11-17），见“本轮完成（跨步骤规则关系）”。
   数据目录的结构来源（DDL / DBML / 真实库反射）也已于同日完成。
2. **复杂公式抽取质量评估**：对真实需求文档标注“明确公式 / 隐含假设 / 缺口”，
   评估 A1 是否只抽取原文关系；复杂自然语言公式不得静默猜测。
3. **示例回归**：用合伙人注册/订单类规则补充“总额与明细”“接口状态与落库状态”
   用例，验证 L2→L3/L4 的实际提升。

Java agent / Dubbo 的历史顺序已完成（详见 `doc/决策记录.md` §17「后续工作」）：

1. **版本矩阵实测**：✅ 已完成（2026-11-16），JDK 8/11/17/21/26 × 16 个客户端
   版本共 80 组合全部通过；Spring Boot 3.5.6 在 JDK 17/21/26 通过。
2. **分发闭环**：✅ 已完成（2026-11-16），`tools/build_agent_wheel.py` 生成
   `astsight-java-agent` wheel（jar + `manifest.json` + sha256 + RECORD），引擎按
   环境变量 → wheel → 仓库解析并校验版本/校验和/协议号。
3. **真实 `agent run` 全链路 e2e**：✅ 已完成（2026-11-16），
   `test_java_agent_flow_e2e.py` 跑通画像 → 计划 → `JAVA_TOOL_OPTIONS` 注入 →
   执行 → 报告；证据零透传、token 不进命令行。同 host 字面量/模板路径并存时
   已实现字面量优先（Python/Java 同规则），e2e 同时覆盖 OkHttp 字面量与
   RestTemplate 模板两个调用点。
4. **覆盖扩展（按需求单独立项）**：HTTP/2 prior-knowledge 反例 ✅ 已完成
   （2026-11-16，负例 e2e）；第三方 SDK 端点发现明确不做（理由见决策记录
   §17）；Dubbo 消费端 Filter 首版 ✅ 已完成（2026-11-16，`-javaagent` +
   会话取桩 + 失败关闭 + 零真实调用）。
5. **Dubbo 版本矩阵**：✅ 已完成（2026-11-16），Dubbo 2.7.22 与 3.2.19 ×
   JDK 8/11/17/21/26 共 10 组合通过。classpath + `dubbo.consumer.filter`
   形态与本地心跳文件明确不做。矩阵外组合一律按 `unsupported` 处理。
6. **Dubbo 真实消费端代理 e2e**：✅ 已完成（2026-11-16），`ReferenceConfig`
   直连 URL 经真实代理调用，普通 classpath 与 Spring Boot fat jar 均返回桩、
   计数端口零建连零字节。下一步是 `agent run` 的 Dubbo 全链路 e2e
   （HTTP + Dubbo SUT，画像 → 计划 → 自动注入 → 执行 → 报告）。

已完成的历史步骤（Java HTTP Mock P0/P1/P1.5）见下方各轮完成记录。

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

- **2026-11-16**：回退 Dubbo classpath 形态与本地心跳文件实现，评估后**明确不做**：
  classpath 形态没有 `premain`，只能挂 Dubbo Filter、覆盖不了 HTTP，收益不足以
  支撑额外支持面；`agent_not_active` 继续以 admin 心跳为准。
- **2026-11-16**：Dubbo 消费端 Filter 首版：
  - 新增 `java/astsight-agent-dubbo`（Filter、`DubboBridge`、`DubboClassInjector`、
    `AstSightDubboModule`）；`dubbo-rpc-api:2.7.22` 为 `provided`；
  - Filter 字节在 `ExtensionLoader` 加载时 `defineClass` 注入应用类加载器，
    不增强 Dubbo 内部类；SPI 文件 `META-INF/dubbo/org.apache.dubbo.rpc.Filter`；
  - Mock 新增 `POST /_admin/dubbo/invoke` 与 `dubbo_deps` 路由表字段、证据
    `adapter=dubbo-filter`；`MockAgent.dubbo` 心跳块；`RouteTableSnapshot` 增加
    `dubbo_deps` 与 `isDubboPlanned`；
  - 调用形态补齐：老式异步、声明 `CompletableFuture<T>`、oneway、泛化
    `$invoke`/`$invokeAsync`；bridge 补设 `RpcContext` future 与
    `InvokeMode.FUTURE`；
  - 新增 `test_mock_dubbo.py`（7 条）、`test_java_agent_dubbo_e2e.py`（4 条，默认
    跳过）与 `java/samples/DubboFilterSample.java`；
  - 文档同步：决策记录 v4.21、能力设计 v4.21、PROGRESS、README。
- **2026-11-16**：Java HTTP agent 补直连 Apache HttpClient adapter：
  - `astsight-agent-http` 新增 `ApacheHttpClientBridge`、`HttpClient4Transformer`、
    `HttpClient5Transformer`、`HttpClient4Module`、`HttpClient5Module`；织入
    `InternalHttpClient#doExecute`，同时替换 target 与 request URI；HC4/HC5
    共用 bridge，HC5 用 `HttpHost.create(URI)`；
  - `agent/mockplan.py` 的 `_JAVA_SUPPORTED_CLIENTS` 增加 `apache-httpclient`，
    JDK 11 `httpclient` 与 `webclient` 仍 `unsupported`；
  - 新增 `test_java_agent_apache_httpclient_e2e.py`（HC 4.5.3 + 5.4.1 同 JVM）
    与两条 guard 测试；OkHttp e2e 的 adapter 断言改为集合包含；
  - 文档同步：`java/README.md`、`engine/README.md`、决策记录 v4.17、能力设计
    v4.17、使用文档、agent 架构、PROGRESS。
- **2026-11-16**：Java HTTP Mock P0 落地（OkHttp + HTTPS + 心跳 + 证据）：
  - 新增 `java/` Maven 工程：`astsight-agent-core`（premain、路由匹配、admin client、
    心跳、适配器注册表）、`astsight-agent-http`（OkHttp 3/4/5 `newCall`）、
    `astsight-agent`（shaded jar）；字节码目标 JDK 8；
  - Mock 服务新增 `GET /_admin/agent/routes`、`POST/GET /_admin/agent/heartbeat`、
    `/_agent/unmatched`（502，`mock_route_unknown`/`mock_route_ambiguous`）；
  - `MockStrategy.AGENT_REWRITE`、`MockAgent`/`MockHttpAdapter` 契约；
    `MockCall` 增加 `adapter`/`mode`/`original_host`，原始 path/query 从
    `X-AstSight-*` 头恢复；
  - `normalize_target_url(..., allow_https=)` 与 `validate_mock_plan` 按
    `agent_rewrite` 放开 HTTPS，其他策略仍 `tls_not_supported`；
  - OkHttp adapter 从 ByteBuddy Advice 改为裸 ASM 注入 + 反射 bridge：Spring Boot
    fat jar 的 `LaunchedURLClassLoader` 下 ByteBuddy 会因无法解析 `okhttp3.Request`
    抛 `NoClassDefFoundError`；
  - 新增两个可选集成测试：`test_java_agent_okhttp_e2e.py`
    （`ASTSIGHT_JAVA_AGENT_E2E=1`）与 `test_java_agent_spring_boot_e2e.py`
    （`ASTSIGHT_JAVA_AGENT_SPRING_E2E=1`，Spring Boot 3.5.6 fat jar），验证 HTTPS
    改写命中 Mock、证据字段与心跳计数；启用时 2 passed，全量 **367 passed, 6 skipped**；
  - 已知未完成：HC4/Feign/RestTemplate 适配器、`build_mock_plan` 策略选择与
    `agent run` 启动参数、JDK 8/11/17/21 矩阵。
- **2026-11-16**：确认 Java HTTP Mock 方案并写回设计（未实现）：
  - 新增 `agent_rewrite` 策略，选择顺序 `url_override > discovery_override >
    http_proxy > agent_rewrite > unsupported`：配置注入优先，agent 兜底；
  - agent 只做 URL 改写，复用内嵌 Mock 的路由/分支/会话/证据，不做进程内响应合成；
  - 计划内主机零透传：未命中转 `/_agent/unmatched`，返回 502 并记
    `mock_route_unknown` / `mock_route_ambiguous`；计划外主机允许真实调用，但报告
    必须列出；
  - HTTPS 仅在 agent 激活时放开；改写后 scheme 为 http，不建 TLS、不装 CA、不 MITM；
  - 写入 `doc/决策记录.md` §17、`doc/framework-capability-design.md`
    C3.5.5/C3.5.3、`doc/agent-architecture.md`，含 adapter 矩阵、失败子码、
    `MockCall` 证据字段与 P0-P3 阶段。
- **2026-11-16**：提交并推送 Java 下游契约与 Feign 服务发现注入（`a3a20ec`，
  `origin/feat/postgres-mysql-data`，20 个文件 / +3470 −36）：
  - 新增 `javaclass` / `javadeps` / `javatypes`，`langscan` 接入响应类型与 Feign
    调用点，画像增加 `response_types` / `response_contract`；
  - Mock `success` body 按契约生成，用例绑定校验告警字段漂移；
  - `discovery_override` 运行时（Spring Cloud 静态实例），注入保留服务名大小写；
  - 用真实 Spring Cloud 工程联调，修复 `injection_document` 缺路由、
    `@RequestMapping` 静态导入方法解析等问题；
  - JDK 标量响应（String/数字/布尔）记为已解析契约，Mock body 按类型生成；
  - 裸 `@RequestMapping` 只展开五个业务方法，避免端点放大；README 补充能力与
    边界；
  - 全量 **358 passed, 2 skipped**。
- **2026-11-16**：DTO 继承字段合并：`ContractIndex._contract_for` 递归合并父类
  字段，源码 `extends` 与 jar `super_name` 两条路径一致处理；子类同名字段优先，
  限深 6 层并防继承环，`java.lang.Object/Enum/Record` 跳过。新增 5 条测试
  （源码同包/跨包 import/泛型边界不误判/jar 继承/画像端到端），全量
  **363 passed, 2 skipped**。
- **2026-11-16**：真实 Spring Cloud 联调固化为可选集成测试
  `engine/tests/integration/test_java_feign_e2e.py`：`ASTSIGHT_JAVA_E2E=1` 启用，
  默认跳过；覆盖 Feign 扫描 → MockPlan → 静态实例注入 → SUT 启动 → Mock 命中
  → 证据留证，以及服务名大小写反证。启用时 2 passed / 16s；默认全量
  **363 passed, 4 skipped**。`engine/README.md` 补充运行方式与前置构建命令。
- **2026-11-13**：Java 下游契约从 A 侧提取：
  - `langscan` 抽取 RestTemplate（按参数位）与 WebClient（`bodyToMono` 等）的响应
    类型，写入 `HttpCallSite.response_type`；变量名不当作类型。
  - 新增 `profile/javadeps.py`（pom/gradle 依赖 + 本地仓库/Gradle 缓存/`libs`
    jar 定位）、`profile/javaclass.py`（常量池/字段/注解/`Signature` 解析，泛型
    签名按 JVMS 递归渲染）、`profile/javatypes.py`（源码优先、jar 兜底的
    `ContractIndex`，容器归一，Feign 接口扫描）。
  - `downstream.py` 为每个 dep 附 `response_types` / `response_contract`
    （`resolved|partial|missing`）；`mock/semantic.py` 用契约生成 `success`
    分支 body（列表/分页按容器包装）。
  - 修正 `javatypes` 中 `_method_return_type` 重名覆盖与 Feign 方法解析被参数
    注解括号截断的问题。
  - `langscan` 接入 Feign 字段调用：按声明类型绑定变量，路径由
    `@FeignClient(path)` + 方法映射拼接，目标取 `url` 或服务名；`downstream`
    传入项目/依赖中的接口清单，并按 `svc:` 身份生成 dep。
  - `cases/validate.py` 绑定校验新增 `mock.body_contract_drift`：`success` 分支
    stub 与 A 侧响应契约字段不一致时告警，不阻断导入。
  - `agent/mockplan.py` 对有服务名的 `svc:` 下游改选 `discovery_override`；
    `agent/sut.py` 把服务名写入 `SPRING_APPLICATION_JSON` 的 Spring Cloud 静态
    实例列表（深合并、不覆盖已有配置），环境不满足时显式告警。
  - `injection_document()` 补 `routes`，注入增加 `service_name`：`dep_id` 里的
    服务名已归一化小写，服务发现必须用原始注册名，小写会导致 500。
  - `@RequestMapping(..., method = GET)` 的静态导入写法在 Feign 接口与 Spring
    路由扫描中都补上；方法级裸 `@RequestMapping` 展开为全部方法。
  - 用 `spring-cloud-samples/feign-eureka`（Boot 3.4 / Cloud 2024，JDK 26）
    跑通真实端到端：扫描 → 计划 → 注入 → SUT → Mock 调用证据。
  - 新增 `tests/test_java_contracts.py`（24 条）、
    `tests/test_case_mock_contract.py`（4 条）与注入策略/路由测试；全量
    **355 passed, 2 skipped**。
- **2026-11-12**：Java/Go/Node 项目支持自动下游 Mock：
  - 新增 `profile/callsite.py` 共享 `HttpCallSite`；`profile/langscan.py` 用有界文本
    扫描识别 RestTemplate、WebClient、Java 11 HttpClient、OkHttp、Apache HttpClient、
    Go `net/http`/resty、Node `fetch`/axios/got/`http.request`，支持字面量、拼接、
    `Sprintf`/`String.format`、模板字符串与配置键（Spring `${...}` 归一化为环境变量名）。
  - 新增 `profile/langroutes.py`：Spring 注解、Gin/Echo/Chi/Fiber/Gorilla、
    Express/Fastify/Koa/Hono、NestJS；`inventory_routes` 在图谱/插件/降级三个分支
    合并并按 `(method, path)` 去重。
  - 路由 `line` 贯通 `inventory → impact → pipeline/flow → build_profiles`，非 Python
    handler 按文件+行定位函数块；Node 多行内联回调也纳入扫描，单行回调显式标
    `inline_handler_unscanned`，不猜。
  - 新增 `tests/test_downstream_languages.py`（12 条）；全量 **315 passed, 2 skipped**。
  - 文档同步：README、使用文档、能力设计、决策记录 v4.14。
- **2026-11-12**：A5/A6 合并为单个诊断修复 Agent：
  - 删除“先诊断、再修复”的两次 LLM 调用；`diagnose_and_repair()` 一次输出归因 +
    可选补丁（`RepairPlan.schema_version=2`），提示词统一为
    `prompts/a5_diagnose_repair.md`，旧 A5/A6 提示词改为废弃指针。
  - `--max-repair-rounds` 默认 **3**（上限 5，0 关闭）；`FlowConfig` 同步。每轮应用补丁
    后重跑到 `<run_id>-repairN`，无变化即停止。
  - 运行期修复不需要人工审核：用例补丁生成新 proposal 后以 `astsight-agent:repair`
    身份自动批准 `new/update/revive`，`deprecate` 仍拒绝；初始用例入库仍需人工批准。
  - 修复护栏：断言条数不得减少、推导强度 L1–L4 不得下降，用例身份/规则引用/前置状态
    不可改；校验失败整条补丁回滚。
  - 新增 Mock 结构失配修复：可改用例绑定的 Mock 分支 `status/headers/body/semantics`
    或 `mock_bindings`；写回 `.astsight/mock_semantics.json` 并通过
    `sync_mock_semantic` 热替换运行中 Mock 路由表，无需重启。5xx 只有归因为
    `mock_error` 且用例有 Mock 绑定时才可修，避免把真实缺陷“修绿”。
  - 运行产物新增 `diagnosis.json`、`diagnosis_meta.json`、`repair.json`、
    `repair-N.draft.json`、`repair-N.mock_semantics.json`。
  - 新增/更新测试：`test_agent_guards.py` 12 条、`test_agent_flow.py` 5 条（含
    Mock 结构失配 → 热更新 → 重跑通过）；全量 **302 passed, 2 skipped**。
  - 文档同步：README、ROADMAP、使用文档、Agent 架构、能力设计、决策记录 v4.13。
- **2026-11-12**：断言深度与强度口径升级：
  - `Assertion` 增加 `where`；支持 `header/contains`、`body_schema/matches` 与
    `db_row`（`exists/absent/count_eq/eq/ne/gte/lte`）。
  - `cases/context.py` 从 OpenAPI 提取响应 JSON Schema（内联本地 `$ref`）和首个 2xx
    Content-Type；`body_schema` 未写 `expect` 时由框架回填。
  - `cases/strength.py` 新增：按断言内容推导 L1–L4（L3 含 `$ref`，L4 引用两个及以上
    步骤）；声明值不一致产生 `assert_strength.mismatch` warning，导入以推导值为准。
  - `db_row` 导入时按 data catalog 校验表/列/`where`（缺目录只 warning）；codegen 新增
    `db_tools`/`schema_tools` fixture，只发只读 `SELECT`，未配置 `ASTSIGHT_DB_URL`
    时显式 skip `data_unavailable`。`jsonschema==4.26.0` 加入运行时依赖。
  - 模板正向用例自动增加 `body_schema` 与 `Content-Type` 断言；失败场景可用
    `db_row/absent` 验证无副作用。
  - 新增 `tests/test_case_assertions_advanced.py`（11 条，含 SQLite 运行时与 schema
    运行时）；全量 **297 passed, 2 skipped**。
  - 文档同步：使用文档断言语言/强度口径、`prompts/a4_case_design.md`、设计文档 3.2.5、
    决策记录 v4.12、engine README。
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
