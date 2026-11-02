# AstSight Agent 架构设计（DeepAgents × CodeGraph × LangGraph）

> 项目名：**AstSight**（AST + Sight）｜ CLI：`astsight` ｜ Maven：`io.astsight:astsight-core`
> 本文回答两个问题：**框架需要几个 Agent**、**每个 Agent 的内部架构是什么**。
> 技术栈：DeepAgents（多智能体编排框架）+ CodeGraph（代码图谱基础设施）+ LangGraph（图式状态机编排）。
> 前置文档：`framework-capability-design.md`（能力设计 v4.7）。本文是能力设计在技术实现层的落点，非替代。

> **当前口径**：本文只描述当前目标架构与节点约束。决策过程、候选取舍与版本历史见 [`决策记录.md`](决策记录.md)（规则层 §2、Dubbo Mock §3、gRPC Mock §4、多 SUT 隔离 §5、Feign 特殊支持删除 §6、HTTP Mock 仅明文与 `dep_id` 唯一性 §7）。
> **实现状态**：`engine/` 仍是 v3.0 的 `context → draft → 审核 → caseset` 链路；规则层与两级用例尚未实现。本文是目标架构，落地前以代码为准。

---

## 一、Agent 切分原则（为什么是这几个，不是按能力 1:1）

**核心原则：Agent 的边界按"是否需要 LLM 自主决策"切分，不按能力清单机械映射。**

| 判据 | 结论 |
|---|---|
| 需要 LLM 自主决策（理解/推理/生成） | → 做成 **Agent**（智能体） |
| 纯确定性逻辑（解析/计算/追溯/执行） | → 做成 **能力服务（Capability Service）**，即多个工具的编排封装 |

> **能力服务 ≠ Agent**。能力服务是"纯确定性工具的流水线封装"，有固定的输入输出契约、无 LLM、无自主决策、无 prompt；Agent 是"带 prompt、会思考、能自主决策"的智能体。二者在编排图上都是节点，但性质完全不同——**不能因为"它在编排图上是节点"就把它叫 Agent**。

**反面教训（TestGen AI）**：把 9 个纯模板活也做成 Agent，结果全是空壳 prompt、九选一单选路由，不是真正的多智能体协作。本设计避免重蹈：确定性能力全部下沉为**能力服务**，Agent 只保留"需要思考"的部分。

---

## 二、总体：2 个框架内 Agent + 外部草案生成 + 能力服务 + 编排层 + 共享工具

```
                     ┌─────────────────────────────────────────┐
                     │        Orchestrator（编排器）             │
                     │   LangGraph StateGraph 根节点             │
                     │   入口分流：需求/契约变更? / 代码变更?      │
                     └───────────────┬─────────────┬───────────┘
                                     │             │
             ┌───────────────────────┘             └───────────────────────┐
             ▼（需求/契约变更）                                              ▼（代码变更）
   ┌───────────────────────┐                                  ┌───────────────────┐
   │ A1 需求分析 (Agent)     │                                  │ S2 变更感知 (服务) │
   │ 文档+契约→规则集        │                                  │ 分支差异→变更接口   │
   │ LLM: a1 prompt         │                                  └─────────┬─────────┘
   └───────────┬───────────┘                                            ▼
               ▼                                              ┌───────────────────┐
   ┌───────────────────────┐                                  │ S3 画像构建 (服务) │
   │ A4 业务级用例导入(服务) │                                  │ 接口→档案+下游依赖 │
   │ 规则→草案→proposal     │                                  └─────────┬─────────┘
   └───────────┬───────────┘                                            ▼
               │                                              ┌───────────────────┐
               │                                              │ A4 代码级用例导入  │
               │                                              │ 变更→缺口→proposal │
               │                                              └─────────┬─────────┘
               └──────────────────────────┬───────────────────────────┘
                                          ▼
                              ┌───────────────────────┐
                              │ A4.5 审核检查 (人工断点)│ ◄── 待审/未批准则阻断
                              └───────────┬───────────┘
                                          │（approved cases）
                                          ▼
                              ┌───────────────────────┐
                              │  共享工具层（Tools）     │
                              │  CodeGraph 客户端       │
                              │  接口解析器 / DB 分析器  │
                              │  造数器 / Mock 编排器   │
                              │  执行器 / 报告器        │
                              └───────────┬───────────┘
                                          ▼
                              ┌───────────────────────┐
                              │ A5 结果诊断 (Agent)    │
                              │ 失败 → 归因 → 重试决策 │
                              └───────────────────────┘
```

**节点职责速览**：

| 节点 | 对应能力 | 形态 | 需要 LLM 吗 | 职责一句话 | 触发条件 |
|---|---|---|---|---|---|
| A1 需求分析 | C1 | **Agent** | 是 | 文档+契约源 → 规则集（BR/CR/questions/gaps） | 需求/设计文档或契约有变更 |
| A4 业务级用例导入 | C4a | **能力服务** | 否 | 规则+context+草案 → 校验/diff → proposal | 规则变更或业务草案产出后 |
| A4 代码级用例导入 | C4b | **能力服务** | 否 | 变更+画像+规则+草案 → 补缺口/校验/diff → proposal | 代码变更或代码草案产出后 |
| A4.5 审核检查 | C4 | **人工断点** | 否 | 检查决定完整、乐观锁、approved 用例 ≥1 | 跟随 A4 |
| S2 变更感知 | C2 | **能力服务** | 否 | 分支差异 → 变更接口列表 | 代码分支有变更 |
| S3 画像构建 | C3 / C3.5 | **能力服务** | 否 | 接口 → 完整档案+下游依赖 | 跟随 S2 |
| C5a 数据目录 | C5a | **能力服务 + LLM 适配** | 是（语义辅助） | DDL+seed+画像 → 表/列/业务态目录 | 事实源就绪、A4 之前 |
| C5b 造数计划 | C5b | **能力服务 + LLM 适配** | 是（业务态候选） | 已批准用例+catalog → 逐用例计划 | A4.5 通过后 |
| A5 结果诊断 | C7.3 | **Agent** | 是（弱） | 失败 → 归因 → 重试决策 | 执行失败时 |
| — Orchestrator | 全流程 | 编排器 | 是（调度） | 串起全链路，管理状态 | — |

> 注：A1 是框架内的 LLM 分析 Agent（需求 → 规则）；A4 是确定性的导入/校验/版本化节点，**框架内不生成用例草案**——业务级/代码级草案由规则驱动的适配层/人工产出。C5 拆为 **C5a 数据目录**（A4 之前，LLM 只补字段业务语义与候选状态）与 **C5b 造数计划**（A4.5 之后，LLM 只补业务态/取值候选）；DDL 解析、FK 闭包、拓扑排序、白名单/类型/枚举校验与 SQL 渲染均为**确定性核心**，模型不输出 SQL。C6 的模板编码、C7.1/C7.2/C7.4 也是确定性工具（图中归入共享工具层）。A5 是另一个框架内 Agent（失败诊断）；A6 自修复是**可选扩展**，只能生成新草案并重新走 proposal/approve，不能直接改用例。A1 与 S2 是**两个独立入口**，分别对应“需求/契约变更”与“代码变更”，可独立触发也可同时触发。

---

## 三、每个节点的架构

> 统一用"输入 → 内部节点（LangGraph 子图）→ 输出 → 关键决策"描述。Agent 和能力服务自身都是一个 LangGraph 子图（subgraph），可独立运行、可被编排器调度。区别仅在：Agent 内部有 LLM 节点 + prompt，能力服务内部全为确定性节点。

### A1 需求分析 Agent（LLM：文档 → 业务规则）

**职责**：把需求/设计文档 + 契约源（HTTP：OpenAPI / Spring MVC 注解；
gRPC：proto/descriptor；Dubbo 2.7+：接口源码 + 标准注解/XML）组织成分析上下文，调用 LLM（主提示词
`prompts/a1_requirement_analysis.md`）产出结构化业务规则，并完成规则校验、契约规则提取与
规则库 diff。**规则只用于设计期，不进入运行时判定。**

```
输入：需求/设计文档 + 契约源（HTTP：OpenAPI / Spring MVC 注解；
      gRPC：proto/descriptor；Dubbo：接口源码 + 标准注解/XML）
      │
      ▼
┌─────────────────────────────────┐
│ 1. 文档切块节点（确定性）          │  section 路径 + 原文 + sha256
│ 2. 元数据排除节点（确定性）        │  版本/目录/术语等不计入覆盖率分母
│ 3. 契约端点目录（确定性）          │  按 transport 解析参数/响应；unsupported 单列
│ 4. 分析上下文固化（确定性）        │  context_id = docs + 契约 hash + prompt 版本
│ 5. LLM 规则分析节点（Agent）       │  a1_requirement_analysis.md
│    输出 questions/rules/gaps       │  BR-NN + authority A/B + source
│ 6. 规则导入校验（确定性）          │  schema/来源 section/A 级落地/rule_key
│ 7. CR 契约规则提取（确定性）       │  契约源 required/type/enum/min/max
│ 8. 规则库 diff（确定性）           │  new/updated/deprecated/disputed
└─────────────────────────────────┘
      │
      ▼
输出：ruleset.json（version/hash）+ rules import 校验报告
```

**关键决策**：
- **A1 是框架内唯一的“需求理解”LLM 节点**：它只产出规则，不产出用例；用例草案在 A4 的输入侧
  由 Skill/人工/适配器生成
- **LLM 节点在框架外执行**：框架只负责 `rules prepare`（context/prompt/schema）与
  `rules import`（校验/diff/版本化），默认不引入模型 SDK；Claude Code Skill、人工或未来
  适配器产出 `rules.draft.json`，与 C4 草案同一模式
- **提示词即契约**：输出字段与 `framework-capability-design.md` 3.1.1 对齐；
  `source.section` 按稳定 section 路径解释（可附原文章节号）
- **不臆造**：文档没写清的进 `questions`，抽不出的进 `gaps`；`rules` 中不得出现原文没有的
  具体数字/枚举，A 级落地校验失败自动降级 B
- **规则不入运行时**：C7 不读 `ruleset.json` 做判定；规则只用于 C4 设计、变更选择、覆盖统计与漂移
- **无独立规则审批**：规则被 approved 用例引用后才算确认；A4.5 审核文件展示规则依据
- **可重跑但非确定**：记录 prompt 版本与 model；同一输入可能得到不同规则，由用例审核收敛
- **契约源按 transport 取值，不强制 OpenAPI**：HTTP 按 `OpenAPI > Spring MVC 注解 > 文档`；
  gRPC 用仓库 proto/descriptor；Dubbo 用 2.7+ 接口源码 + 标准注解/XML。HTTP 全部缺失时 CR 为空、标
  `contract_incomplete`；gRPC/Dubbo 无可用契约 → `unsupported`（`no_descriptor` /
  `interface_in_jar` 等），不猜字段，A4 不得生成对应契约断言用例
- **HTTP 客户端不作契约源**：`@FeignClient` 等客户端声明只是消费方，不参与端点与契约提取；
  A1/S1 不为任何客户端框架写专用解析分支，解析不出的部分标 `inferred`，不臆造
- **Dubbo/gRPC 端点识别零 LLM**：只认 proto/descriptor 与全限定注解/XML；2.6、接口在 jar、
  动态代理/反射/`getBean`、streaming/oneway 一律 `unsupported`，不靠模型补全
- **transport 在 A1 context 中固化**：每个端点携带 `transport/transport_class/transport_evidence`，
  后续 A4/A4.5/C6/C7 不再猜测或改写

### A4.5 用例审核检查（人工断点）

**定位**：A4 `cases import` 只生成待审 proposal；A4.5 检查审核结果，决定能否进入 C6。
**没有独立的“规则确认”节点**：规则确认隐含在用例审核中——审核文件展示 `rule_refs` 的
原文、`authority`、`source.kind` 与 `reasoning`，批准引用 B 级规则的用例即视为确认该规则。

**检查项**：

| 检查 | 通过条件 | 不通过 |
|---|---|---|
| proposal 存在 | 有 `proposal.json` 且未应用 | 阻断 |
| 决定完整 | 所有 actionable 项 = approve/reject，无 pending | 阻断 |
| 至少一条可执行 | approved case ≥1 | 阻断 |
| 两级完整性 | business：`rule_refs`+`requirement_refs`；code：`rule_refs`+`change_refs` | 阻断 |
| transport 一致 | 用例 `transport/endpoint` 与画像、context 完全一致；请求/断言字段属于该 transport | 跨 transport 字段 → 阻断 |
| 覆盖角色 | `coverage_role` 与画像一致；每个 `business_key` 只有一个 primary；alias 至少一条成功冒烟 | 角色不一致 / 无 primary → 阻断；alias 超额 → `alias_over_coverage` 警告且不计业务覆盖 |
| transport 范围 | `--transport-scope=primary` 时 alias 项不进 proposal，汇总 `transport_alias_skipped`；`equivalent` 跳过必须带 `adapter_fingerprint` | 静默跳过 → 阻断；`transport_alias_skipped` 未出报 → 报告不完整 |
| Mock 绑定 | `mock_bindings.dep_id` 在画像中且属于 `type=http` 或 `type=dubbo`（2.7+）下游；`branch` 在语义分支中；HTTP 下游给出 `mock.strategy`、`route_key` 与实际配置键（或发现/代理覆盖证据），且策略属于 v1 边界（承诺：下游专属 `url_override`、显式明文 `http_proxy`；条件：`discovery_override`、透明代理），Dubbo 下游标 `mock.strategy="agent_filter"` | 缺失或不可解析 → 阻断；绑定 `mock="unsupported"` 的下游（gRPC、HTTPS、共享 base URL、动态 URL 或无法覆盖的 HTTP）→ 阻断；以条件能力冒充 v1 承诺 → 阻断；只绑 `success` 却声称测降级 → `weak_oracle` |
| 不可 Mock 下游 | 依赖 SUT → gRPC 下游的用例已显式排除或标 `blocked`，且审核文件展示原因 | 静默放行 → 阻断 |
| gRPC Mock（v4.4） | 绑定 gRPC 下游时，执行计划声明 `mock.strategy="grpc_server"`、Mock 服务器 endpoint 与 descriptor 来源；审核文件展示 `wire_roundtrip` 与已知 int64 匹配限制 | 未声明 / descriptor 缺失 / 服务器未就绪 → 阻断 |
| Dubbo agent 前提 | 用例绑定 Dubbo 下游时，执行计划必须声明 agent 加载方式（`javaagent`/`classpath`）与兼容矩阵；`serialization_verified=false` 必须在审核文件展示 | 未声明 / 矩阵未覆盖 → 阻断 |
| 规则引用有效 | `rule_key` 可解析；非 `disputed`/`ungrounded`；C/D 不得单独作 oracle | 阻断 |
| 规则漂移 | 引用 `rule_drift` 规则的用例未标 `stale`，或已显式降级 | 阻断 |
| 乐观锁 | `base_store_version` + `base_store_hash` 匹配 | 阻断，要求重新 import |
| 审核人 | `--reviewer` 非空 | 拒绝批准 |
| deprecate | 逐项 approve | 未批准则保持原状 |

**关键决策**：
- 框架不自动批准；demo 的自动批准只对 `new/update/revive`，绝不自动 `deprecate`
- reject 不进入 store；需要改用例时改草案重新 import，不能在 review 文件里直接改内容
- approve 重新校验/重算 fingerprint，review 文件只取 decision/reviewer/note
- 审核日志写入 `review_log`，`approval.json` 标记 proposal 已应用
- 与 6.5 缺陷案例入库的区别：审核对象是用例，时机在 C6 之前，目的是防止未审用例进入执行

### S2 变更感知能力服务（分支差异 → 变更接口）

**职责**：从 git 分支差异精确推导受影响接口（`changed_apis.json`）。

```
输入：仓库 + base/target 分支 + base/target 契约源
      │
      ▼
┌─────────────────────────────────┐
│ 1. git diff 节点（确定性）         │  变更文件 + 函数级 AST diff
│ 2. 契约 diff 节点（确定性）        │  base/target 契约解析 + 端点级差异
│ 3. 图谱追溯节点（确定性，CodeGraph）│  反向追溯至端点层（深度可配）
│ 4. 端点匹配节点（确定性，插件）     │  仅提供方：HTTP 路由 / gRPC service impl / Dubbo service
│ 5. transport 判定节点（确定性）     │  http/grpc/dubbo + evidence；不可用标 unsupported
│ 6. 多协议归组节点（确定性）         │  business_key（角色由 S3 结合画像确定）
│ 7. 置信度判定节点（确定性）         │  high/medium/low
│ 8. 低置信度分流节点（确定性）       │  仅列报告供人工勾选，不进下游
└─────────────────────────────────┘
      │
      ▼
输出：changed_apis.json（含 transport/endpoint/business_key/change_kinds/contract_change/
      confidence/trace/unsupported_reason）+ contract_diff.json
```

**关键决策**：
- **纯确定性，零 LLM**——变更感知要的是精确，不是创造力。原"弱 LLM 复核"改为人工复核（编排层的人工断点），避免为一句可读性描述引入不确定性
- 低置信度**不进入下游**，只在报告列出供人工勾选
- 语言不支持 → 该文件/符号跳过，不做降级兜底（见下）
- **三种 transport 分派识别**：HTTP 走框架路由插件；gRPC 走仓库 `.proto`/descriptor 的 service/method；Dubbo 走 `@DubboService`/`@DubboReference` 全限定名与 XML。2.6、接口在 jar、动态代理/反射/`getBean`、无 descriptor 一律 `unsupported`，不从 HTTP 路由“猜”对应关系
- **transport 判定只做一次**：S2 输出 `transport/transport_class/transport_evidence`，S3 不重新判定；Dubbo 家族中证据不足时默认 `dubbo` + `inferred`，但 `unsupported` 不得被 `inferred` 掩盖
- **只收提供方**：HTTP 客户端、`@DubboReference`、gRPC stub 是消费方，写入 S3 的 `downstream_deps`，不作为被测端点；把消费方当端点会测到仓库外实现且无法追溯
- **不追溯仓库外变更**：依赖 jar、外部仓库、注册中心实现不在 `git diff` 内；仅依赖版本变化标 `external_dependency_change`，不生成外部端点用例
- **纯契约变更必须走 diff**：只改 `.proto`/OpenAPI/接口签名时调用链是断的，S2 用 base/target 契约 diff 产出 `change_kinds=contract` 与 `contract_change`；解析失败标 `contract_diff_unavailable`，不得当作“无变更”
- **多协议端点全部保留**：`business_key` 只做归组，不按方法名合并；`coverage_role` 由 S3 结合画像与 `adapter_fingerprint` 确定，C4 只消费

**CodeGraph 接入方式（代码嵌入）**：

> 第 2 步「图谱追溯」依赖 CodeGraph（`colbymchenry/codegraph`）。本节点通过**代码嵌入**接入：常驻 Node worker 进程内直接 `import` 其库 API，经 stdio JSON 调用；**不走 MCP**（MCP 返回面向模型阅读，非结构化事实源），CLI 仅用于诊断。**不自研 AST 图谱**，但**自研路由插件**——理由见下。

- **接入方式**：S2 调 `getImpactRadius` / `getCallers` / `getCallees` 做反向追溯；AstSight 只做"编排 + 证据等级判定 + 分流"
- **符号级 → 接口级的换算必须显式做**：`impact` 给的是符号闭包，C2 要的是接口清单。客户端内部与路由表求交后返回 `affected_routes`，**不允许**把符号名列表当接口清单上报
- **深度用尽即降级**：`depth_used >= depth` 时标 `truncated`，证据等级不得升为 `verified`（闭包可能没走完）
- **语言不支持 → 直接跳过**：该变更符号标记 `unsupported` 并从变更集剔除，报告显式列出"因语言不支持未追溯的符号"——**宁可漏，不可错**
- **证据等级自行判定**：实测 CodeGraph 不输出 `FACT/POSSIBLE/UNKNOWN` 字段，`evidence` 由 AstSight 按「双源独立一致 → `verified`；单源 → `inferred`；冲突 → `unmapped`」判定

### S3 画像构建能力服务（接口 → 完整档案 + 下游依赖）

**职责**：为变更接口建立唯一事实源（`api_profiles.json`）。

```
输入：ChangedAPI[] + 契约源（HTTP：OpenAPI/Spring MVC 注解；gRPC：proto/descriptor；
      Dubbo：接口源码 + 标准注解/XML）+ DDL + context + 术语表
      │
      ▼
┌─────────────────────────────────┐
│ 1. 契约提取节点（确定性）          │  按 transport 分派；无可用契约标 unsupported
│ 2. DB 映射节点（确定性）          │  字段↔表列映射，命名归一
│ 3. 三路约束校验节点（确定性）      │  契约源/DB 取最严；需求原文仅作审核依据
│ 4. 下游识别节点（确定性，CodeGraph）│ 正向追溯（出站 HTTP 调用点/gRPC stub/Dubbo 引用/MQ/Redis）
│ 5. 下游可 Mock 判定（确定性）      │ HTTP→mock（覆盖不了→unsupported）；Dubbo 2.7+→agent_filter；gRPC→unsupported（v4.4 起 grpc_server）
│ 6. 多协议归组节点（确定性）        │  business_key + adapter_fingerprint + coverage_role
│ 7. 语义附着节点（确定性）          │  术语表 + 需求 section/quote + DB comment
└─────────────────────────────────┘
      │
      ▼
输出：api_profiles.json（transport + 调用字段 + business_key/coverage_role/
      adapter_fingerprint + downstream_deps + side_effect 护栏标记）
```

**关键决策**：
- **纯确定性，不放 LLM**——画像是下游一切的事实源，错一处全盘错。这也是它必须做成能力服务（而非 Agent）的根本原因：Agent 的自主性在事实源环节是风险而非收益
- 下游识别的副作用字段已降级为"安全护栏标记"（见 v2.9），只需标出资损类写操作
- 画像构建常与 S2 串行：S2 输出 changed_apis → S3 只画像变更接口（避免全量画像浪费）
- **transport 原样承接 S2**：S3 按 `transport` 分派契约提取；HTTP 用 OpenAPI/Spring MVC 注解，
  gRPC 用 repo proto/descriptor，Dubbo 用接口源码 + 注解/XML。`unsupported` 端点不建画像，直接进报告
- **多协议按 `business_key` 归组，不按方法名合并**：同一实现暴露多协议时保留全部端点；
  `primary` 由 `transport_priority` 选（未配置默认 `http > grpc > dubbo`，标 `primary_inferred`），
  其余为 `alias` 或 `independent`；`alias` 必须带 `alias_of`
- **`adapter_fingerprint` 决定 alias 能否跳过**：对请求/响应模型、序列化、错误映射、鉴权/拦截器、
  校验位置做哈希；相同才允许 `equivalent` 跳过，不同至少跑契约冒烟，算不出按冒烟处理
- **不扩展新 Agent 角色**：多协议只是 transport 维度的分派，S2/S3 仍是确定性能力服务；
  不因为 Dubbo/gRPC 增加一个“RPC Agent”。v4.3 的 Java agent 是**执行期 Mock 注入组件**（C3.5.3），
  不是编排层 Agent
- **Dubbo 画像只含可静态证明的字段**：`interface/method/param_types/return_type/group/version`
  来自源码或 XML；接口在 jar、动态代理/反射/`getBean` 直接 `unsupported`，不尝试反编译或运行时反推
- **gRPC 画像不含 HTTP 语义**：`request_type/response_type` 是 proto message，请求/响应字段按
  message 建模；不生成 `method/path`，不套用状态码
- **下游按类型分档**：HTTP 与 Dubbo 2.7+ 下游可 Mock（HTTP 靠通用配置/发现/明文代理覆盖，首版不支持 HTTPS，覆盖不了标 `unsupported`；Dubbo 标 `mock="agent_filter"`，
  需执行期 agent 自检）；gRPC 下游只观察，`control="observe"`、`mock="unsupported"`，
  依赖用例显式排除或 `blocked`，不允许静默真实调用（v4.4 决策改为 `mock.strategy="grpc_server"`，
  由独立 gripmock 实例承载，见能力设计 C3.5.4）
- **HTTP 客户端一律按普通出站调用处理**：不按客户端框架分类，`type=http`，不新增 transport
  或画像类型。识别调用点、目标地址/端口与可覆盖的配置/注入点；服务发现地址不可静态确定时
  标 `inferred`，调用点无法静态定位时标 `unidentified` 进缺口清单
- **下游 `id` 必须稳定且唯一**：HTTP 下游取 `http:{downstream_key} {METHOD} {path}`（如
  `http:svc:pay-service POST /gateway/pay`），`downstream_key` 按 `svc:` > `cfg:` > `host:` >
  `call:` 取值；路径占位符归一，同一端点多调用点合并，不按调用点拆分。不同下游同路径不撞 id，
  同一端点多调用点不重复；归一后仍冲突标 `dep_collision` 阻断。完整规则见能力设计 C3.5.5
- **下游契约不取自客户端声明**：按 `OpenAPI > 接口文档 > 运行时观测` 取值，客户端 DTO 只作
  线索并标 `inferred`；跨仓库 provider 不可见时标 `unmapped` 进缺口清单，不猜

**CodeGraph 接入方式（与 S2 同源，代码嵌入）**：

> S3 的第 4 步「下游识别」，与 S2 共用**同一个 worker 进程与同一份图谱索引**（一仓一实例），只是遍历方向相反：S2 反向（变更→路由），S3 正向（路由→下游）。

| 节点 | CodeGraph 可提供 | 映射的AstSight字段 |
|---|---|---|
| 路由清单（第 1 步） | `getRoutingManifest` + `getNodesByKind("route")`，**仅供交叉校验** | HTTP `apis[]` 的 handler 绑定 |
| 下游识别（第 4 步） | `getCallees` 递归 + ORM/出站 HTTP client/gRPC stub/Dubbo reference/MQ SDK 调用点 | `downstream_deps[]`（下游调用链） |
| DB 映射（第 2 步） | **仅到 ORM/mapper 调用点**，无表列级血缘 | `db_mapping` 的**候选**，须 DDL 侧确认 |

- **路由必须先过插件**：实测 `getRoutingManifest` 对 Flask `url_prefix` 与 Django `include()` 的前缀不拼接、对 `@app.get/@app.post` 完全不识别、对 Django 不产出 HTTP method。直接采信会生成缺前缀的 URL，请求 404 且**失败归因到无关模块**。因此路由以框架插件为主、图谱为辅，交叉校验后才定等级
- **一源两用**：避免重复建图；但注意一仓一写者——S2/S3 并发时索引写入需串行化
- **语言不支持 → 直接跳过**：下游识别直接跳过并标 `unsupported`，报告显式列出"未识别下游的接口"——宁可漏，不可错
- **DB 血缘的边界（更正）**：CodeGraph **不提供** `READS_TABLE`/`WRITES_TABLE`/`MAPS_TO_TABLE` 这类表列级血缘（那是独立项目 `raghurammrsd/CODE_GRAPH_MCP` 的能力，不可混用）。图谱最多追到 ORM / mapper 调用点；列级 `DBLineage` 仍靠 DDL + SQL 解析 + 设计文档，原生 SQL 字符串尤其超出图谱能力。画像须显式区分"ORM 可证血缘"与"原生 SQL 待补"
- **gRPC/Dubbo 契约解析不归 CodeGraph**：`.proto`/descriptor、`@DubboService`/`@DubboReference` 的全限定名与 XML 由 AstSight 自有解析器完成；CodeGraph 只提供符号/调用边。不得把图谱的“同名接口”当作 Dubbo 接口证据，接口类必须能在仓库源码中定位

### A4 用例导入与版本化编排（能力服务）

**职责**：读取外部草案（业务级/代码级），完成规则引用校验、覆盖缺口检查、与已批准用例库
diff、生成待审 proposal；**不生成用例，不调用 LLM**。

```
输入：ruleset.json + context.json + cases.draft.json + caseset.json
      + profiles.json + mock_semantics.json + data_catalog.json（有 DDL 时）
      （代码级额外：changed_apis.json）
      │
      ▼
┌─────────────────────────────────┐
│ 1. 结构与边界校验（确定性）        │  schema/大小/用例数/断言数/body 深度/路径安全
│ 2. transport 校验（确定性）        │  transport/endpoint 与画像一致；请求/断言字段属于该形态
│ 3. 覆盖角色与范围校验（确定性）    │  coverage_role 与画像一致；alias 只做冒烟；transport_scope
│ 4. 规则与需求校验（确定性）        │  rule_key 可解析/C-D 限制/quote 子串/section 存在
│ 5. 缺口校验（代码级，确定性）      │  change_refs 有效；无规则依据 → change_without_oracle
│ 6. fingerprint 与 diff（确定性）   │  new/update/revive/deprecate/unchanged/out-of-scope
│ 7. proposal 落盘（确定性）         │  proposal.json + review.md/json + validation.json
└─────────────────────────────────┘
      │
      ▼
输出：.astsight/cases/proposals/<proposal_id>/（待人工审核；不改变 caseset.json）
```

**关键决策**：
- **框架不生成草案**：业务级草案由规则驱动（`prompts/a4_case_design.md` / Skill）产出；
  代码级草案由变更缺口驱动（Skill/人工）产出；框架只做校验、diff、审核编排与版本化
- **规则是输入不是装饰**：`rule_refs` 必须可解析；business 用例挂 `requirement_refs`，
  code 用例挂 `change_refs`；无规则依据的变更写 `change_without_oracle`，不生成用例
- **business 优先**：同一 `(endpoint, case_key)` 只允许一条；code 用例与 business 期望
  冲突时以 business 为准
- **transport 是导入校验的一部分**：用例不得自行改写 `transport`；gRPC/Dubbo 用例必须使用
  各自原生请求/断言字段，跨形态字段直接拒绝；`mock="unsupported"` 的下游不得出现在
  `mock_bindings` 中
- **业务场景归 primary，alias 只做契约冒烟**：同一 `business_key` 的全量业务场景只落
  primary；alias 生成成功 + 一个错误/异常分支的冒烟，不计入业务规则/需求覆盖
- **`--transport-scope` 是可审计的显式降级**：`all` 默认（primary 全量 + alias 冒烟 +
  independent 全量）；`primary` 跳过 alias 并写 `transport_alias_skipped` + `degraded`；
  `full` 全端点全量。跳过不得静默
- 校验失败写 `validation.json`，命令退出非 0，**store 字节不变**
- `cases import` 只生成 proposal；`cases approve` 才写库，且必须显式 `--reviewer`
- approve 重新推导/重算 fingerprint，review 文件只取 decision/reviewer/note
- 只有 approved 用例进入 C6；C6 编码由确定性模板完成，复杂编排可另接 LLM 但需双重自检

### A5 结果诊断 Agent（失败 → 归因 → 重试决策）

**职责**：对失败用例自动归因，决定是否重试（`report.json` 的归因部分）。

```
输入：失败用例 + 执行上下文（响应/日志/环境）
      │
      ▼
┌─────────────────────────────────┐
│ 1. 信号提取节点（确定性）          │ 状态码/异常/断言/耗时/Mock 命中/fallback
│ 2. 归因分类节点（确定性规则为主）  │  六类根因：code_defect/case_error/data_error/mock_error/change_induced/adapter_error
│ 3. 疑难归因节点（LLM，可选）      │  规则判不了的复杂失败 → LLM 辅助（兜底）
│ 4. 动作归属节点（确定性）         │  user（反馈用户）vs framework（回流框架升级）
│ 5. 重试决策节点（确定性）         │  仅非 SUT 问题重试，≤3 次，留痕
└─────────────────────────────────┘
      │
      ▼
输出：归因结果（root_cause + action_target + confidence）+ 重试计划（喂给执行器）
```

**关键决策**：
- **归因主体是确定性规则**（错误码集合、超时关键词、DB 锁等待特征），LLM 只兜底疑难项
- **六类根因 + 动作归属路由**：`code_defect`/`case_error`/`change_induced` → `user`（反馈用户改进）；`data_error`/`mock_error`/`adapter_error` → `framework`（回流框架升级）。判定要点：用户资产写错→user，框架能力不足→framework
- **transport 错误先归一**：gRPC `status.code`、Dubbo sidecar 的 `rpc_status/exception_type`、HTTP status/超时统一进错误信封，再按根因归类；不得把 gRPC `UNAVAILABLE` 直接当 5xx 判成 `code_defect`
- **适配器错误独立成类**：descriptor 加载失败、sidecar 启动/版本/stdio 协议错误归 `adapter_error`（framework），不计入 SUT 缺陷；gRPC→sidecar 回退失败也先归此类，除非响应证明是 SUT 业务缺陷
- **归因不确定 → 不重试**，标 `unknown` 交人工——错误重试会掩盖真实缺陷
- **下游失败先看 Mock 证据，再判 SUT**：`mock_hits=0` 且存在真实出站 → `mock_error`
  （注入失效，按资损风险处理），绝不归 `code_defect`；`mock_hits>0` 但 `fallback_triggered=true`
  → `mock_error`（分支或降级配置错）；Mock 已按用例绑定分支返回、SUT 仍失败，才进入
  `code_defect`/`case_error`。证据字段：`dep_id`、`branch`、`strategy`、`hits`、`fallback`
- SUT bug **永不重试**，这是铁律
- **主提示词**：`prompts/a5_result_diagnosis.md`——只驱动第 3 步疑难归因节点，输出契约含 `root_cause`（六类）/`action_target`（user|framework）/`confidence`（诚实原则：证据不足必 low）/`evidence`（可溯源）/`verify_action`

### A6 用例自修复（可选扩展，非默认链路）

**职责**：A5 判定 `case_error` 后，生成**候选用例补丁**，再走一次完整的 proposal/approve。

- **只改可执行输入**：允许修改 `path_params/query/headers/body/asserts`；不得修改
  `case_key/level/transport/endpoint/rule_refs/change_refs/mock_bindings`，也不得改前置状态来“绕过”失败
- **数据问题不改用例**：`data_error` / `data_unavailable` 只建议重新生成 C5b 计划或修复数据库，
  不触发 A6，避免把造数缺陷伪装成用例修复
- **仍是人工资产**：A6 输出 `cases.draft.json` 级别的补丁，必须重新校验、diff、人工批准后才进入
  `caseset.json`；A6 不自动批准、不直接修改已批准用例
- **可关闭**：默认 `max_repair_rounds=0`；开启后每轮都留痕（原断言/补丁/证据/审批人），
  修复后失败仍可回溯到原始用例版本

---

## 四、Orchestrator（编排器）架构

**职责**：用 LangGraph StateGraph 串起 A1 需求分析、S2/S3、A4/A4.5、C5、C6/C7 与 A5 诊断，管理全局状态与流转。

```
State（全局共享状态）：
  changed_apis / profiles / context / ruleset / mock_semantics / data_catalog /
  proposals / caseset / data_plan / report / 门禁结论

流转图（主流程，含入口分流 + 人工断点 + 执行子图）：
  START
   │
   ├─[文档/契约变更] A1 需求分析(LLM) → 规则导入 ─┐
   │                                            │
   └─[代码变更] S2 变更感知 → S3 画像构建 ───────┤
                                                ▼
                                  C3.5.1 Mock 语义（契约 success + 规则分支）
                                                │
                                  C5a 数据目录（LLM 语义辅助，有 DDL 时）
                                                │
                                  A4 业务级/代码级用例导入 ──┐
                                                             ▼
                                                A4.5 审核检查（人工断点）
                                                待审未决/无 approved → 阻断
                                                             │ approved
                                                             ▼
                                                C5b 造数计划（按用例前置状态）
                                                             │
                                                             ▼
                                               [工具] C6 编码 → C7.1 冲突检测
   │                                                              ▼
   │                                            [工具] C7.2 并行调度
   │                                                              ▼
   │                          ╔═══════════════════════════════╗
   │                          ║  [执行编排子图 C7]（详见 5.1）   ║
   │                          ║  ┌─────────────────────────┐  ║
   │                          ║  │ 执行（单用例执行器 × N）  │◄─┐║
   │                          ║  │ 六阶段：造数→Mock→请求    │  │║
   │                          ║  │  →断言→熔断→数据清理      │  │║
   │                          ║  └───────────┬─────────────┘  │║
   │                          ║              ▼                │║
   │                          ║        ┌─────┴──────┐          │║
   │                          ║        │ 有失败？     │          │║
   │                          ║        └─────┬──────┘          │║
   │                          ║      ┌───────┴────────┐        │║
   │                          ║      ▼                ▼        │║
   │                          ║  A5 结果诊断      全部通过       │║
   │                          ║  （Agent,兜底）      │          │║
   │                          ║      │              │          │║
   │                          ║   ┌──┴───┐          │          │║
   │                          ║   │可重试?│          │          │║
   │                          ║   └──┬───┘          │          │║
   │                          ║   是(≤3)│否           │          │║
   │                          ║    └────┼────────────┘          │║
   │                          ║         ▼                       │║
   │                          ║  （回环只回"执行"节点）           │║
   │                          ║     └──────────────────────────┘║
   │                          ╚═══════════════┬═════════════════╝
   │                                          ▼
   └────────────────────────── [工具] C7.4 报告合并 → 门禁判定 → END
```

**关键设计**：
- **入口分流**：框架有两个变更源——**需求/设计文档或契约变更**（触发 A1 需求分析 → 规则导入 → A4 业务级用例导入）和**代码分支变更**（触发 S2 → S3 → A4 代码级用例导入）。二者可独立发生，也可同时发生。纯代码变更（如改了个 bug）不触发 A1，直接走 S2 这条线，代码级用例只补 business 未覆盖的缺口。
- **A4.5 人工断点**：A4 `cases import` 只生成 proposal。Orchestrator 检查**是否存在未决的 actionable 项**（pending）、**是否至少有 1 条 approved 用例**、`rule_refs`/`change_refs` 是否完整可解析、是否有 `disputed`/C-D-only 规则或 `stale` 用例，以及 `base_store_version`/`base_store_hash` 是否匹配。任一不满足即**阻断**，不得进入 C6。批准后的 approved 用例汇入 State，供 C6/C7 消费；规则本身不进入执行 State。
- **执行子图（C7）**：冲突检测 + 并行调度 + 执行 + 归因 + 重试，整体封装为一个 LangGraph 子图（详见 5.1），主流程只看到"执行子图"这一层。子图内部有自己的状态机（scheduled→executing→diagnosing→retrying→reporting），重试回环**只回子图内的"执行"节点**，不冒出到主流程。
- **条件分支**：执行子图内 A5 诊断出"可重试" → 回环到子图内执行节点；诊断出"终态" → 子图返回主流程，进入报告
- **失败即中止**：事实源环节（S2/S3 与 C5 的 DDL/FK/校验核心）失败 → 直接终止，不回退硬跑；C5 的 LLM 语义候选失败只降级为 `unmapped`/`inferred`，不污染已校验结构
- **其余人工断点**：A4.5 用例审核门（用例进入 C6 前）、低置信度确认（S2）作为**人工交互节点**插入图，需人工放行才继续

**DeepAgents 的角色**：
- DeepAgents 提供 Agent 的**声明式定义**（prompt + 工具绑定 + 子 agent 描述），以及**人机协作审核门**（Agent Inbox 模式）
- LangGraph 提供**确定性编排骨架**（状态图、条件分支、回环）
- 两者分工：**DeepAgents 管 Agent 本体，LangGraph 管 Agent 之间的流转**——各用所长，而非二选一

---

## 五、共享工具层（Tools，非 Agent）

| 工具 | 对应能力 | 是否 LLM | 说明 |
|---|---|---|---|
| CodeGraph 客户端 | 基础设施 | 否 | 反向/正向追溯、变更 diff |
| 接口解析器 | C3 | 否 | OpenAPI/YAML/$ref + Spring MVC 注解解析 |
| DB 分析器 | C5a | 否 | DDL 解析、外键闭包、拓扑排序、catalog 骨架 |
| 造数器 | C5b | 否 | catalog 校验、fixtures → 参数化 SQL、SQLite apply/teardown |
| C5 语义适配器 | C5a/C5b | 是 | 字段业务语义、候选业务态/取值；只能引用 catalog 中的表/列/枚举，不输出 SQL |
| Mock 编排器 | C3.5 | 否 | 语义模板、Mock 会话（session）与出站隔离 |
| Mock Agent | C3.5.3 | 否 | ByteBuddy agent 加载、Dubbo Filter、心跳自检；只拦截 SUT 出站调用 |
| 执行器 | C7 | 否 | 执行编排子图（状态机+单用例管道+Mock 会话租约），详见 5.1 |
| 报告器 | C7.4 | 否 | 归因合并、门禁判定 |

**原则**：工具是**无状态、可单测、可复用**的确定性函数；Agent 是**有状态、带 prompt、会思考**的智能体。两者边界清晰，互不越界。

> 例外：C7 执行器虽然归入工具层，但它**内部有状态机 + 回环 + Mock 会话生命周期**，不是无状态的纯函数。它是一组工具被**一个执行编排子图**串起来的结果。下节专门展开。

---

### 5.1 执行编排（C7）展开

> 定位：`framework-capability-design.md` 的 C7 章节讲的是**能力设计**（做什么——冲突检测/并行调度/归因重试/报告合并）。本节讲的是**编排细节**（怎么串——执行周期状态机、单用例执行器机制、Mock 会话与出站隔离的运行时时序）。两者是"能力"与"编排"的分层，不重复。

执行编排由三块组成，构成一个完整闭环：

1. **执行周期状态机**——管"整个执行周期怎么流转"（调度→执行→归因→回环→报告）
2. **单用例执行器机制**——管"一个用例怎么真正跑起来"（造数→Mock 会话→HTTP→断言→会话证据→数据清理）
3. **Mock 会话与出站隔离**——管"跑的时候下游命中哪个分支、透传靠什么兜底"

#### 5.1.1 执行周期状态机（LangGraph 子图）

执行器整体是一个 LangGraph 子图，内部状态机如下：

```
                    ┌─────────────────────────────┐
                    │  [工具] C7.1 冲突检测          │
                    │  输入：cases + data_plan      │
                    │  输出：conflicts.json(分组)    │
                    └──────────────┬──────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │  [工具] C7.2 并行调度          │
                    │  输入：conflicts 分组          │
                    │  输出：执行计划(组间并行/组内串行)│
                    └──────────────┬──────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │  [工具] 执行（单用例执行器 × N） │ ◄─┐
                    │  每个用例走 5.1.2 的执行器机制   │   │
                    └──────────────┬──────────────┘   │
                                   ▼                   │
                          ┌────────┴────────┐          │
                          │  有失败？         │          │
                          └────────┬────────┘          │
                     ┌─────────────┴─────────────┐     │
                     │ 是                        │ 否  │
                     ▼                          ▼     │
              ┌──────────────┐           ┌────────────┐│
              │ A5 结果诊断    │           │ [工具] C7.4 ││
              │ (Agent, 兜底) │           │ 报告合并     ││
              └──────┬───────┘           └─────┬──────┘│
                     │                          ▼      │
              ┌──────┴───────┐              ┌─────────┐ │
              │ 归因=可重试？  │              │ 门禁判定  │ │
              └──────┬───────┘              └────┬────┘ │
            ┌────────┴───────┐                   ▼     │
            │ 是(≤3次)        │ 否              END     │
            ▼                ▼                          │
      ┌────────────┐  ┌──────────────┐                 │
      │ 回环到执行   │  │ 终态 → C7.4   │                 │
      └─────┬──────┘  └──────────────┘                 │
            └──────────────────────────────────────────┘
```

**状态机关键点**：

| 要素 | 设计 |
|---|---|
| 状态 | `scheduled → executing → diagnosing → (retrying) → reporting → gated → done` |
| 回环条件 | A5 归因输出 `action_target` 为「测试侧可重试」（数据/用例/环境抖动/竞争残留），且 `attempt < 3` |
| 终态条件 | ① 全部通过；② 归因为 SUT bug（永不重试）；③ 重试 3 次仍失败；④ 归因 `unknown`（不重试，交人工） |
| 回环边界 | **只回环到"执行"节点，不回环到"冲突检测/调度"**——重试不重新分组，保证可重放（对齐 C7.2 的确定性分组） |
| 状态迁移 | 每个状态迁移都写 State，供报告器追溯（谁触发了回环、为什么、第几次） |

> 关键约束：**重试回环是"局部回环"**——只回执行节点，不回冲突检测/调度。因为冲突分组是确定性的，重跑时分组必须不变（可重放），否则失败重跑会对不上原归因。

#### 5.1.2 单用例执行器机制（一个用例怎么跑起来）

每个用例（pytest 函数或独立执行单元）在并发 worker 里走一条固定的执行管道，六个阶段：

```
┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐
│ 1. 造数前置 │→│ 2. Mock会话 │→│ 3. 发请求   │→│ 4. 断言校验 │→│ 5. 会话证据 │→│ 6. 数据清理 │
│  (setup)  │  │ (租约+会话) │  │  (HTTP/SQL)│  │ (asserts) │  │ (命中/晚到)│  │ (teardown) │
└───────────┘  └───────────┘  └───────────┘  └───────────┘  └───────────┘  └───────────┘
```

| 阶段 | 做什么 | 失败时的处理 |
|---|---|---|
| **1. 造数前置** | 用例的 `data_state` fixture 读取 `data_plan.json`，按拓扑序在事务内 apply 本用例 fixtures（外键前置表先造）| 缺 plan/数据库 → `blocked`/`data_unavailable`；SQL 执行失败 → 事务回滚并标 `data_error`，**不进入请求** |
| **2. Mock 会话** | 先取 `mock:<namespace>` lease，再 `POST /_admin/sessions` 登记本用例的 `mock_bindings`（见 5.1.3）；`namespace = sut_id`；绑定 Dubbo 下游时还需确认 agent 心跳已就绪且 `sut_id/namespace/admin_endpoint/pid` 与本 run 一致（能力设计 5.5） | 拿不到 lease → 排队；`mock_session_conflict`/`mock_session_missing` → 阻断请求，归 `mock_error`；`agent_not_active` / `mock_identity_mismatch` → 整批阻断 |
| **3. 发请求** | 执行 HTTP 请求（或被测入口调用），记录完整 req/resp | 连接失败 → 归因环境抖动；响应异常 → 进入断言/归因 |
| **4. 断言校验** | 逐条执行 `asserts`（status_code/body_field/…）| 断言失败 → 区分「响应符合契约但值不符=SUT bug」vs「断言本身错=case_error」 |
| **5. 会话证据** | 读会话命中记录（每个 binding 的调用次数、未授权 dep、晚到调用），再 drain + `DELETE` + 释放 lease | 无命中/晚到/过期 → 显式子码入报告；无会话证据的 Mock 用例标 `weak_oracle` |
| **6. 数据清理** | 在 `finally` 中按计划 `teardown`（逆拓扑、按主键参数化 `DELETE`）回收本用例造的数据 | teardown 失败显式 `data_teardown_failed`，不回退成通过 |

> 阶段 2/5 必须成对出现在 `try/finally` 里：**无论断言成功、失败还是异常，会话都要 drain 后关闭并释放 lease**，否则下一个 mock-bound 用例会卡在 `mock_session_conflict`。

> 设计要点：**造数失败不进请求**——这是"数据问题"与"接口问题"的物理隔离。造数挂了直接归因 data_error，不污染接口执行结果，避免"造数失败被误判成接口失败"。

**数据清理规则（第 6 阶段）**：

| 规则 | 说明 |
|---|---|
| **必清，无论成败** | 用例通过/失败/熔断都要执行清理——失败更必须清，否则残留脏数据污染后续用例或重试 |
| **按计划主键回收** | 依据 C5b 的 `teardown` 语句（逆拓扑、WHERE 主键 = ?）精确删除本用例 fixtures；只清本用例造的行，不按表全量删除 |
| **重试前先清** | 失败重试时，**重试的第一动作是清理上次残留**，再重新造数——否则"数据已存在"会导致重试永远失败（唯一键冲突） |
| **keep-data 例外** | `--keep-data` 调试模式下跳过清理，保留现场供人工排查（默认关闭） |
| **清理失败告警** | 清理本身失败（如外键级联删除被拒）→ 记录告警，标记 `cleanup_failed`，报告提示可能存在残留，但不阻断门禁（残留不影响本次结论，但需人工关注） |

> 关键：清理不是"可选收尾"，而是**执行管道的固定第 6 阶段**。它保证用例之间的数据隔离——即便一个用例因为 SUT bug 而失败，它造的数据也必须在归因前清掉，否则下一个用例跑在同一张表上会拿到被污染的初始状态，产生连锁假失败。

#### 5.1.3 Mock 编排与出站隔离（执行期时序）

全 Mock 策略下，Mock 不是"测试前配好就完了"，而是**执行期的一条编排链**。方案 A 的关键前提：**HTTP URL 覆盖与 Dubbo agent 都是 SUT 启动期配置，不能按用例改**——SUT 启动时统一指向 Mock 服务 / 加载 agent，执行期用"会话"决定当前用例命中哪个语义分支。HTTP 与 Dubbo 下游共用同一会话。

```
用例启动
   │
   ▼
获取 mock:<namespace> lease（C7.1 独占资源；mock-bound 用例串行）
   │  拿不到 → 等待，不抢占已有会话
   ▼
POST /_admin/sessions
   │  {session_id, namespace, case_id, bindings:[{dep_id, branch, binding_source}], ttl_ms}
   │  bindings 来自用例 mock_bindings（导入期已补全，不存在运行时隐式默认）
   │  已有 active session → mock_session_conflict，阻断
   ▼
执行请求（被测接口内部会调用下游）
   │
   ├─ 出站请求按 dep_id 命中 session binding → 返回该 branch 的编排响应 ✔
   ├─ 无 active session              → mock_session_missing ✘
   ├─ dep 不在 bindings 中           → mock_unexpected_dep ✘
   └─ branch 不在语义模板中          → mock_branch_not_found ✘
   ▼
读取会话证据（每个 binding 的命中次数、未命中、未授权 dep、晚到调用）
   ▼
drain（默认 200ms）→ DELETE /_admin/sessions/{id} → 释放 lease
   ├─ 关闭后到达 → mock_late_call（告警，报告 degraded）
   └─ 超过 ttl_ms → mock_session_expired（告警）
```

**Dubbo 下游（agent + ByteBuddy + Filter）**：

```
SUT 启动（AstSight 拉起）
   │  -javaagent:astsight-agent.jar（或 classpath + dubbo.consumer.filter）
   ▼
agent：ByteBuddy Agent 加载 → filter jar 追加进 classloader
   │  Filter 经 Dubbo SPI @Activate(group=CONSUMER) 进入消费端调用链
   ▼
启动自检：读本地心跳文件（sut_id/namespace/admin_endpoint/pid/jvm_start_time 与本 run 一致、心跳新鲜、Filter 已实例化）+ 版本匹配
   │  缺失/残留/过期 → agent_not_active；身份不符 → mock_identity_mismatch，整批阻断
   ▼
用例执行：SUT 调下游 → Directory 选中 provider → Filter 命中会话
   │  ├─ 命中 binding → Jackson 按方法返回类型构造对象，返回 Result
   │  ├─ provider 不可用 → env_error（环境不满足，不伪造 provider）
   │  ├─ dep 未绑定 → mock_unexpected_dep
   │  └─ branch 不存在 → mock_branch_not_found
   ▼
会话证据：每个 binding 的命中次数（0 次 → mock_not_used）
```

- 不部署 Mock Provider、不做注册中心替身；注册中心/provider 由测试环境保证；
- Filter 只拦截 SUT 出站 Dubbo 调用，不参与调用 SUT；
- 不增强 Dubbo 内部类；Filter 未激活（如应用 `filter=...` 带 `-default`）由心跳文件的 `filter_instantiated=0` 发现；
- 响应对象构造失败 → `mock_response_invalid`；异步调用返回 `AsyncRpcResult`；
- `serialization_verified=false` 固定写入报告（跳过 Hessian2）。

**失败子码与处理**：

| 子码 | 含义 | 处理 |
|---|---|---|
| `env_error` | provider 不可用、注册中心不可达、发现超时 | 用例 `blocked`；不计 SUT 缺陷，不伪造 provider |
| `agent_not_active` | 心跳文件缺失/残留/过期，或 Filter 未实例化（未进 consumer 链） | **整批阻断**；绝不允许继续跑（否则会真实调用下游）|
| `mock_identity_mismatch` | 心跳或会话命名空间与本 run 的 `sut_id`/endpoint/pid 不一致 | **整批阻断**；说明连到了其他 SUT 的 agent/Mock（能力设计 5.5）|
| `mock_session_conflict` | 同命名空间已有 active session | 阻断请求；调度缺陷，归 `mock_error` |
| `mock_session_missing` | 请求到达 Mock 时没有 active session | 阻断请求；归 `mock_error`（通常是 fixture 生命周期漏掉）|
| `mock_unexpected_dep` | 出站到会话未授权的下游 | 阻断/告警；说明画像漏识别或用例多调了下游 |
| `mock_branch_not_found` | 引用了语义模板里不存在的分支 | 阻断请求；`semantic_incomplete`，回流 C3.5.1 |
| `mock_response_invalid` | 响应对象构造失败 | 阻断请求；归 `mock_error` |
| `mock_not_used` | 绑定 dep 在本用例命中 0 次 | 用例失败；防假绿，不按通过处理 |
| `mock_late_call` | 会话关闭后仍有调用到达 | 告警 + `degraded`；通常意味着 drain 窗口不足或 SUT 有异步/重试 |
| `mock_session_expired` | 超过 `ttl_ms` 自动过期 | 告警 + `degraded`；检查用例是否卡死 |

**出站隔离（原“熔断护栏”的正确口径）**：

| 手段 | 能保证什么 | 前提 |
|---|---|---|
| **网络出站隔离**（首选）| 真实下游**不可达**，请求根本无法打到生产/第三方 | 测试环境有 NetworkPolicy / 安全组 / hosts 黑洞，或 SUT 只配置 Mock 地址且无其他出站路由 |
| Mock 会话绑定 | 到达 Mock 的调用只能命中本用例授权分支 | Mock 服务可控且 SUT 确实指向它 |
| 会话证据缺失检测 | 画像里识别到的下游在应被调用的用例里从未命中 → 可能是绕过了 Mock | 需要画像准确，只能作弱信号 |

> **必须诚实标注**：应用层（会话记录）**无法观测"根本没到达 Mock 的请求"**。如果 SUT 配置错误、硬编码 URL 或走了代理，请求会直接出网，Mock 侧看不到任何异常。因此：
> - 有网络出站隔离时，报 `egress_guard=network`，这是"全 Mock 无透传"的**唯一强保证**；
> - 没有网络隔离时，报 `egress_guard=none` 并标 `degraded`，同时把 `mock.strategy` 与 SUT 配置来源写进报告，由人确认；
> - 不得把"会话内没报错"等同于"没有透传"——那是把弱证据当强结论。
>
> 唯一豁免：Cache/Redis 等随测随建的基础设施，不作为"下游服务"处理。SUT → gRPC 下游仍 `mock="unsupported"`，只能靠用例排除/`blocked`（v4.4 决策：gRPC 下游指向 gripmock 假服务器，target 改写显式可审计；未重定向的真实出网仍触发告警）；Dubbo 下游由 agent Filter 拦截，但同样受 `egress_guard` 约束（agent 失效时仍可能真实出网）。

#### 5.1.4 与 A5 结果诊断 Agent 的接口

执行编排子图在"归因"节点调用 A5，边界如下：

| 项 | 内容 |
|---|---|
| 触发 | 有失败用例，且确定性归因规则未命中（疑难项）|
| 输入 | 失败用例的 `case + execution + context`（对齐 `prompts/a5_result_diagnosis.md` 的输入契约）|
| 输出 | `root_cause + action_target + confidence + evidence + verify_action` |
| 回环决策 | `action_target` 为测试侧可重试 → 回环执行；`user`/终态 → 进报告 |
| 归因确定性路径 | 大多数失败由**确定性规则**（错误码集合、超时关键词、DB 锁特征）直接归因，**不经过 A5**；只有规则没覆盖的疑难项才兜底给 LLM |

> 关键：A5 的 LLM 只兜底**疑难归因**，不是每个失败都过 LLM。这是"LLM 用量最小化"在 C7 的落地——绝大多数失败（超时、5xx、断言值不符）都是确定性可判的。

---

## 六、Agent 记忆设计

> Agent 记忆按生命周期分三层，各司其职，避免"什么都往里塞"。AstSight的核心原则：**重资产、轻向量库**——事实源必须是可 diff、可人工编辑的结构化文件，不是 embedding。

### 6.1 三层记忆模型

| 记忆层 | 生命周期 | 存什么 | 载体 | 谁写 / 谁读 |
|---|---|---|---|---|
| **短期记忆** | 单次运行 | 当前流程中间产物与状态 | LangGraph State | 编排器 / 各节点 |
| **长期记忆（资产）** | 跨运行、跨项目 | 可复用的事实源知识 | 版本化资产文件 | **人工维护 / 只读引用** |
| **工作记忆** | 单 Agent 内部 | 该 Agent 多步推理的中间结论 | Agent scratchpad | Agent 自身 / Agent 自身 |

### 6.2 短期记忆 = LangGraph State

编排器在内存中流转的全局状态，运行结束即释放：

```
State：changed_apis / profiles / ruleset / data_plan / cases / report / 门禁结论
```

- **不落盘**（中间产物另有契约文件承载，见能力文档"能力间落盘通信"）
- 作用是让各节点共享上下文，而非持久化

### 6.3 长期记忆（资产）= 版本化事实源文件

**原则：只读引用 + 人工维护。** 资产是事实源，运行结果不得自动反写污染它，任何更新须过人工审核。

| 资产 | 内容 | 维护方式 | 消费方 |
|---|---|---|---|
| 术语表 | 业务术语/字段语义 | 人工维护，随项目演进 | C3 语义附着、C4 校验 |
| 规则库 `ruleset.json` | 业务规则 BR / 契约规则 CR、`rule_key`、questions/gaps/conflicts、prompt/model 版本 | 只能由 `rules import` 写入；规则被 approved 用例引用后才算确认 | C4 设计/选择、覆盖与漂移报告 |
| 用例库 `caseset.json` | 已审核可执行用例（`TC-NNNN`、`level`、`rule_refs`、版本、fingerprint、review_log、history） | 只能由 `cases approve` 写入；proposal 不自动入库 | C6 编码、C7 判定、报告 |
| Mock 语义模板 | 下游依赖的语义分支枚举 | 人工维护 | C3.5.1 语义绑定、C7 执行 |

> 用例库是**唯一执行与判定资产**：草案和 proposal 只是审核过程材料，不参与编码与判定；
> `review_log` 追加式记录审核决定，`history` 保留被替代的旧版本供 replay。已被拒绝的草案
> 不进入 store，修改后必须重新 import 生成新 proposal。

**为什么不用向量库**：这三类资产是**结构化、字段级、需精确匹配**的，存成可 diff、可编辑的 JSON/YAML 文件，配合确定性查表即可。向量库适合"语义相似"的模糊召回，但事实源环节最怕模糊——一个字段约束记错就是假绿。

### 6.4 工作记忆 = Agent scratchpad（仅 A5）

框架内有两个 Agent：A1（需求分析）与 A5（失败诊断）。A1 的分析是一次结构化输出，不需要
跨步骤 scratchpad；scratchpad 只服务 A5 的多步归因与重试决策：

- **A5 结果诊断**：记录"已尝试哪些归因、重试次数"，避免重试决策反复

A1 的 LLM 节点只是一次结构化输出（规则分析），不存在跨步骤推理记忆；A4 用例导入、S2/S3/C5
都是确定性能力服务，均无 scratchpad。草案生成发生在框架外，其多步记忆由外部 Skill/模型
自行管理，不进入本编排状态。

### 6.5 历史缺陷案例库（框架自我改进资产）

**定位**：保存「框架自身不足导致的用例失败」案例，用于**反向促进框架改进**——它不是给 A5 做归因检索用的，是给框架迭代做依据的。

> 关键区分：普通的"缺陷案例库"记的是"被测系统有什么 bug"；AstSight的"历史缺陷案例库"记的是"**框架自身有什么不足**"。二者是两码事。

**入库判据（只记框架缺陷，不记 SUT 缺陷）**：

| 归因类型 | 是否入库 | 说明 |
|---|---|---|
| SUT 缺陷（真实被测 bug） | ❌ 不入库 | 那是被测系统的问题，交给缺陷追踪 |
| **框架不足**（生成/造数/画像/归因出错） | ✅ 入库 | 如"漏识别下游""约束推错""Mock 语义失真" |
| 环境抖动 / 竞争残留 | ❌ 不入库 | 偶发，非框架缺陷 |

**记录结构**（纯本地文件，`failures/framework_issues.jsonl`，追加式）：

> 一条案例 = 一份"完整失败现场快照 + 根因 + 改进"，信息密度要支撑框架迭代时无需翻原始日志即可定位问题。

```json
{
  "id": "FW-001",
  "category": "case_gen | data_build | profile | mock_semantic | attribution",
  "framework_version": "v2.15",

  "interface": {
    "method": "POST",
    "path": "/api/orders",
    "endpoint_key": "POST /api/orders"
  },

  "params": {
    "request": {"qty": 2, "sku_id": "S001"},
    "response_expect": {"status": 200, "fields": ["order_no"]},
    "response_actual": {"status": 400, "body": {"error": "库存不足"}}
  },

  "downstream_calls": [
    {"id": "pay-gateway", "type": "sync_rest", "mock_semantic": "success"}
  ],

  "biz_context": {
    "rule_refs": [{"rule_id": "BR-01", "rule_key": "RK-1a2b3c4d5e6f", "text": "库存不足返回 40001"}],
    "db_tables": ["orders", "products"],
    "constraints": {"qty": "min 1 / max 999"}
  },

  "case": {
    "case_id": "TC-B-012",
    "code": "def test_create_order(): ...",
    "source": "generated | manual"
  },

  "failure": {
    "reason": "case_gen 生成了不存在的业务约束",
    "root_cause": "需求上下文缺少该约束，草案生成未标记 inferred",
    "llm_initial_judgement": "疑似草案生成幻觉：qty 约束与 DB/需求原文不符"
  },

  "improvement": {
    "action": "C4 模板兜底时强制标注 inferred，缺失约束须显式告警",
    "category_hint": "case_gen"
  },

  "review": {
    "reviewer": "人工",
    "status": "confirmed | rejected",
    "confirmed_at": "2026-10-20"
  },
  "created_at": "2026-10-20"
}
```

**字段分组说明**：

| 分组 | 字段 | 回答的问题 |
|---|---|---|
| 定位 | id / category / framework_version | 是什么问题、哪个环节、哪个版本 |
| 接口快照 | interface / params | 测的是哪个接口、出入参实际长什么样 |
| 依赖快照 | downstream_calls | 涉及哪些下游、Mock 了什么语义 |
| 业务上下文 | biz_context | 当时的用例库版本、需求上下文与约束全貌 |
| 用例资产 | case | 失败的用例代码 + 来源 |
| 失败分析 | failure | 失败原因 + 根因 + **LLM 初判** |
| 改进 | improvement | 怎么改框架 |
| 审核 | review | 人工确认结果（入库前必填） |

### 6.5.1 入库流程（LLM 初判 → 报告 → 人工确认 → 入库）

> 关键：**入库必须过人工确认**。LLM 只做初判，不做终审——避免把 LLM 误判的案例污染进库（一旦污染，框架迭代就会基于错误证据走偏）。

```
用例运行失败（重试耗尽）
        │
        ▼
┌─────────────────────────┐
│ 1. A5 结果诊断           │  归因：SUT 缺陷 / 框架不足 / 环境 / 数据
└───────────┬─────────────┘
            │ 归因 = 框架不足（case_gen/data_build/profile/mock_semantic/attribution）
            ▼
┌─────────────────────────┐
│ 2. LLM 初判（A5 或独立）  │  生成「候选案例」：
│                          │  - 自动收集现场快照（接口/出入参/下游/业务/代码）
│                          │  - 初步判断 root_cause 与 improvement 建议
└───────────┬─────────────┘
            │ 候选案例随报告输出
            ▼
┌─────────────────────────┐
│ 3. 报告呈现给人工         │  候选案例列在报告「框架改进候选」区
│                          │  标注 llm_initial_judgement（明示是 AI 判断）
└───────────┬─────────────┘
            │ 人工确认 / 修正 / 驳回
            ▼
┌─────────────────────────┐
│ 4. 人工确认后入库         │  review.status = confirmed 才写入 jsonl
│                          │  rejected → 丢弃或修正后重提
└─────────────────────────┘
```

**关键设计决策**：

- **LLM 只做初判、不做终审**——它负责"把现场快照收集齐 + 给出初步根因假设"，最终是否入库、root_cause 是否准确，由人工拍板
- **候选案例随报告输出**，不直接写库——报告里单列「框架改进候选」区，人工过目后才落盘
- **`llm_initial_judgement` 与 `review` 分离**——AI 判断永远明示为 AI 判断，人工确认结果单独记录，不混在一起
- **入库判据前置**：只有 A5 归因为「框架不足」才进入候选；SUT 缺陷、环境抖动不进入此流程

**异常与边界**：LLM 初判拿不准 root_cause → 仍生成候选案例，`root_cause` 标 `unknown`，交人工补全；人工长时间不确认 → 候选案例留在报告，不自动入库（宁缺毋滥）

**消费方式（人工驱动，非自动检索）**：

1. 框架迭代时，人工翻阅案例库，提炼共性根因 → 反哺设计/实现
2. 案例库是**促进框架改进的证据来源**，不是运行时检索库
3. 未来若要"按语义相似召回历史案例给 A5 参考"，再评估引入向量库——当前不做

### 6.6 记忆存储与更新总原则

- **存储**：纯本地文件，无独立知识库/数据库
- **资产（长期记忆）**：只读引用 + 人工维护，运行结果不自动反写
- **案例库**：A5 归因为框架不足 → LLM 初判生成候选 → 随报告呈现 → 人工确认后追加；不入库 SUT 缺陷、环境抖动
- **短期/工作记忆**：随运行生命周期释放，不持久化

---

## 七、关键权衡与风险

| 权衡点 | 决策 | 理由 |
|---|---|---|
| 节点数量 | 2 Agent（A1/A5）+ 确定性节点/能力服务 + 1 编排 | 确定性能力下沉为节点/服务，Agent 只保留会思考的部分 |
| LLM 用量 | 框架内 A1 规则分析（重）、A5 诊断（弱）、C5a/C5b 语义辅助（轻）；用例草案生成在适配层（规则驱动） | LLM 用在"理解/生成/诊断"，不用在"校验/追溯/判定" |
| 编排粒度 | LangGraph 确定性骨架 + DeepAgents Agent 本体 | 编排要可预测，Agent 要可思考 |
| 失败策略 | 事实源失败即中止，生成失败可降级 | 上游错了下游全垃圾 |
| 人工介入 | 审核门 + 低置信度确认作为图中节点 | 幻觉防不住，只能人兜底 |

**最大风险**：用例草案生成发生在框架外，模型可能写出“看似合理实则错误”的请求或断言。对策：① schema/路径/字段/quote 严格校验；② 与已批准用例库做 fingerprint diff；③ 人工审核门硬卡，未批准不进 C6；④ codegen 双重自检。**四道防线，缺一不可。**

---

## 八、与能力设计的映射对照

| 能力 | 形态 | LLM 程度 |
|---|---|---|
| C1 需求分析 | A1 需求分析 Agent（LLM） | 规则分析（`a1_requirement_analysis.md`） |
| C2 变更感知 | S2 变更感知能力服务 | 无 |
| C3 接口画像 | S3 画像构建能力服务 | 无 |
| C3.5 下游依赖控制 | S3 + Mock 编排器工具 + C3.5.1 语义构建 | 契约分支无；业务分支规则/适配层补充 |
| C5 数据准备 | C5a 目录 + C5b 造数器工具 + 语义适配器 | 语义辅助（LLM 候选，确定性校验/SQL） |
| C4 两级用例设计 | A4a/A4b 导入节点 + A4.5 人工断点 | 无（草案由框架外/规则驱动生成） |
| C6 用例编码 | 编码工具 | 无（模板为主；复杂编排可选 LLM） |
| C7 执行验证 | A5 诊断 Agent + 执行器/报告器工具 | 弱 |

> **能力服务的命名规范**：凡无 LLM 的确定性能力，统一以 `S` 前缀命名（Service），以 `A` 前缀标识真 Agent（含 LLM 自主决策）。当前框架内 A1（需求分析）与 A5（失败诊断）是真 Agent；A4 保留 A 前缀是历史编号，实际形态是能力服务；A4.5 是人工断点。

> 本设计与 `framework-capability-design.md` **v4.6.1** 完全对齐：能力定义以能力文档为准，本文只是把能力映射到 Agent/能力服务的技术形态上。任何边界调整，需回写能力文档并升版本。
