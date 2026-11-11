# AstSight

> **名称释义**：AST（抽象语法树）+ Sight（视野） ｜ **CLI**：`astsight` ｜ **Maven**：`io.astsight:astsight-core` ｜ **设计基线**：v4.7

**上一代平台问「你测了多少条」；AstSight 只回答「这次变更带来什么质量结论」。**

这个名字说的是它的地基：**先看得见结构，才谈得上判定。** AST 是前半段——框架靠代码图谱（tree-sitter 解析出的语法树）知道「改了什么」；Sight 是后半段——再靠契约与不变量说清「对不对」。缺了前半段，判定没有着力点；缺了后半段，看得再清也只是个图形化工具。


---

## 这是什么

一套**接口自动化测试引擎**（不是平台，不做用户管理/用例管理 UI/任务调度）。输入是代码库、需求与设计文档、接口契约（HTTP：OpenAPI / Spring MVC 注解；gRPC：proto/descriptor；Dubbo 2.7+：接口源码 + 标准注解/XML）、数据库结构；输出是**带证据等级的判定结论**，不是测试报告。

```
变更 / 全量 / 历史数据
        │
        ▼
   感知层（C2 变更感知 · C3 接口画像 · C3.5.1 Mock 语义 · C5a 造数目录）
        │
        ▼
   生成层（A1 业务规则 + CR · 业务/代码级草案 · 人工审核 · C4 用例库 · C6 编码）
        │
        ▼
   造数层（批准后 C5b 逐用例计划 · SQLite apply/teardown）
        │
        ▼
   执行层（C7 并行调度 · 冲突检测 · 重试收敛）
        │
        ▼
   判定层（契约与不变量 → 三态结论 + 证据）
```

核心立场：**判定能力（oracle）是整个框架的资产端，生成能力是消费端。** 生成会被模型迭代抹平，判据不会。

## 运行模式

同一个引擎，差异只在「选哪个范围」，是一个 `scope` 参数，不是多条 pipeline：

```bash
astsight run --mode full    --project order-svc   # 建基线：扫全量接口 → 画像 → 契约 → 生成 → 执行
astsight run --mode delta   --project order-svc   # 维护基线：只重判受影响范围
astsight run --mode cases   --project order-svc   # 只执行项目级用例库中已审核通过的用例
```

`replay`（复用历史数据重判）仍是设计目标，当前实现未接入。实现约束：模式是参数不是分支；
`full` 必须建契约基线（只生成用例不建基线 = 负债）；所有模式都必须把 `unmapped`
（没看到/没判到）单列进报告——**静默通过是最大风险**。

## 文档地图

| 文档 | 讲什么 | 状态 |
|---|---|---|
| `framework-capability-design.md` | **设计基线 v4.7**（含 v4.4/v4.6/v4.6.1/v4.7 决策）：C1–C7 能力规格、规则库 + 两级用例库、A4 规则驱动、C5a/C5b 造数与 LLM 边界、HTTP + gRPC + Dubbo 三协议接入、HTTP 明文 Mock（C3.5.5：唯一 `dep_id` + `route_key`）、Dubbo/gRPC 下游 Mock（gripmock 二进制按平台专用 wheel 随包）、多 SUT 并发隔离（5.5）、数据契约、门禁指标、风险对策 | 主文档 |
| `agent-architecture.md` | 节点切分与内部架构（A1 规则 Agent + C3.5.1 Mock 语义 + C5a/C5b 数据准备 + A4/A4.5 导入审核 + A5 诊断 + 可选 A6），确定性状态机边界 | 与主文档对齐 |
| `决策记录.md` | 设计决策的过程：运行环境基线、规则层取舍、Dubbo/gRPC Mock 选型（候选对比、spike 证据、落选原因、被推翻的判断）、版本变更历史、待复验项 | 决策记录 |
| `CodeGraph-集成设计.md` | 代码图谱接入方式（**代码嵌入**，MCP 已否决）、库 API 全清单、路由静默缺陷与交叉校验方案 | 补丁 v2.0 |
| `第三方方案理论依据分析-腾讯云API智能测试平台.md` | 同源竞品的理论依据核对（含 MAST 多智能体失败实证） | 对照 |
| `implementation-feasibility.md` | 立项前可行性调研（四来源交叉验证） | 历史 |
| `api-test-platform-feasibility.md` | 腾讯云方案可行性专项研究 | 历史 |
| `similar-articles.md` | 同类文章与开源项目清单 | 参考 |
| `AstSight-使用文档.md` | **全项目使用文档**：安装/配置、C1 规则、A4 用例、C5 造数、Mock、三种运行模式、CodeGraph、Agent、产物与门禁、排错与命令速查 | 使用文档 |
| `C1-规则生成使用文档.md` | C1 专题深入：单命令与两段式流程、需求/设计/OpenAPI 输入、`ruleset` 产物、校验门禁、重复执行与排错 | 专题 |
| `engine/README.md` | 正式实现进度（M1 delta 闭环 / 规则→审核用例→版本化 / C5 造数 / CodeGraph 抽象） | 代码 |

> **设计文档口径（v4.3，v4.5 增补多 SUT 隔离，v4.6 删除 Feign 特殊支持，v4.6.1 收紧 HTTP Mock 范围，v4.7 规则驱动 A4 与 C5a/C5b）**：`framework-capability-design.md` 与 `agent-architecture.md`
> 已改为“需求/设计文档 + 契约源 → A1 LLM 需求分析（`a1_requirement_analysis`）→ 业务规则库
> → 业务级用例（规则驱动）+ 代码级用例（变更缺口补充）→ 人工审核 → 版本化用例库 → pytest”。
> 契约源按 transport 取值：HTTP 按 `OpenAPI > Spring MVC 注解`；gRPC 用仓库
> proto/descriptor；Dubbo 2.7+ 用接口源码 + 标准注解/XML。HTTP 客户端（含 Feign）一律按普通
> 出站调用处理，不新增 transport，也不做客户端框架专用支持。gRPC 仅 unary；Dubbo 走独立 Java sidecar 泛化调用（Hessian2），调用 SUT 不使用
> Java agent；Dubbo 3 + Triple + protobuf + descriptor + 无 group/version 可路由 gRPC，其余走 sidecar。
> 无可用路径标 `unsupported`，不静默降级；gRPC→sidecar 回退仅限 Dubbo 家族、默认关闭、
> 需显式 `--allow-transport-fallback`。v4.3 起 Mock 覆盖 SUT → HTTP **与 Dubbo 2.7+** 下游：
> HTTP 走通用配置/服务发现/代理覆盖 + 会话，Dubbo 走 **agent + ByteBuddy + Filter** 低侵入拦截
> （`serialization_verified=false`），测试环境不由框架负责；SUT → gRPC 下游 v4.3 仍标 `mock="unsupported"`，
> v4.4 决策改为 **gripmock 假服务器 + target 重定向**（`wire_roundtrip`，每命名空间一实例，见能力设计 C3.5.4）；
> v4.5 起二进制按平台专用 wheel（`astsight[grpc-mock]`）随包分发，用户无需单独下载。
> C2 只感知被测仓库的提供方端点，并做 base/target 契约 diff
> 以覆盖纯契约变更；同一实现多协议暴露时保留全部端点，按 `business_key` 归组为
> `primary/alias/independent`，默认 primary 全量 + alias 契约冒烟。规则是设计期资产，不执行表达式、不作运行时 oracle；C7 仍以
> 已批准用例断言 + 接口契约 为准。v3.0 的“纯用例、无规则层”方案只保留在版本记录中，不代表现行设计。
> v4.5 起同环境并发多个 SUT 按 **5.5** 隔离：`sut_id` 为隔离单元，框架端口原子分配，
> 命名空间等于 `sut_id`，DB/用例库按 run 作用域隔离。
> v4.6 起删除 Feign 特殊支持：Feign 只作为普通 HTTP 客户端，通用机制覆盖不了的下游直接标
> `mock="unsupported"` / `unidentified`，依赖它的用例显式排除或 `blocked`，不做专用兜底。
> v4.6.1 起 HTTP Mock 首版只支持明文 HTTP：HTTPS 下游标 `tls_not_supported` 且用例 `blocked`，
> 不降级、不 MITM；下游 `dep_id` 改为 `http:{downstream_key} {METHOD} {path}`，同路径跨服务不重名、
> 同端点多调用点不重复，运行期用 `route_key` 路由（见能力设计 C3.5.5）。
> v4.7 起 A4 只消费 `ruleset + profiles + mock_semantics + data_catalog`，不再先做自由语义分析；
> C5 拆为 A4 前的 C5a 数据目录和批准后的 C5b 逐用例计划。C5 使用大模型补字段业务语义与
> 业务态候选，但 DDL/FK/拓扑/类型枚举校验/参数化 SQL/apply-teardown 全部确定性；模型不产出 SQL。
> v1 注入只承诺**下游专属 `url_override`** 与**显式 `http_proxy`**；`discovery_override`/透明代理条件支持，
> 共享 base URL（`mock_config_ambiguous`）、同 JVM bean mock、动态 URL、请求匹配/序列/流式不支持。

## 命名边界（别混）

| 说的是 | 实际是什么 | 关系 |
|---|---|---|
| **AstSight** | 本项目 | — |
| `deepx-code`（itmisx） | 开源参考项目 | 借鉴图谱操作抽象 |
| `@colbymchenry/codegraph` | 底层基础设施 | 零件，不是整机 |
| TestHub | 旁挂对接的测试资产平台 | 外部系统 |
| ~~Plumbline / 准绳~~ | v2.13 曾用名 | **已弃用**，统一改用 AstSight |
| ~~Ruling~~ / ~~DeltaQ~~ / ~~DeltaProof~~ | 更早评估的备选名 | **已弃用**，勿再使用 |

## 代码

当前可运行的里程碑是 **M1 确定性闭环 + 规则驱动用例 + SQLite 造数**（LLM 为可选适配层）。
C2 变更感知已支持 **Python AST + CodeGraph 文件符号表**：Java/Spring 项目可做到
`git 分支 → 受影响 API 列表`（API 级）；非 Python 行的符号边界来自 CodeGraph，
框架内不另写解析器。

已落地：规则层（A1 BR + CR）、两级用例（business/code）、C3.5.1 Mock 语义、HTTP 下游
画像/明文 HTTP Mock、C5a/C5b 与 SQLite apply/teardown、规则驱动 `agent run`。
未落地：gRPC Mock（C3.5.4 已决策）、Dubbo 下游 Mock 的 Java agent 实现、多 SUT 隔离实现、
MySQL/PostgreSQL 造数方言、replay 模式、L2–L4 断言强度。旧
`context → draft → 审核 → caseset` 直接生成链路保留为兼容模块，不再是 canonical。
v4.2 明确三种 transport：**HTTP、gRPC（仅 unary）、Dubbo（仅 2.7+）**；HTTP
仍是主路径，HTTP 客户端（含 Feign）按普通出站调用处理，gRPC 走 proto/descriptor，Dubbo 走 sidecar；Thrift 不考虑。
`unsupported` 端点显式列出，不静默降级。

```bash
cd engine
pip install -r requirements.txt            # langgraph / pydantic / httpx / PyYAML / pytest / fastapi / uvicorn

# CodeGraph 运行时：代码感知/调用链/非 Python 项目必需。
# 安装到用户级缓存，所有项目复用（Windows: %LOCALAPPDATA%\AstSight\codegraph，
# Linux/macOS: ~/.local/share/astsight/codegraph）。
python -m astsight codegraph install
python -m astsight codegraph status

# 一条命令：建临时 git 仓库 → 起示例服务 → delta 全链路
python examples/demo_delta.py
python examples/demo_delta.py --defect     # 注入 500，验证门禁失败与归因

# 需求 → 草案 → 审核 → 版本化 → 执行（示例自动批准仅演示用）
python examples/demo_case_review.py
python examples/demo_case_review.py --no-auto-approve  # 停在待审 proposal
# Claude Code Skill 包装：.claude/skills/requirements-to-cases/SKILL.md
# （只生成草案并 cases import，禁止自动 approve）

# 对已有仓库
PYTHONPATH=. python -m astsight run \
  --project /path/to/repo --mode delta --base main --target feature \
  --base-url http://127.0.0.1:8010 \
  --openapi /path/to/repo/openapi.json

# 规则驱动：先生成规则上下文，再由模型/人工写 rules.draft.json
PYTHONPATH=. python -m astsight rules prepare \
  --project /path/to/repo \
  --requirements /path/to/repo/docs \
  --openapi /path/to/repo/openapi.json
PYTHONPATH=. python -m astsight rules import \
  --project /path/to/repo \
  --context /path/to/repo/.astsight/rules/<analysis_id> \
  --draft /path/to/rules.draft.json

# 生成端点上下文包，按 prompts/a4_case_design.md 写 cases.draft.json
PYTHONPATH=. python -m astsight cases prepare \
  --project /path/to/repo \
  --requirements /path/to/repo/docs \
  --base-url http://127.0.0.1:8010

# 导入草案（只生成待审 proposal，不改用例库）
PYTHONPATH=. python -m astsight cases import \
  --project /path/to/repo \
  --context /path/to/repo/.astsight/casegen/<context_id> \
  --draft /path/to/cases.draft.json \
  --ruleset ruleset.json

# 人工编辑 proposal 下的 cases.review.json 后批准
PYTHONPATH=. python -m astsight cases approve \
  --project /path/to/repo \
  --proposal /path/to/repo/.astsight/cases/proposals/<proposal_id> \
  --reviewer alice

# C5a 造数目录（有 DDL 时）→ 批准后 C5b 计划
PYTHONPATH=. python -m astsight data catalog --project /path/to/repo \
  --ddl /path/to/repo/schema.sql
PYTHONPATH=. python -m astsight data plan --project /path/to/repo \
  --catalog .astsight/data/data_catalog.json --cases caseset.json

# 只执行已审核通过的用例
PYTHONPATH=. python -m astsight run --project /path/to/repo --mode cases \
  --base-url http://127.0.0.1:8010 \
  --ruleset ruleset.json --data-plan .astsight/data/data_plan.json

# 或一条命令跑完整 LLM 编排（需真实 key）
python examples/demo_agent_flow.py --keep
```

项目级用例库默认是 `<project>/caseset.json`（`--case-base` 可改）。`TC-NNNN` 只在审核批准
写库时分配，编号永不复用；更新/恢复沿用原编号并升版本，旧版本进入 `history` 标
`superseded`。待审草案在 `.astsight/cases/proposals/`，默认不阻塞运行但报告标 `degraded`；
`--fail-on-pending-proposals` 可升级为门禁 blocker。

目录：`astsight/`（M1 流水线）· `astsight/rules/`（规则库/CR/diff）·
`astsight/cases/`（上下文/校验/proposal/用例库/模板用例）· `astsight/data/`（C5a/C5b/SQLite）·
`astsight/mock/`（C3.5.1 语义与 C3.5.5 服务）· `astsight/agent/`（LLM 适配与编排）·
`contracts/`（数据契约）· `codegraph/`（图谱客户端抽象）· `orchestration/`（LangGraph 编排骨架，
当前 CLI 主路径未接入，实际执行走 `astsight/pipeline.py`）· `examples/`（示例服务与 demo）· `tests/`。

M1 的边界：用例断言仍以 L1 为主（状态码/字段存在/字段值），需要数据库前置状态的用例
用 `preconditions.state_key` + C5b fixture，无 plan/DB 时标 `blocked`；自动“矫正”只含生成物
自检与环境抖动重试；审核必须人工完成（`cases approve --reviewer`），框架不提供自动全量批准；
A6 默认关闭。未覆盖范围、未执行用例、待审 proposal、契约/需求/规则漂移在报告中单列，
不静默通过。

**运行环境基线：Linux（服务器 / CI 容器）**。Windows 只是开发机本地调试环境，不承担部署与验收；
路径分隔符、子进程、信号、端口绑定、文件锁均按 POSIX 语义设计。

开发机可直接用的解释器（已装齐依赖）：`C:\Users\magic\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
