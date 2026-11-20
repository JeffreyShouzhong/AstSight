# AstSight 项目进度

> 最后更新：2026-11-19
> 分支：`main`（已合并 `unified-mock-runtime`，与远端同步）
> 最新代码提交：`1b950d9`（`新增：astsight init 默认模型配置`）；
> 当前存档已同步远端；设计基线 v5.01。
>
> 当前阶段：HTTP / Dubbo / gRPC 下游 Mock 已统一到同一 session、分支、证据与门禁；
> gRPC unary 已打通 Python 与真实 Java SUT 两条完整 `agent run` 链路，并支持
> Linux amd64/arm64 gripmock wheel 构建与包内解析。Dubbo 在 Hessian2 sidecar 之外，
> 已支持 Triple unary：Protobuf descriptor 走 gRPC-compatible adapter，Java interface
> 无 proto 时走 Dubbo 3.2.19 sidecar 泛化调用；真实 Apache Online Boutique 的
> `tri` 服务 `run --mode full` 1/1 通过。多服务场景新增直连目标解析：
> 来源校验、代码地址、单 endpoint 去重、服务级映射与 IP 字面量约束，
> 注册中心不参与选 provider；三服务试点 6 条用例 5 条通过。
> 用例可声明业务实体 `data`，请求与下游 Mock 通过 `$data` 引用同一份数据；
> `mock_replies` 把实体写进 HTTP/Dubbo Mock 响应，按业务语义造数，
> 不再依赖环境里碰巧存在的真实 ID。测试数据只有两个来源：本服务数据库由
> C5b 插表构造（主表/关联表一起插，用例直接使用），下游服务由 Mock 构造；
> 不支持调业务接口造前置数据。真实 HTTP SUT e2e 已验证“实体 → 插表 →
> 下游 Mock → 请求 → 断言”同源链路，并修复了多表 fixture 的 teardown 顺序。gRPC target 配置已覆盖 Java
> `@Value/@ConfigurationProperties/System.getenv/System.getProperty`、Python settings、
> Node config/env 和 Go env/viper。
> HTTP Mock 已支持请求级 query/header/body 子集匹配与同 dep 调用序列；
> 多规则同时命中失败关闭，sequence 游标按 `session + dep_id` 隔离。
> HTTP agent 安全矩阵已覆盖 OkHttp 连接复用、同步/异步调用、客户端内部重试、
> 3xx 重定向、pinning、HTTPS scheme guard、二次改写、计划内未命中与 Host/签名边界。
> MQ 生产者拦截已落地 Kafka 首版：Agent 在 `KafkaProducer.send` 处截获，
> 计划内 topic 不真正发送，用例断言 topic/key/headers/body 子集与条数；
> 不连 broker、不验证分区与序列化。RabbitMQ 也已接入生产者拦截
> （`ChannelN.basicPublish`，`topic=exchange/routingKey`）和 RocketMQ
> （`DefaultMQProducer.send`）；真实客户端无 broker e2e 已通过，SUT 启动连接
> 仍可能需要中间件。Agent 已把 Jackson relocate 到独立包，避免被应用旧版本覆盖。
> Python SUT 也已支持：sitecustomize 注入，拦截 kafka-python 与 pika；confluent-kafka、
> aio-pika、kombu 暂不支持。MQ 断言只覆盖消息是否发出、topic/key/headers/body 子集
> 与条数，不做先后顺序；引擎也不测试 SUT 的多线程/并发行为。MQ 自动发现已解析
> Spring `@Value`/`@ConfigurationProperties` 与 properties/yml 配置键。
> 默认套件、平台 wheel 冒烟、真实后端 e2e 与 JDK/客户端矩阵均已进入 GitHub
> Actions Linux CI（真实后端 nightly + 手动触发）。delta 检测到接口删除后，
> 会直接把用例库中指向它的已批准 HTTP 用例标为 deprecated（逻辑删除），
> 不进入运行；报告单列。其余原待办项已明确不做。
> 近期收尾：C7.4 报告补上 audit 完整性块、weak_oracle、逐用例会话证据与 token
> 用量；run / agent run 在门禁失败时自动生成本地诊断包 feedback.zip，配合
> GitHub Issue 提交；LangGraph 编排与统一插件机制标注为骨架/设计方向，不排期。
> 工程侧：新增 `pyproject.toml` 与 `astsight` 命令入口，langgraph 移入 optional；
> `cli.py` 抽成 commands 服务层（2386 → 1469 行）；`cases/validate.py` 拆为职责
> 模块（3523 → 738 行，门面与错误码不变）；补 skipped/无 Mock 证据的防假绿测试。
>
> 数据侧：C5b 已支持多表多行、父子 fixtures、`$ref` 跨行父键引用、单父键自动关联、
> 多父歧义阻断；`state_paths` 已展开为 `state_path_*` 多行 DataState，可产生唯一主键、
> 共享父行、每迁移状态一行，并进入 SQLite apply/teardown 闭环。多行终态断言已
> 按终态行定位，负例可用 `db_row/unchanged` 做请求前后快照比较。
> 本地默认全量为 **728 passed / 72 skipped**；GitHub Actions Linux CI 默认套件
> **728 passed / 90 skipped**，真实后端 core **40 passed / 28 skipped**。开启 MQ、
> Java agent、Dubbo、Triple 与 gripmock 的历史本地全量为 **724 passed / 32 skipped**。
> A4 已用 DeepSeek 做真实验证：flash 的 MQ 场景能正确选 topic，chat 的实体场景
> 能生成 data/mock_replies；flash 在实体场景会输出非法断言，已记录为模型差异。

## 待办盘点（2026-11-19）

**当前没有计划中的待办项。** 删除接口的旧用例处置已落地（见“当前状态”）；
其余原待办项按 2026-11-19 的决定统一移入下方“明确不做”。

**明确不做**：RocketMQ 批量发送拦截、RabbitMQ/RocketMQ 连接级 Mock、注册中心选 provider、自动批准初始用例、MQ 消息先后/顺序断言（消息在生产者侧被同步截断，只有 SUT 调用先后，没有投递顺序；并发发送时先后无业务意义）、针对 SUT 多线程/并发行为的测试（并发请求、竞态、并发发送）、pika BlockingChannel 真实调用 e2e（patch 已覆盖，不再补专项验证）、时间窗口测试（不做时钟注入或专用测试接口）、异步多行/多表分组轮询、gRPC/Dubbo streaming 与 reflection、replay 模式、多 SUT 并发隔离（5.5 设计保留但不排期）、变异测试、confluent-kafka/aio-pika/kombu/Celery 与 Node/.NET MQ 拦截、多协议覆盖策略（M2.8，仅字段占位，alias 冒烟/`--transport-scope`/`transport_coverage` 不排期）、跨 transport 回退（M2.7，`transport_fallback` 不实现）、C7.1 数据冲突检测与 C7.2 并行调度（当前全量串行执行）、LangGraph 编排接线（`orchestration/` 骨架保留，不排期；当前为线性流程 + `case_error`/`mock_error` 最多 3 轮修复回环）、统一扩展插件机制（设计方向，当前无插件接口/注册表，不排期）。

## 当前状态

| 阶段 | 状态 | 产物 / 证据 |
|---|---|---|
| 默认模型配置 `astsight init` | ✅ 2026-11-20 | 新增 `astsight init`：默认写入 `~/.astsight/llm.json`，自动检测 Anthropic/OpenAI key 环境变量并选择 provider/model；交互式可改，`--yes`/`--local` 等参数；配置文件不写明文 key；缺配置错误提示运行 init；README/使用文档/快速指南统一改用 `astsight` 命令；新增 5 条测试，全量 728 passed / 72 skipped |
| CodeGraph 平台 wheel | ✅ 2026-11-20 | 新增 `tools/build_codegraph_wheels.py`：打包 CodeGraph 1.6.2 与自包含 Node 到 `astsight-codegraph-bin`；引擎从包资源解析 Node/node_modules，不依赖 PATH/npm/网络；`codegraph status` 显示 `source=packaged`；本地 Windows wheel 约 53MB，worker 握手 ready；CI 增加 linux-x64 构建与验证 job |
| 工程债务清理 | ✅ 2026-11-19 | 新增 `engine/pyproject.toml`（包元数据、`astsight` 入口、postgres/mysql/orchestration/dev extras），删除 requirements.txt，langgraph 移入 optional；`cli.py` 2386→1469 行，data/rules/mock 抽到 `commands/`；`cases/validate.py` 3523→738 行，拆为 `validate_base` + 9 个职责模块，门面与错误码不变；报告修复 skipped/无 Mock 证据假绿；本地 723/72、CI 723/90 |
| 文档与范围归档 | ✅ 2026-11-19 | C7.4 审计、诊断包、LangGraph 编排、扩展插件机制与运行模式口径已同步到设计文档、ROADMAP、使用文档和 CONTRIBUTING；设计基线 v4.99；PROGRESS 计数更新为本地 718/72、Linux CI 718/90 |
| 本地诊断包与 GitHub Issue 反馈 | ✅ 2026-11-19 | 新增 `feedback collect/open`：从 run 产物生成脱敏 zip（报告/执行/Mock 证据/失败用例快照与代码），生成 issue 草稿与 GitHub 提交链接；`run`/`agent run` 在门禁失败时自动生成 `feedback.zip`（`--feedback auto/always/off`），并纳入 `diagnosis.json`/`flow.json`；默认脱敏、不自动上传、不依赖服务端；新增 7 条测试，相关子集 12 passed |
| C7.4 审计报告补强 | ✅ 2026-11-19 | 报告新增 `audit` 完整性块；`weak_oracle` 标出 mock-bound + L1 + 零 Mock 命中的用例；`summarize_evidence` 输出逐用例 `session_evidence`（bindings/dep_id/branch/calls/status）；agent flow 把 `token_usage` 汇总写进 report；`egress_guard` 标 `not_implemented`，`instance`/`transport_coverage`/`transport_fallback` 标 `not_applicable`/`not_implemented`；新增 5 条测试 |
| 文档与代码对齐 | ✅ 2026-11-19 | 运行模式统一为 `full`/`delta`/`cases`（`replay` 明确不实现）；修正 gRPC 下游“仍 unsupported”的过期表述、C7.2 并行调度未实现标注、CLI 无 `--sut-id`；ROADMAP 非目标补全 RocketMQ 批量/连接级 Mock 与注册中心；README/设计文档能力表与测试计数同步 |
| C1 四类契约源 ContractModel | ✅ 2026-11-19 | 新增 `rules/contract_model.py`：OpenAPI、Spring MVC 参数/DTO、gRPC descriptor unary、Dubbo 2.7+ 接口先归一为统一操作/字段/来源/gap；CR 只从各源能力子集生成，跨源同事实合并 `support[]`，不同约束生成 framework conflict；Spring 常见校验注解、gRPC enum/repeated/oneof、Dubbo 源 DTO 约束、OpenAPI `$ref` 缺口均已接入；未定位 gRPC provider 时 `contract_eligible=false`，不生成 CR；新增 5 条测试，默认全量 706 passed / 72 skipped |
| 删除接口旧用例自动废弃 | ✅ 2026-11-19 | 新增 `cases/deleted_cleanup.py`：delta 确认接口删除后，把 caseset 中指向该接口的 approved HTTP 用例标为 `deprecated`（逻辑删除），`approved_cases` 与执行流程不再包含；`review_log` 以 `astsight:deleted-api` 记录，落库前备份；报告单列被废弃用例；扫描未完成或没有用例库时不动；新增 5 条测试，全量 701 passed / 72 skipped |
| 修复：心跳 adapter 分类 | ✅ 2026-11-19 | Linux CI 真实后端首跑暴露：Java agent 心跳把 `kafka/rabbitmq/rocketmq-producer` 也放进 `http_adapters`，Spring Boot e2e 的严格集合断言失败；`registerAdapter` 增加 kind，MQ 模块注册为 `mq`，心跳拆出 `mq_adapters`，`MockAgent` 契约同步；本地 Spring Boot 与 Java MQ e2e 均通过 |
| Linux CI 最小版 | ✅ 2026-11-19 | 新增 `.github/workflows/ci.yml`：`default-suite` 在 ubuntu + Python 3.12 跑默认套件；`linux-wheels` 用 Temurin 17 + Maven 构建 Java agent/Dubbo sidecar wheel，构建 gripmock amd64/arm64 平台 wheel，安装后跑三个分发校验，并解析包内二进制在 Linux 实跑 `test_grpc_gripmock_e2e.py`；另加 nightly/手动 core 真实后端 job 与手动 matrix job（JDK 8/11/17/21、MQ 客户端版本）；2026-11-19 手动触发全绿：默认套件 696 passed / 90 skipped，真实后端 core 40 passed / 28 skipped，JDK 矩阵各 18 passed，MQ 矩阵 6 passed |
| 跨协议 Mock 统一运行时 | ✅ 2026-11-18 | HTTP/Dubbo/gRPC 共用 session、分支、`mock_calls.jsonl`、证据与门禁；gRPC 真实 gripmock + Python/Java SUT agent run e2e 已通过 |
| Dubbo 直连目标解析 | ✅ 2026-11-18 | 新增 `execute/dubbo_targets.py`：来源校验（commit/branch 或 artifact sha256）→ 代码/XML 地址 → 单 endpoint 去重 → 服务级映射 → 运行直连 → 受限全局 URL；只接受 IP 字面量 + 端口，域名/Service/VIP 拒绝，注册中心不参与；`run`/`agent run` 新增 `--dubbo-target*` 与 `--dubbo-source-*`，结果写 `dubbo_targets.json`，conftest 按服务键严格查表；新增 15 条测试，全量 639 passed / 60 skipped |
| gRPC/Dubbo 契约 diff | ✅ 2026-11-19 | 新增 `changes/transport_contracts.py`：gRPC 比较 method/request/response 类型、streaming、message 字段编号/类型/枚举；Dubbo 比较方法签名、group/version、DTO 字段；`contract_changes.json` 增加 grpc/dubbo 段，报告单列并统一 `contract_change` 告警；只报告不自动生成入口用例；新增 2 条测试，全量 690 passed / 72 skipped |
| OpenAPI 字段级契约 diff | ✅ 2026-11-19 | 新增 `changes/contract_diff.py`：delta 用 worktree 读取 base/current OpenAPI，比较参数、请求体字段/必填、响应状态码/字段；变化端点并入 `changed_apis`；写 `contract_changes.json`，报告单列并给 `contract_change` 告警；新增 3 条测试 |
| 设计文档基线刷新 | ✅ 2026-11-19 | 标题/文末基线更新到 v4.88；新增“当前实现状态”摘要，区分已实现/部分实现/未实现；修正 gRPC 下游过期表述 |
| A4 真实模型验证（DeepSeek） | ✅ 2026-11-19 | `deepseek-flash` + DeepSeek Anthropic 兼容端点：MQ 场景正确从 `mq_producers` 选 `orders-topic`、生成 `mq_assertions` 且不编 key/body；`deepseek-chat` 在实体场景生成 `data.product`、`query $data`、`mock_bindings`/`mock_replies` 与回显断言；`deepseek-flash` 在该实体场景生成了非法 `body_field op=absent`，暴露模型差异；顺手修复 A1 规则上下文把 `ParamSpec` 对象写进 JSON 的序列化 bug |
| 真实后端全量回归 | ✅ 2026-11-19 | 开启 MQ、Java agent、Dubbo sidecar/provider、Java agent flow、Triple、gripmock 后全量为 **724 passed / 32 skipped**；默认套件 684 passed / 72 skipped；未发现 MQ/数据链回归 |
| MQ 客户端版本矩阵 | ✅ 2026-11-19 | 新增 gated `test_mq_version_matrix_e2e.py`：Maven 按版本解析 classpath、javac 编译后运行，不构建 shade jar；Kafka 2.8.2/3.7.0、RabbitMQ 4.9.0/5.21.0、RocketMQ 4.9.7/5.3.1 共 6 组真实客户端无 broker e2e 全部通过；Python 当前验证 kafka-python 3.0 / pika 1.3 |
| Python MQ 自动注入 e2e | ✅ 2026-11-19 | 新增 gated `test_agent_flow_python_mq_e2e.py`：真实 `agent run` + FastAPI SUT + KafkaProducer，自动注入生效、用例执行、`mq_tools` 断言与门禁通过；Python capture 启动上报 `mq.installed`/clients；默认全量 684 passed / 66 skipped |
| 被删除接口变更感知 | ✅ 2026-11-19 | delta 用临时 `git worktree` 检出 merge-base，重扫路由并与当前对比；删除项写 `deleted_apis.json`、报告单列并给 `deleted_api` 告警；扫描失败显式 warning；当时只提示不自动废弃旧用例（v4.94 起自动逻辑删除）；新增 2 条 git 测试，全量 684 passed / 65 skipped |
| MQ 配置值解析 | ✅ 2026-11-19 | `profile/mq.py` 支持 Spring `@Value`（含 `${key:default}`）、`@ConfigurationProperties`、`application*.properties/yml` 与项目根 `.env`；`@ConfigurationProperties` 字段跨文件全局索引，调用点支持 `properties.field`/`getField()`；同键多值冲突、缺失、SpEL 与过深占位符显式告警；所有未解析调用点写 `mq_topic_unresolved`；producer 增加可选 `topic_source`/`config_key`；新增 6 条测试，全量 696 passed / 72 skipped |
| MQ 自动发现与 A4 生成 | ✅ 2026-11-19 | 新增 `profile/mq.py`：扫描 Java Kafka/Rabbit/Rocket 与 Python kafka-python/pika 生产者，解析字符串常量；画像附 `mq_producers[]`；A4 只从候选生成 `mq_assertions`；未知 topic 告警 `mq.topic_not_in_profile`；新增 8 条测试，全量 682 passed / 65 skipped |
| Python MQ 生产者拦截 | ✅ 2026-11-19 | 新增 `astsight/mq/inject`（sitecustomize + import hook）：patch kafka-python `KafkaProducer.send` 与 pika `Channel/BlockingChannel.basic_publish`；`agent run` 对 Python SUT 自动注入 `PYTHONPATH` 与 `ASTSIGHT_MQ_CAPTURE_*`；真实 kafka-python 3.0 / pika 1.3 无 broker e2e 通过；confluent-kafka、aio-pika、kombu 不做 |
| MQ 真实客户端 e2e | ✅ 2026-11-19 | 新增 `java/samples/mq-producer`（Kafka 3.7 / RabbitMQ 5.21 / RocketMQ 5.3.1）与 gated `test_java_mq_producer_e2e.py`：无 broker 下真实客户端消息被 Agent 拦截，Mock 收到完整内容；修复 MQ transformer 的 stackmap frame（VerifyError）；agent shade 将 Jackson relocate，移除未用 ByteBuddy；OkHttp/Dubbo consumer e2e 回归通过；默认全量 672 passed / 63 skipped |
| MQ 生产者拦截 RocketMQ P0 | ✅ 2026-11-19 | 新增 `astsight-agent-rocketmq`：织入 `DefaultMQProducer` 同步/异步 send，计划内消息不真正发送；topic 为 RocketMQ topic，key 取 getKeys、headers 取 getProperties；同步返回反射构造的 SendResult（失败为 null），异步尽力回调 onSuccess；`client=rocketmq` 进入心跳按客户端校验；SUT 启动仍可能连接 NameServer/broker；Java agent 重新打包 |
| MQ 生产者拦截 RabbitMQ P0 | ✅ 2026-11-19 | 新增 `astsight-agent-rabbitmq`：织入 `ChannelN.basicPublish` 4/5/6 参重载，计划内消息不真正 publish；`topic=exchange/routingKey`、`key=routingKey`、headers 取 BasicProperties，body 支持 JSON/文本/base64；Java agent 重新打包；SUT 启动阶段的 AMQP 连接/声明仍需环境或后续连接级 Mock；RocketMQ 待做 |
| MQ 生产者拦截 Kafka P0 | ✅ 2026-11-18 | 新增 `astsight-agent-kafka`：织入 `KafkaProducer.send`，计划内 topic 上报后不真正发送；引擎新增 `MqAssertion`、`MockPlan.mq_routes`、`/_admin/mq/publish` 与 `/_admin/mq/messages`，生成 `mq_tools.expect`；证据 `transport=mq`；心跳缺 `mq.installed` 阻断；新增 8 条 Python + 1 条 Java 测试，全量 671 passed / 60 skipped；RabbitMQ/RocketMQ 待做 |
| 异步断言优先消息边界 | 📌 2026-11-18 | 口径修正：MQ 等消息型异步断言消息本身（发出/topic/key/payload/headers 与条数），不启动消费者、不轮询数据库，不校验先后顺序（v4.89）；`wait_ms` 只用于没有消息边界的内部异步；MQ 消息捕获已实现 Kafka/RabbitMQ/RocketMQ 与 Python kafka-python/pika |
| 多行终态与状态不变断言 | ✅ 2026-11-18 | C5b 迁移后断言支持多表/多列/多行/复合主键，`state_path_*` 按终态行定位；新增 `db_row/unchanged`，请求前快照、请求后逐列比较，支持 `ignore_columns`；单请求与多步骤均覆盖；新增 6 条测试，全量 663 passed / 60 skipped |
| 实体 + 造数 + Mock 真实链路 e2e | ✅ 2026-11-18 | 新增 `test_case_entity_mock_e2e.py`：真实 HTTP SUT 从 SQLite 读订单/明细，调用下游；用例 `data` 同时用于插表、请求和 `mock_replies`；断言验证请求、Mock 与插入行同源；顺带修复生成 fixture 的 teardown 顺序错误（`plan.teardown` 已是逆拓扑，代码又反转一次导致外键删除失败） |
| README 首页差异化 | ✅ 2026-11-18 | 首页第一屏增加“pytest + LLM vs AstSight”对比表，明确门禁、规则事实源、插表造数 + 下游 Mock、失败定位四点；能力表同步用例实体造数口径 |
| 请求字段全量对照（不做） | ⛔ 2026-11-18 | 明确非目标：fixtures 是数据库基线而非请求镜像；业务输入、负例值、SUT 自生成字段、下游 Mock 实体 ID、认证头都不要求入库；硬校验只保留显式 `preconditions.table + where`，不做默认字段映射 |
| 实体与插入行闭环 | ✅ 2026-11-18 | `preconditions.where/set` 支持 `$data`，造数前解析为实体值；显式 `table+where` 必须能在 fixtures 中找到匹配行，否则拒绝 `precondition_row_missing`；请求/Mock/前置条件共用同一实体；新增 2 条测试，全量 656 passed / 60 skipped |
| 测试数据两来源原则 | ✅ 2026-11-18 | 前置数据只走 C5b 插表（主表/关联表一起插，用例直接用这组值），下游数据只走 Mock；拒绝 `api_state` 调接口造数；C5b 提示词与输入改为使用已解析 `$data` 的请求值，要求 fixtures 业务键与请求一致；新增 2 条测试，全量 654 passed / 60 skipped |
| A4 自动生成实体与 Mock | ✅ 2026-11-18 | A4 提示词要求按业务实体建模：规则依赖实体属性时生成 `data`，请求/断言/`mock_replies` 引用同一实体；`mock_semantics` 分支实际响应体进入提示词；漏写 binding 自动补 `success` 并告警；补充未知字段给 `mock.reply_field_unknown` 告警；新增 3 条测试，全量 652 passed / 60 skipped |
| 用例业务实体与 Mock 联动 | ✅ 2026-11-18 | `TestCase`/`DraftCase` 新增 `data` 与 `mock_replies`；请求、Dubbo 参数、断言和下游 Mock 统一引用 `{"$data": "实体.字段"}`；会话 binding 携带 `reply`，HTTP/Dubbo 分支 body 递归合并；校验未知引用、无绑定补充与 gRPC 拒绝；codegen 生成前替换为字面量；新增 10 条测试，全量 649 passed / 60 skipped；A4 自动生成实体待做 |
| Apache Online Boutique 多服务试点 | ✅ 2026-11-18 | 检出广告/购物车/商品目录三服务，目标映射 `tri://127.0.0.1:20880/20881/20886`；画像与来源校验通过，生成 6 条用例、5 通过；`getProduct` 因采样商品 ID 不存在返回空值，属 DTO/种子数据边界而非目标解析问题；修复 `implements Serializable` 被误判为多接口的画像缺陷 |
| Dubbo Triple unary | ✅ 2026-11-18 | `tri://` Protobuf descriptor 走 Python gRPC-compatible；Java interface 走 Dubbo 3.2.19 sidecar；真实 `online_bontique_demo` full run 1/1 |
| gRPC target 配置矩阵 | ✅ 2026-11-18 | Java `@Value/@ConfigurationProperties/System.getenv/System.getProperty`、Python settings、Node config/env、Go env/viper；硬编码 target 显式阻断 |
| HTTP 请求匹配与调用序列 | ✅ 2026-11-18 | `mock_rules` 支持 query/header/body 子集匹配，`mock_sequences` 按 session+dep 推进；rule 优先，sequence 耗尽回退 default，歧义失败关闭 |
| HTTP Agent 安全矩阵 | ✅ 2026-11-18 | 同一 OkHttpClient 的同步/异步多次调用全部命中 Mock；内部重试复用已改写 URL，只改写一次但可产生多次 Mock 证据；3xx、HTTPS scheme guard 和计划内未知路径均失败关闭；pinning 不阻断显式改写的明文 Mock；已改写 URL 不二次改写，真实下游始终 0 连接 |
| 多行 fixtures / 父子表 / state_paths | ✅ 2026-11-18 | 多父多子、`$ref` 父键、单父自动关联、歧义阻断；`state_paths` 展开为逐状态多行 DataState 并完成 SQLite apply/teardown |
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
| Dubbo sidecar 首版 | ✅ 2026-11-17 | 新增 `java/astsight-sidecar-dubbo`：独立进程 stdin/stdout JSONL（`ping`/`invoke`/`shutdown`），`ReferenceConfig<GenericService>` + Hessian2 泛化调用，引用按 `interface/group/version/url/timeout` 缓存、固定 `retries=0`；错误归一为 `TIMEOUT`/`SERIALIZATION`/`NETWORK`/`NO_PROVIDER`/`RPC_EXCEPTION`/`BAD_REQUEST`；仅直连 URL，注册中心与 Kryo/FST 显式失败；JDK 9+ 自动 `--add-opens`；Java 单测 17 条 |
| Dubbo 提供方画像 | ✅ 2026-11-17 | `profile/dubbo.py` 扫描 `@DubboService` / 2.7 `@Service` / XML `<dubbo:service>`：接口必须在仓库、参数与返回类型解析到 JVM 名，`group/version` 进入 endpoint；多接口无 override、2.6、接口在 jar、Kotlin 提供方显式 `unsupported`，Spring `@Service` 不误判；`build_profiles` 追加 `transport=dubbo` 画像，context 增加 `dubbo` 证据、`dubbo_providers_only` 告警与提供方指纹；模板用例跳过非 HTTP；新增 10 条测试，全量 522 passed / 41 skipped |
| Online Boutique adService 试点 | ✅ 2026-11-18 | 从 `apache/dubbo-samples` 检出 `online_bontique_demo` 的 adService/common/interface；样例默认 `tri` + Nacos，试点时覆盖为 `dubbo` + `N/A` + Hessian2；`run --mode full` 1/1 通过，Lombok DTO 的 `contextKeys` 被正确采样为 `["sample"]`；发现：真实项目默认 Triple/注册中心时，需要配置覆盖或补注册中心/Triple 支持 |
| Apache direct 样例试点 | ✅ 2026-11-18 | 从 `apache/dubbo-samples` sparse 检出 `2-advanced/dubbo-samples-direct`（Dubbo 3.3.1 / Spring Boot 3.2.3，直连 URL、无注册中心），构建启动后 `run --mode full` 生成并执行 1 条用例通过；验证 sidecar 泛化调用对 Dubbo 3.3.1 兼容；下载目录在仓库外 |
| 常驻 Dubbo provider 样例 | ✅ 2026-11-18 | 新增 `java/samples/dubbo-provider`：Spring Boot 3.5 + Dubbo 2.7.22，`@DubboService` 供静态画像、`ServiceConfig` 实际导出；DTO 含 `@NotBlank/@Size/@Min/@Max/@Email/@Pattern`、枚举、`LocalDate`、嵌套 `Address`；5 个方法；`astsight run --mode full` 5/5 通过；样例 e2e 5/5；同时修复 sidecar ping 的 `dubbo_version` 被 shade 覆盖的问题；全量 564 passed / 45 skipped |
| A5 多步骤修复 | ✅ 2026-11-18 | `CasePatch.steps: [CaseStepPatch]`：按步骤名定位，可改 `path_params/query/headers/body/dubbo/asserts`；未知步骤整条拒绝；步骤级断言不得减少/降强，Dubbo 步骤必须保留 `rpc_status`；应用后重新走 `validate_draft`；新增 3 条修复测试；全量 564 passed / 44 skipped |
| Sidecar wheel 分发 | ✅ 2026-11-18 | `tools/build_sidecar_wheel.py`：jar + `manifest.json`（版本/协议/sha256/大小）+ RECORD；引擎解析 env → 已安装 wheel → 仓库构建产物，wheel 逐项校验；8 条分发/校验测试 |
| Dubbo 多步骤 | ✅ 2026-11-18 | `CaseStep`/`DraftCaseStep` 支持 `transport=dubbo` 与 `dubbo`；校验按步骤检查 `rpc_status` 与 `$ref`；codegen 生成多个 `dubbo_call` 并把 `result.value` 暴露给后续步骤；真实 provider e2e 覆盖两步调用与字段引用 |
| Dubbo 诊断与报告 | ✅ 2026-11-18 | `classify` 读取 `rpc_status`/`error_code`：NO_PROVIDER/NETWORK/TIMEOUT → 环境重试，SERIALIZATION/BAD_REQUEST → 用例错误；A5 可修单步 `dubbo.arguments`，case payload/profile view 补 Dubbo 字段；报告新增 `by_transport`；新增 3 条归因测试 |
| DTO 采样再增强 | ✅ 2026-11-18 | 新增正则样例生成（字面量/字符类/范围/量词/分支/锚点，复杂结构安全失败）；自定义校验注解（`@Mobile`/`@Range`/`@EnumValue`/`@Column` 等）进入 `constraints.custom` 并推断常用约束；集合元素上限 3→10，按 `min_length`/`max_length` 生成；新增 9 条正则测试与自定义注解/集合上限断言；全量 546 passed / 43 skipped |
| DTO 采样增强 | ✅ 2026-11-18 | 时间类型（`java.time.*`、`java.util.Date`、`UUID`/`URI`/`URL`）进入 schema 并由 sidecar 转成真实 Java 对象；集合/Map/数组按 `element`/`key`/`value` 采样；`@Size`/`@Min`/`@Max`/`@Pattern`/`@Email`/`@Positive` 等注解进入 `constraints`，模板按约束生成合法值，`pattern` 提示人工确认；未知第三方类型按空对象并写入 note；Java 19 条单测、真实 provider 验证 `LocalDate` DTO 字段；全量 537 passed / 43 skipped |
| A4 Dubbo 草案 + DTO 采样 | ✅ 2026-11-17 | A4 提示词收入 `transport=dubbo` 画像、`parameter_schemas`、`result_schema`、`direct_url/protocol_port`；草案只填 `dubbo.arguments`，interface/method/param_types 由画像回填；provider 扫描解析 DTO 字段、嵌套 DTO、record 组件、枚举常量与 `@NotNull` 等必填标记；模板按字段生成 arguments，无法解析的类型按 `null` 并在 note 标明；新增 3 条测试，全量 535 passed / 43 skipped |
| Dubbo 入口用例闭环 | ✅ 2026-11-17 | `TestCase.dubbo: DubboCall` + 原生断言 `rpc_status`/`result_present`/`result_field`/`error_code`/`exception_type`/`error_message`；模板生成正向 Dubbo 用例（POJO 参数按 `{}` 并在 note 标明）；codegen 生成 `dubbo_call`，conftest 管理 sidecar 生命周期；目标地址 `ASTSIGHT_DUBBO_URL` → 用例 `dubbo.url` → 画像 `protocol_port` + `ASTSIGHT_DUBBO_HOST`，解析不了 blocked；纯 Dubbo 项目（无 HTTP 路由）`run --mode full` 可执行；草案校验按画像回填 interface/method/param_types；真 provider e2e 覆盖 codegen 与 full 模式；全量 532 passed / 43 skipped |
| Dubbo sidecar 客户端 | ✅ 2026-11-17 | `engine/astsight/sidecar/dubbo.py`：进程生命周期、JSONL 请求/响应、超时与退出检测、stderr 尾部诊断、jar 解析（`ASTSIGHT_DUBBO_SIDECAR_JAR` → 仓库构建产物）；Python 单测 9 条 + 真实 jar e2e 4 条 |
| 跨协议 Mock 统一控制面 P0 | ✅ 2026-11-18 | 新增 `MockOutcome`、`HttpMockReply`/`DubboMockReply`/`GrpcMockReply` 与 `MockReply` discriminated union；`MockBranch.outcome/reply` 支持协议中立结果和协议专属响应；新增 `MockRuntimeAdapter`/`MockRuntimeManager`，HTTP/Dubbo 通过内嵌 adapter 接入统一 activate/collect/deactivate，含失败回滚与 call_id 去重；`MockCall` 增加 `transport/grpc_status`；新增 6 条单测，全量 570 passed / 45 skipped |
| 跨协议 Mock 统一控制面 P1 | ✅ 2026-11-18 | 新增 `HttpMockLocator`/`DubboMockLocator`/`GrpcMockLocator` 与 `MockPlanDep`；`MockPlan` 校验后派生统一 `deps[]` 并写入 manifest/injection 文档；MockSemantic 改为按真实 dep 类型生成 HTTP/Dubbo/gRPC transport，Dubbo 使用 `agent_rewrite` + `DubboMockReply`；gRPC plan builder 在 runtime 未实现前显式阻断；新增 2 条统一 Plan/Semantic 测试，全量 572 passed / 45 skipped |
| 跨协议 Mock 统一控制面 P2 | ✅ 2026-11-18 | gRPC dep 支持纯 `deps[]` Plan 输入并分配确定性 route_key；新增 `GrpcDescriptorIndex`：`.proto` 经 `grpc_tools.protoc` 递归编译或直接加载 `.protoset`，索引 unary method、拒绝 streaming、按响应 message 严格校验分支 body、记录 descriptor sha256；gRPC Plan 在 runtime 未实现前仍显式阻断；新增 5 条 descriptor/Plan 测试，全量 577 passed / 45 skipped |
| 跨协议 Mock 统一控制面 P3 | ✅ 2026-11-18 | 新增 `GripMockHttpBackend`：官方 readiness/stubs/history REST API、stub id 校验；新增 `GrpcMockRuntimeAdapter`：session 激活时清 stubs/history、安装 `GrpcMockReply`、history 归一化为 `MockCall`、canonical gRPC status/branch/unmatched 处理；新增 `GripMockProcess`：端口环境、日志、readiness 超时与 TERM/KILL；新增 5 条 backend/runtime/process 测试，全量 582 passed / 45 skipped；Plan 自动进程/target 接线仍待完成 |
| 跨协议 Mock 统一控制面 P4 | ✅ 2026-11-18 | 新增 `RuntimeSessionBridge` 并接入 `MockHttpServer` session hooks：create 激活全部 runtime、drain 采集 gRPC history 并去重写 evidence、delete 清理 stubs；激活失败回滚 session，采集/关闭失败显式 502；`start_mock_plan` 支持注入 `grpc_backend`，gRPC-only Plan 使用内嵌 HTTP 控制面可完成 create→install→drain→collect→delete→clear 闭环；新增 1 条 lifecycle 测试，全量 583 passed / 45 skipped；自动启动真实 gripmock/target 改写仍待完成 |
| 跨协议 Mock 统一控制面 P5 | ✅ 2026-11-18 | `start_mock_plan` 支持 `gripmock_path` 自动模式：分配 gRPC/admin/gateway 端口、启动 `GripMockProcess`、失败回收进程；runtime/manifest/injection/report 暴露三个 endpoint；每个 gRPC dep 自动生成 `grpc_server` target 注入项；首版限制单一 descriptor source；新增 process/port/injection 测试，全量 584 passed / 45 skipped；SUT 启动器实际应用 target 与真实 binary e2e 待完成 |
| 跨协议 Mock 统一控制面 P6 | ✅ 2026-11-18 | `GrpcMockLocator.property` 支持声明 SUT target 环境变量；自动 injection 携带 property；`injection_env` 支持 `grpc_server`，写目标 env 并输出 applied 告警，缺配置键/非 env key 显式失败；新增 gRPC target env 注入测试，全量 585 passed / 45 skipped；真实 gripmock binary e2e 与配置文件/编程式 target 改写仍待完成 |
| 跨协议 Mock 统一控制面 P7 | ✅ 2026-11-18 | 下载官方 gripmock v3.23.8 Windows amd64 slim binary 并按 release sha256 校验；新增 `GRIPMOCK_BIN` gated 真实 e2e：自动进程→session install→unary success→drain/history/evidence→delete→error `UNAVAILABLE`→cleanup；修正无匹配 matcher 为 `input.equals={}`；engine README 增加运行说明；Linux 分发与 automatic gRPC semantics 待完成 |
| 跨协议 Mock 统一控制面 P8 | ✅ 2026-11-18 | `run --mock-plan` 与 `agent run` 新增 `--gripmock-path`；`RunConfig`/`FlowConfig` 透传到自动 process；手工 gRPC plan 可从正式 CLI 启动，agent plan 含 gRPC dep 时也可使用该 binary；engine README 更新用法；定向 20 条回归通过；gRPC SUT 主链路样例与 automatic profile/semantics 仍待完成 |
| 跨协议 Mock 统一控制面 P9 | ✅ 2026-11-18 | 新增真实 SUT→gRPC 下游 Mock e2e：SUT 从 `INVENTORY_GRPC_ADDR` 读取注入 target，通过 grpcio 调用 gripmock；success 返回 protobuf 字段，`UNAVAILABLE` 在 SUT 映射为 502；两段 session history 均进入统一证据；真实测试 1 passed，全量默认 585 passed / 47 skipped；Linux 分发与 automatic profile/semantics 待完成 |
| 跨协议 Mock 统一控制面 P10 | ✅ 2026-11-18 | 新增 gRPC downstream 自动画像：仓库 `.proto` unary 方法索引；Python `*Stub(channel)`、Java `*BlockingStub/FutureStub` 调用识别；env target property 与字面量 target 识别；profile 附带 gRPC dep，plan 草稿输出统一 `deps[]` + `grpc_server` injection；新增 2 条测试，全量 587 passed / 47 skipped；Node/Go/复杂 DI 与 automatic semantics 待完成 |
| 跨协议 Mock 统一控制面 P11 | ✅ 2026-11-18 | gRPC descriptor 新增 response message 递归采样；MockSemantic 默认 success 生成 `GrpcMockReply`；规则分支映射 HTTP 错误语义到 canonical gRPC code，错误体使用空消息；service/grpc_path/method/property 参与规则匹配；新增 1 条 semantic 测试，全量 588 passed / 47 skipped；agent run 自动 plan 接线待完成 |
| 跨协议 Mock 统一控制面 P12 | ✅ 2026-11-18 | `build_mock_plan` 支持 gRPC binding，生成统一 `MockPlanDep`、`GrpcMockReply` 与 `grpc_server` injection；热更新刷新 gRPC replies；A4 semantic view 已含 transport/outcome；新增 build_mock_plan 测试，全量 589 passed / 47 skipped；完整 gRPC agent run 样例与 Linux 分发待完成 |
| 跨协议 Mock 统一控制面 P13 | ✅ 2026-11-18 | 新增 `astsight-gripmock-bin==3.23.8` Linux amd64/arm64 平台 wheel 构建器：manylinux 标签、可执行位、MANIFEST/RECORD、LICENSE/NOTICE，固定 release 压缩包 sha256、source commit 与二进制 sha256；新增包资源解析器，不查 PATH/运行期不联网；`start_mock_plan` 默认自动解析并校验 binary，`--gripmock-path` 标记 `source=override/degraded`；真实两个架构 wheel 均可构建并解析，6 条分发测试，全量 596 passed / 48 skipped；完整 gRPC agent run 样例待完成 |
| 跨协议 Mock 统一控制面 P14 | ✅ 2026-11-18 | 完整 gRPC `agent run` e2e 打通：仓库 `.proto`/Python stub 自动画像 → semantic/Plan → 真实 gripmock → `INVENTORY_GRPC_ADDR` 注入 SUT → A4 用例 → history/evidence/report；`build_mock_plan(project_root=...)` 将相对 descriptor 固化为项目绝对路径，保留手工 Plan 按 plan 目录解析的兼容行为；新增项目根路径回归与全链路证据断言，真实 e2e 1 passed，全量 596 passed / 48 skipped；Linux CI 待完成 |
| 跨协议 Mock 统一控制面 P15 | ✅ 2026-11-18 | 新增真实 Spring Boot + Java gRPC SUT 样例：`@Value("${inventory.grpc.addr}")`、`InventoryServiceBlockingStub`、proto 自动编译与 HTTP `/orders` 入口；`agent run` 完成 Java 画像 → gRPC Plan → gripmock → `INVENTORY_GRPC_ADDR` 注入 → 用例执行 → 证据/门禁，真实 e2e 1 passed；Python 与 Java 两条完整 SUT 链路均已覆盖 |
| Dubbo Triple unary P0 | ✅ 2026-11-18 | 新增 `TripleUnaryCaller`：`tri://`/`grpc://` 直连、动态 protobuf descriptor、unary request/response、deadline、metadata、gRPC status 归一；`DubboCall` 增加 protobuf/descriptor/grpc_path/compatibility；codegen 按 scheme 选择 sidecar 或 Triple adapter，并把相对 descriptor 固化到项目根；Triple provider 画像可从 `dubbo.protocol.name=tri` + proto service 匹配生成字段；新增真实 Dubbo 3.2.19 Triple provider 样例，`run --mode full` 完成画像 → 模板 → codegen → 真实 gRPC-compatible 调用，e2e 1 passed；开启 Triple/gripmock 联调的全量为 609 passed / 45 skipped；streaming/reflection/注册中心仍不做 |
| Dubbo Triple unary P1 | ✅ 2026-11-18 | 真实 Apache `online_bontique_demo` 试点：`tri` + Java interface（无 proto）通过 Dubbo 3.2.19 sidecar `GenericService` 调用成功，`AdsService#getAds` 返回真实 `AdResponse`；`tri://` 仅在 descriptor + protobuf 同时存在时走 Python gRPC-compatible adapter，否则走 Triple-native sidecar；画像记录 `protocol=tri`、`serialization=hessian2`、`compatibility=native`；完整 `run --mode full` 1/1 pass；源码 jar 忽略过期旁路 manifest，发布 wheel 仍严格校验；既有 Dubbo sidecar/case e2e 7 passed；开启 gripmock、Java、Triple 与真实样例联调的全量为 619 passed / 38 skipped |
| gRPC 下游 target 配置 P0 | ✅ 2026-11-18 | 补全 target 配置矩阵：Java `@Value` / `@ConfigurationProperties` / `System.getenv` / `System.getProperty`，Python settings/dotenv 属性，Node 生成客户端 + config/env 别名，Go 生成客户端 + `os.Getenv`/`viper.GetString`；Java 同时注入环境变量和 JVM `-D`，plan/manifest 记录 `config_key/config_kind`；硬编码 target 标 `grpc_target_hardcoded`；Java gRPC agent run e2e 验证 `JAVA_TOOL_OPTIONS` 实际生效；全量联调 627 passed / 38 skipped |
| 多行 fixtures 与父子表 P0 | ✅ 2026-11-18 | C5b fixtures 新增关系解析：子行可用 `{"$ref": "orders[0].id"}` 引用同批父行；单父键候选自动补子表外键并写 `fixture_fk_autofilled`，多父键未显式引用阻断 `fixture_fk_ambiguous`；至少 2 父 3 子计划按拓扑序 INSERT、逆拓扑逐行 DELETE，SQLite 真实 apply/teardown 通过；C5a/C5b 提示词补充多行父子示例；全量联调 631 passed / 38 skipped |
| `state_paths` 可执行展开 P0 | ✅ 2026-11-18 | 每条线性迁移路径自动生成 `state_path_*` DataState；`pending → paid → shipped` 按起点/终点去重生成 3 行，唯一主键由框架分配，父表/FK/NOT NULL 自动补齐，`target_state` 指向路径终点；路径子行共享同一父行；同时支持普通状态列与 `from_*/to_*` 成对字段；新增父表 + 3 状态子行 SQLite 真实 apply/teardown e2e；全量联调 632 passed / 38 skipped |
| HTTP 请求匹配与调用序列 P0 | ✅ 2026-11-18 | 新增 `MockRequestMatch` / `MockBindingRule` / `CaseMockRule`；session、DraftCase、TestCase、codegen、plan 全链路支持 `mock_rules` 与 `mock_sequences`；query/header/JSON body 子集匹配，rule 优先于 sequence，sequence 游标按 session+dep 隔离并在耗尽后回退 default branch；多条规则同时命中 502 `mock_request_ambiguous`；证据新增 `branch_source` / `sequence_index`；全量联调 635 passed / 38 skipped |
| HTTP Agent 正确性矩阵 P1 | ✅ 2026-11-18 | 真实 OkHttp 安全矩阵扩至 9 条：连接复用的同步/异步连续调用全部命中 Mock；客户端内部重试复用同一已改写 URL（2 次 Mock 请求、1 次改写）；3xx 返回 502 `mock_redirect_unsupported`；CertificatePinner 下显式改写为明文 Mock 可完成；应用拦截器拒绝 HTTPS→HTTP 时显式失败且真实下游 0 连接；直接使用 `/_dep/` 不二次改写；计划内未知路径进入 `/_agent/unmatched` 并留证；显式 Host 与改写前签名头保留，改写后应用拦截器看到 Mock URL；所有场景均核对零透传 |
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
| 多步迁移路径（历史阶段） | ✅ 2026-11-17 | 当时建立 `DataCatalog.state_paths` + `StatePath/StatePathStep`，最多 5 步/50 条，先作为只读描述；2026-11-18 已扩展为可执行 `state_path_*` 多行 DataState |
| 迁移后断言 | ✅ 2026-11-17 | `DataPlan.post_assertions`：正例 + `transition_*` + 单表单列 + 非成对字段 + 单列主键且单行时生成只读 `db_row` eq 断言；生成物在用例结束后、teardown 前执行；负例/成对字段/多行/复合主键不生成；测试 503 passed / 37 skipped |

## 历史进度

按轮次的实现细节已归档到 [`doc/PROGRESS-history.md`](doc/PROGRESS-history.md)。
