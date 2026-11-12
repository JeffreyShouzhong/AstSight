# AstSight — 能力设计规格说明书（v4.7，含 v4.4 gRPC Mock、v4.6 Feign 决策、v4.6.1 HTTP Mock 范围、v4.7 规则驱动 A4 与 C5a/C5b）

> **名称**：AstSight（AST + Sight）｜ **CLI**：`astsight` ｜ **Maven**：`io.astsight:astsight-core`
> **一句话主张**：上一代平台问「你测了多少条」，AstSight 只回答「这次变更带来什么质量结论」。
> **命名理由**：AST 是框架的地基——它靠代码图谱（tree-sitter 解析出的语法树）看清「改了什么」；Sight 是它的落点——再用契约与不变量说清「对不对」。**先看得见结构，才谈得上判定。**

| 项    | 内容                                                      |
| ---- | ------------------------------------------------------- |
| 定位   | 变更驱动 + 全量基线 + 历史回放的接口自动化测试引擎（运行模式三分，见 3.0）      |
| 形态   | CLI 直驱（无交互层、无意图层）                                       |
| 输入   | 需求文档、设计文档、代码库变更分支、数据库结构、接口契约（HTTP：OpenAPI / Spring MVC 注解；gRPC：proto/descriptor；Dubbo 2.7+：接口源码 + 标准注解/XML；每种 transport 至少一种）                |
| 输出   | 变更接口列表、接口画像、业务规则集、业务级/代码级用例集、造数计划、pytest 用例代码、执行报告（含冲突检测/并行/重试/合并） |
| 运行环境 | **服务器 / CI 容器：Linux 为主**（Windows 仅开发机本地调试）；路径、子进程、信号、端口、文件锁按 POSIX 语义设计，不依赖 Windows 专有行为 |
| 文档性质 | **纯设计文档，不含实现**。所有 JSON 为数据契约，非代码                        |

> **当前口径**：本文只描述当前有效的能力规格、契约与约束。决策过程（候选对比、落选原因、被推翻的判断、版本变更历史）统一记录在 [`决策记录.md`](决策记录.md)，本文不复述“为什么”。
> gRPC 下游 Mock 已决策（gripmock 假服务器）但未实现，落地约束见 **C3.5.4**；v4.3 报告口径不变。
> 多 SUT 同环境并发的隔离规则见 **5.5**（v4.5）：实例身份、端口原子分配、持久化作用域缺一不可。
> **v4.6 决策**：取消对 Feign 的特殊支持。Feign 不单独解析、不作契约源、不生成专用配置；
> 只按普通 HTTP 客户端处理，由通用 HTTP 识别与注入点覆盖。通用机制覆盖不了时直接标
> `mock="unsupported"` / `unidentified`，依赖它的用例显式排除或 `blocked`，不做 Feign 专用兜底。
> **v4.6.1 范围收紧**：HTTP Mock 首版只支持**明文 HTTP**；HTTPS 下游不降级、不 MITM，
> 统一标 `mock="unsupported"`（`unsupported_reason="tls_not_supported"`），依赖用例 `blocked`。
> `dep_id` 改为携带下游身份并做规范化合并，落地约束见 **C3.5.5**。
> **v4.6.1 HTTP 注入承诺**：v1 只承诺下游专属 `url_override` 与显式 `http_proxy`（均为明文）；
> `discovery_override`/透明代理为条件支持，共享 base URL、同 JVM bean mock、动态 URL、
> 请求匹配/序列/流式不支持。完整矩阵见 C3.5.5。

---

## 一、系统上下文

```
        ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
        │  需求/设计文档 │   │  Git 代码库  │   │  数据库      │
        └──────┬──────┘   └──────┬──────┘   └──────┬──────┘
               │                 │                 │
               ▼                 ▼                 ▼
        ┌────────────────────────────────────────────────┐
        │               AstSight（本系统）               │
        │       感知层 → 分析层 → 生成层 → 执行层        │
        └───────────────┬────────────────────────────────┘
                        │
          ┌─────────────┼──────────────┐
          ▼             ▼              ▼
     用例资产库      CI 门禁        测试资产管理平台
    （pytest 代码） （通过率卡控）   （测试平台等，旁挂对接）
```

**边界声明**：

- AstSight 是**引擎**，不是平台——不做用户管理、用例管理 UI、任务调度，这些交给测试平台
- 与外部系统仅有三类交互：读（文档/代码/库结构）、写（用例代码与报告落盘）、执行（HTTP 请求被测服务 + SQL 造数）

---

## 二、总体架构

### 2.1 分层与能力映射

| 层   | 能力          | 职责一句话                          |
| --- | ----------- | ------------------------------ |
| 感知层 | C1 需求分析     | 需求+设计文档 + 契约源（HTTP：OpenAPI / Spring MVC 注解；gRPC：proto/descriptor；Dubbo：接口源码）→ 可追溯的业务规则集（BR/CR + questions/gaps） |
| 感知层 | C2 变更感知     | git 分支差异 → 变更接口列表（带置信度）        |
| 分析层 | C3 接口画像     | 单接口的完整档案：契约+约束+DB 映射+业务语义+下游依赖 |
| 分析层 | C3.5 下游依赖控制 | 对外调用的识别与 Mock（唯一主策略，含通用 HTTP URL/配置覆盖）+ 语义模型     |
| 分析层 | C5 数据准备     | C5a 造数能力目录（表/列/可用业务态）+ C5b 逐用例造数计划；LLM 辅助语义，校验与 SQL 确定性 |
| 生成层 | C4 两级用例设计与版本化 | 业务级（规则驱动）/ 代码级（变更补充）草案 → 校验 → diff → **人工审核** → 版本化 caseset.json（资产定义见 **3.2**） |
| 生成层 | C6 用例编码     | 已批准用例集 → 可入库的 pytest 代码资产         |
| 执行层 | C7 执行验证     | 冲突检测 → 并行调度 → 执行 → 归因重试 → 报告合并；**判定依据取自用例断言与接口契约（HTTP：OpenAPI / Spring MVC 注解；gRPC：proto/descriptor；Dubbo：接口源码）** |

### 2.2 主流程（数据视角）

```
需求/设计文档 + 契约源 ──C1──▶ Context + RuleSet（BR / CR / questions / gaps）
   （HTTP：OpenAPI/Spring MVC；gRPC：proto/descriptor；Dubbo：接口源码） │
变更分支 ──C2──▶ ChangedAPI[]（含 transport）──C3──▶ Profile[]（含 transport + 下游）
                                      │
                                      ├──C3.5.1──▶ MockSemantic（契约 success + 规则分支）
                                      │
DDL/库结构 + seed ──C5a──▶ DataCatalog（可用表/列/业务态） │
                                      │                  │
                    ┌─────────────────┴──────────────────┘
                    ▼
        C4a 业务级草案（全量、规则驱动）      C4b 代码级草案（仅变更缺口）
   RuleSet + Context + Profile             ChangedAPI + Profile + RuleSet
   + MockSemantic + DataCatalog            + MockSemantic + DataCatalog
                    └─────────────────┬─────────────────┘
                                      ▼
                     校验 + diff → proposal → 人工审核 → CaseSet
                                         （level = business | code）
                                      │
                                      ▼
        C5b 逐用例造数计划（LLM 业务态候选 + 确定性校验）
                                      │
                                      ▼
                            C6 ──▶ pytest 代码（tests/ 目录）
                                      │
                                      ▼
                            C7 ──▶ 执行报告 + 归因 + 门禁判定
```

**判定链**：C4 产出的 `CaseSet`（只含 approved 用例）同时流向 C6（编码）和 C7（判定）。
C7 的 oracle 是**审核通过的用例断言**与接口契约（HTTP 的 OpenAPI/Spring MVC 响应定义、gRPC 的 proto message、Dubbo 的返回类型）；规则（BR/CR）只作为断言
来源的追溯与覆盖依据，**不参与运行时判定**。用例里的 `requirement_refs`
（doc + section + quote）与 `rule_refs` 只用于追溯，不能单独证明断言正确；断言是否成立
仍由人工审核确认。

**设计约束**：各能力之间只通过**落盘的契约文件**通信（JSON），不共享内存对象——保证任一环节可独立重跑、可人工干预中间产物。

### 2.3 CodeGraph（代码图谱）的定位

> CodeGraph 不是某个能力，而是**贯穿感知层与分析层的基础设施**。它把代码解析为"符号 + 调用关系"图，提供两种追溯能力与一种 diff 能力，供多个能力模块复用。

**核心能力**：

| 能力          | 说明                 | 消费方                 |
| ----------- | ------------------ | ------------------- |
| **反向追溯**    | 谁调用了符号 A？（沿调用链向上）  | C2 变更感知             |
| **正向追溯**    | 符号 A 调用了谁？（沿调用链向下） | C3 下游识别、C3.5.1 语义反推 |
| **变更 diff** | 两个版本的符号级差异         | C2 变更感知             |

**在框架中的四种作用**：

| # | 作用         | 位置     | 追溯方向 | 具体用途                                                    |
| - | ---------- | ------ | ---- | ------------------------------------------------------- |
| 1 | 变更影响分析     | C2     | 反向   | 改 `calculate_total` → 追到 `POST /api/orders`，实现"改了什么测什么" |
| 2 | 下游依赖识别     | C3     | 正向   | 接口 → 服务 → 外部客户端，识别支付网关/MQ/Redis 等出站调用                   |
| 3 | Mock 语义完整性 | C3.5.1 | 正向   | 找出接口对下游响应的所有判断分支，反推必须 Mock 的语义集合                        |
| 4 | 调用参数溯源（辅助） | C3/C5  | 正向   | 入参经哪些转换写进哪张表（弱依赖，主靠 DDL+接口契约，图谱兜底）                   |

**CodeGraph 与接口依赖图谱的区别（两个不同的图，不可混淆）**：

| 维度 | CodeGraph（代码图谱） | 接口依赖图谱（业务级）                         |
| -- | --------------- | ----------------------------------- |
| 层次 | 实现级             | 业务级                                 |
| 节点 | 函数/类/模块         | 接口（`POST /api/orders`）              |
| 边  | 调用关系            | 数据依赖（A 产出 order_no → B 消费 order_no） |
| 回答 | 改了这个函数影响哪些接口    | 用例能不能串起来（登录→Token→下单）               |
| 来源 | 代码 AST 解析       | 契约源（HTTP：OpenAPI/Spring MVC 注解；gRPC：proto/descriptor；Dubbo：接口源码）+ 画像 produces/consumes |

**关键设计决策**：

- CodeGraph 通过 **代码嵌入接入**（常驻 Node worker 进程内直接 `import` 其库 API，见 `CodeGraph-集成设计.md` v2.0）。**不走 MCP**：MCP 的返回面向模型阅读（源码/路径/爆炸半径混排的 markdown 包），不能作为结构化事实源，C2/C3/C5 需要机器可解析、可复现、可审计的输出。CLI 退为诊断工具（子命令覆盖面远小于库 API）
- 图谱由嵌入式 worker 现场构建或增量更新，构建结果落盘可复用；一仓一实例、一仓一写者
- 语言不支持的文件/符号**直接跳过**，标记 `unsupported`，不做降级兜底（宁可漏、不可错）
- **路由不能直接采信图谱输出**：实测 1.6.2 对 Flask `url_prefix` 与 Django `include()` 的前缀不拼接、对 `@app.get/@app.post` 完全不识别、对 Django 不产出 HTTP method。路由以**框架插件为主、图谱为辅**，两者交叉校验后才定证据等级（`CodeGraph-集成设计.md` 1.3/3.3）

**部署形态与侵入性（关键）**：

| 问题                   | 结论                            |
| -------------------- | ----------------------------- |
| 被测应用需要安装 CodeGraph 吗 | ❌ **不需要，零侵入**                 |
| 需要被测应用在运行吗           | ❌ 不需要，纯离线静态分析                 |
| 需要改被测应用配置/注入 SDK 吗   | ❌ 不需要                         |
| 需要什么                 | ✅ 仅需被测应用的**源码路径**（目录或 git 仓库） |

**原理**：CodeGraph 是**离线静态代码分析**，而非运行时埋点。它读取被测应用源码文件，用 AST 解析器构建符号与调用关系图——只读源码，不向被测应用注入任何东西。这与"链路追踪 agent / 插桩"这类运行时监控有本质区别。

**分析边界**：

| 可分析                 | 不可分析                         |
| ------------------- | ---------------------------- |
| 被测应用自有源码（路由/服务/调用点） | 三方依赖的二进制内部逻辑（import 的包看不到源码） |

> 不可分析三方依赖**不影响AstSight**——我们的追溯目标是"路由 → 自有服务层 → 外部客户端调用点"，这些调用点都在被测应用源码内，不在三方包内部。

**语言构建差异**：

- Python：直接读源码即建图（无编译步骤）
- Java/Spring：AST 可直接解析 `.java` 源码；若项目含运行时注解/反射动态路由，精度下降，需实测（见风险表"Java/Spring 路由识别精度不足"）

**开源引擎参照（复用而非重造）**：

> AstSight的 CodeGraph 底层**直接采用 `colbymchenry/codegraph`，不自研**。这里所称 **CodeGraph 特指 `colbymchenry/codegraph`** 这一个项目（Rust 内核 + tree-sitter + SQLite，提供 Node 库 API / CLI / MCP 三种形态，AstSight采用**库 API 嵌入**），它已提供AstSight C2/C3 所需的绝大部分底层能力。

| 引擎 | 仓库 | 技术栈 | 与AstSight能力的映射 |
|---|---|---|---|
| **CodeGraph**（主体参照） | github.com/colbymchenry/codegraph | Rust 内核 + tree-sitter + SQLite + **Node 库 API** | C2 变更感知（`getImpactRadius`/`getCallers`/`getCallees`）、符号调用图、**路由节点（仅供交叉校验，路径不可直接采信）** |

> 另有一个**独立项目**（非"CodeGraph"）`raghurammrsd/CODE_GRAPH_MCP`（Python AST + SQLite + MCP），其 **DB 血缘能力**（`READS_TABLE`/`WRITES_TABLE`/`MAPS_TO_TABLE`/`FOREIGN_KEY_TO`）是AstSight **C5 造数 DB 血缘**的能力参照来源——仅此一项，其余仍以 CodeGraph 为主。

**关键借鉴：证据等级（epistemic state）机制**——这是**设计理念的借鉴，不是对 CodeGraph 输出的读取**。实测 1.6.2 的库返回与 CLI `--json` **均不携带** `FACT`/`POSSIBLE`/`UNKNOWN` 字段，因此 AstSight 的 `verified`/`inferred`/`unmapped` 必须**自行判定**，由来源组合决定：

| 理念分级 | 含义 | AstSight 对应场景 | AstSight 判定依据 |
|---|---|---|---|
| 可证明事实 | AST 可证明 | 确定性提取的符号/调用/路由 | 单一确定性来源，或双源独立一致 |
| 推断未证实 | 推断但未证实 | 动态分发（`getattr`）、别名解析 | 仅单一来源（如图谱单方给出） |
| 无法判定 | 无法判定 | 反射、动态 SQL、重名符号未消歧 | 来源冲突且无法仲裁 → `unmapped` |
| 多源矛盾 | 多源矛盾 | 接口契约（HTTP/gRPC/Dubbo）与 DDL 冲突 | 两路证据矛盾，保留双方证据不猜边 |

> AstSight 沿用这个理念：所有画像/血缘字段必须标注证据等级，**证据不足时显式标 `unknown` 而非猜测**——这是"事实源环节零 LLM"的底层保证。具体判定规则：**双源独立一致 → `verified`；单源 → `inferred`；冲突 → `unmapped`**（含"保留了冲突双方的证据"这一要求，不允许静默选边）。字段溯源仍保留 `constraint_source` / `inferred` / `unmapped` 三种口径。

**采用策略**：**只通过代码嵌入使用 CodeGraph，不自研 AST 图谱**（早期 tree-sitter 自研仅作可行性原型，非正式路径）。但**必须自研路由插件**——这不是可选项：实测 CodeGraph 的路由产出有四个静默缺陷（前缀丢失 ×2、method 缺失、节点数与清单不一致），直接采信会产出批量错误 URL，且错误不会以报错形式出现，只会表现为"测试失败但归因错"。是否可直接引用其调用图，仍须按语言/框架实测（见风险表）。

---

## 三、资产基线与能力规格（C1–C7）

> 先定义**资产**（3.1 规则库、3.2 用例库），再定义**能力**（C1–C7）。顺序不能反：规则库是需求分析的稳定产物，用例库是唯一被 C6/C7 共同消费、且独立于生成它的模型而存活的资产；能力只是往它们里面写、从它们里面取。
>
> 每项能力按统一模板：职责 / 输入 / 输出契约 / 核心逻辑（步骤级）/ 关键设计决策 / 异常与边界 / 门禁指标

### 3.0 运行模式（scope 三分）

触发方式会持续扩张（变更、定时、事件、流量），**因此框架不能以"变更驱动"命名，也不能只为变更设计**。三种模式共用同一条链路，差异只在"选哪个范围"：

| 模式 | 干什么 | 输入前提 | 何时用 | 核心约束 |
| -- | -- | -- | -- | -- |
| `full` | **建**基线：扫全量接口 → 画像 → 契约 → 生成 → 执行 | 代码库 + 文档即可，**不依赖 Git diff** | 首次接入、存量摸底、周期全回归 | **必须产出契约与画像基线**；只生成用例不建基线 = 负债 |
| `delta` | **维护**基线：只重判受影响范围 | 需要 Git 变更集 + 已有基线 | 每次提交、每次发布 | 未命中的接口**不得视为通过**，只能标注为未覆盖 |
| `replay` | **复用**基线：用今天的用例库重判历史数据 / 影子流量 | 需要历史请求样本 + 已有契约 + 已批准用例 | 用例/契约更新后回刷、事故回溯 | 结论必须携带**规则库版本号 + 用例库版本号**，否则跨版本不可比 |

**三条实现约束（不可妥协）**：

1. **三种模式不是三条 pipeline。** 画像、契约、生成、执行、判定五组组件完全共用，差异收敛为一个 `scope` 参数。若拆成两套代码，必然各自漂移——修一个 bug 要改两遍。
2. **CLI 契约统一**：`astsight run --mode full|delta|replay --project <id> [--sut-id <id>]`。模式是参数，不是子命令分支；`sut_id` 缺省取 project 名，多 SUT 并发隔离见 5.5。
3. **三种模式都必须输出覆盖边界**：`unmapped`（没看到 / 没判到）必须单列进报告。**静默通过是最大风险**——尤其是 `full` 模式扫出的"我以为我全测了"。

> 门禁口径随之收紧：C2 的「召回 ≥90%」只对 `verified` 路由成立，`unmapped` 必须独立计数并阻断放行，不得并入通过率分母。

---

### 3.1 业务规则库（Rule Store）——需求分析的稳定产物

> **规则库不是运行时判定表。** 它把需求/设计文档中的业务约束变成可追溯、可 diff、可审计的
> 结构化规则，供 C4a 设计业务级用例、C4b 选择变更缺口、报告计算规则覆盖与漂移。
> C7 不执行规则表达式；判定仍由已批准用例断言完成。

**两种规则来源**：

| 来源 | 编号 | 生成方式 | 权威等级 |
| --- | --- | --- | --- |
| 需求/设计文档 | `BR-NN` | A1 需求分析（LLM，prompt=`a1_requirement_analysis.md`） | A / B |
| 接口契约（OpenAPI / Spring MVC 注解 / gRPC proto / Dubbo 接口） | `CR-NN` | C1 确定性提取（required/type/enum/min/max/format） | A（契约事实） |

#### 3.1.1 数据模型

| 实体 | 关键字段 | 说明 |
| --- | --- | --- |
| `RuleSet` | schema_version, ruleset_id, project, version, generated_at, prompt{name, version, model}, documents[], rules[], contract_rules[], questions[], gaps[], conflicts[] | 当前规则库快照；`ruleset_id` = 文档 hash + 契约源 hash + prompt 版本 |
| `Rule` | rule_key, rule_id, text, type, entities[], fields[], constraints[], source{doc, version(sha256), doc_version, section, kind}, authority, impact, priority, reasoning, fingerprint | BR 业务规则；字段对齐 a1 提示词输出契约（`version` 回填为文档 hash） |
| `RuleConstraint` | field, operator(eq/ne/in/not_in/min/max/between/pattern/required/forbidden/transition/other), values[], condition, error_code, exhaustive | 结构化约束；只用于 quote 落地与确定性冲突检测，不是运行时表达式 |
| `ContractRule` | rule_key, rule_id, transport, endpoint, field, constraint, source, authority=A | CR 契约规则；由契约源（HTTP：OpenAPI / Spring MVC 注解；gRPC：proto/descriptor；Dubbo：接口源码）确定性生成，不调用 LLM |
| `RuleQuestion` | id, text, reason, impact | 文档没写清、必须人工澄清的问题；不参与判定 |
| `RuleGap` | kind(implicit/quantity/cross_system), desc, reason, related_interfaces[] | 抽不出的规则；不占 BR 编号，不计入覆盖率分母 |
| `RuleConflict` | topic, rule_keys[], docs[], origin(model/framework/model+framework), reason | 模型自报或框架确定性检测到的规则冲突；只记录，不改变规则状态，在用例审核中确认 |

**身份与字段规则**：

- `rule_key = "RK-" + sha256(doc | section | normalized_text)[:12]`，是跨版本稳定身份；
  `rule_id`（`BR-NN`/`CR-NN`）只用于展示与排序。**用例引用 `rule_key`，不引用会漂移的编号。**
- `source.section` 必须是 context 中真实存在的 Markdown section 路径。实例化 `prompt.md` 时
  同时提供原文章节号；导入时接受 section 路径或章节号，统一映射到路径。解析不到 section
  的规则标记 `ungrounded`，不得作为 A 级。
- 提示词输出的 `source.version` 是文档自报版本（如 `v1.3`）：导入时保留为
  `source.doc_version`，并用 context 的文档 sha256 回填 `source.version`，漂移判断只看
  sha256，不看自报版本号。
- 提示词输出的 `id` 在导入时映射为 `rule_id`；`source.kind` / `authority` / `impact` /
  `priority` 原样保留。`source.kind` 只有 `explicit` / `inferred` 两值；`reasoning` 仅 P0
  与高风险规则输出（提示词声明下游可忽略），框架保留用于审核阅读与审计，不进入
  fingerprint、不进入执行。
- `Rule.fingerprint` 覆盖 `text / type / fields / constraints / source(doc, section, kind) /
  authority / priority`；不覆盖 `rule_id / reasoning / source.version / source.doc_version /
  生成时间`。空约束不写入 fingerprint payload，兼容加结构化约束之前保存的规则库。
  文档 hash 变化但规则文本与 section 未变时不算规则变更，只记 `doc_version_drift`。
- `authority` 只允许 A/B/C/D；A1 的输入只有文档，因此只产出 A/B。C（代码反推）与
  D（历史缺陷）只能来自其他导入通道，且**不得单独作为 oracle**。

存储不变量：

- `rule_key` 在 `RuleSet` 内唯一；`rules` 内不得混入 `questions`/`gaps`。
- 同一 `source.section` 可对应多条规则；一条规则只能有一个 `source`。
- `RuleConflict` 只记录冲突，不改变规则 `status`；参与冲突的规则仍可被 A4 引用，
  冲突进入 A4 输入与用例审核文件，由用例审核这一步统一确认。
- `gaps` 不是规则：报告单列“规则缺口”，不并入规则覆盖率分子/分母。
- 加载时重算 `rule_key` 与 fingerprint；不匹配即拒绝加载。

#### 3.1.2 生成与导入

**唯一用户入口：需求文档。**

```
rules extract --project <repo> --requirements <docs> [--openapi ...]
  → .astsight/rules/<analysis_id>/
      context.json      # 文档 section/quote/hash + 契约端点目录（HTTP：OpenAPI/Spring MVC；gRPC：proto/descriptor；Dubbo：接口源码）+ transport
      prompt.md         # a1_requirement_analysis.md 的实例化
      rules.schema.json # RuleDraft 输出 schema
      rules.draft.json  # 本次模型输出（先落盘再校验）
  → 确定性校验 → rule_key diff → ruleset.json (version + 1) + ruleset.md
```

需求修复后重跑同一条命令；每次抽取都是完整快照。

**模型在框架外运行（同一流程的两段式）**：需要把模型调用从框架里拿出去时，使用：

```
rules prepare --project <repo> --requirements <docs> [--openapi ...]
  → .astsight/rules/<analysis_id>/（context/prompt/schema/README）

Claude Code Skill / 人工 / 企业模型网关 / 离线批处理读取 context + prompt
  → rules.draft.json   # { questions: [], rules: [], gaps: [], conflicts: [] }

rules import --context <analysis_id> --draft rules.draft.json
  → 与 extract 完全相同的校验 → rule_key diff → ruleset.json + ruleset.md
```

- **模型只是候选来源**：无论模型在框架内（`rules extract` 的适配层）还是框架外运行，
  `rule_key`、fingerprint、A 级落地、冲突检测、CR 契约规则与版本号都由框架重算；
  校验失败不写 `ruleset.json`。框架内模式是可选适配器，核心 `rules/` 包保持零模型。
  `ruleset.json` 记录 `prompt.version` 与 `model`，保证“哪版提示词、哪个模型”可审计；
  同一输入重跑可能得到不同规则，**人工审核用例是最终稳定器**。
- **规则阶段没有人工审核节点**：规则导入只做确定性校验并直接落库；规则在被至少一条
  approved 用例引用后才成为“已确认规则”。`questions`/`gaps`/`conflicts` 在报告与
  用例审核文件中显式展示，全流程唯一的人工门禁是用例审核。
- `rules extract`/`rules import` 不修改 `caseset.json`；规则变化对用例的影响通过
  `rule_drift` 标记与重新审核解决（见 3.1.3）。

#### 3.1.3 规则 diff、生命周期与漂移

以当前 `ruleset.json` 为基线，按 `rule_key` 分类：

| 情况 | 处理 |
| --- | --- |
| `rule_key` 存在且 fingerprint 相同 | `unchanged` |
| `rule_key` 存在但 fingerprint 不同 | `updated`：依赖它的 approved 用例标 `rule_drift` |
| 新 `rule_key` | `new`：可被新用例引用 |
| 基线规则本次消失 | `deprecated`：不再参与新用例；已批准用例标 `rule_orphaned`，不自动删除 |
| `source.section` 或文档 hash 变化但规则未变 | 只记 `doc_version_drift`，不要求重审 |
| `questions`/`gaps` 变化 | 报告展示，不改变已有用例状态 |

生命周期只有 `active` / `deprecated` / `ungrounded` 三态，没有规则级审批流：

- `active`：可用于设计用例；参与冲突的规则仍为 `active`；
- `deprecated`：新分析中消失；引用它的用例保留并标 `rule_orphaned`；
- `ungrounded`：quote/来源不足以支撑规则值，需修正草案后重新导入；不得作为用例依据。

`rule_drift` 的处理：`full`/`delta` 默认把受影响的 approved 用例视为 `stale` 并阻断，
要求重新审核或显式 `--allow-stale-cases` 降级为 warning + `degraded`；`cases` 模式只告警。
这样规则文本变化不会静默改变判定口径。

#### 3.1.4 权威等级与依据

| 等级 | 来源 | 能否单独作 oracle | 处理 |
| --- | --- | --- | --- |
| A | 文档显式约束 / 接口契约（HTTP：OpenAPI / Spring MVC 注解；gRPC：proto/descriptor；Dubbo：接口源码） | 可以 | 仍需人工审核用例 |
| B | 文档流程描述推断 | **不可以** | 审核文件必须展示 `reasoning`；批准引用它的用例即视为确认该规则 |
| C | 代码反推 | **禁止** | 只能作差分参考；引用它的用例必须有 A/B 依据 |
| D | 历史缺陷归纳 | 仅作回归断言 | 必须标 provenance，且不能替代 A/B |

**规则值落地校验（确定性）**：规则 `text` 中的数字、反引号内容、错误码，以及结构化
`constraints.values` / `constraints.error_code`，必须能在 `source.quote` 中逐字找到。
值只在 `source.section` 其他位置出现时标 `ungrounded`（A 级同时降 B）；section 中完全
不存在则视为编造并拒绝导入。该检查只保证“值有出处”，不证明语义正确——语义仍由用例审核确认。

**结构化约束与冲突检测**：`Rule.constraints`（`field/operator/values/condition/error_code/
exhaustive`）只用于确定性比较，不是运行时表达式。框架对同一 `(field, condition)` 的结构化
约束做范围、枚举、必填/禁止、错误码冲突检测，生成 `RuleConflict(origin="framework")`；
模型自报的冲突标 `origin="model"`，二者可合并为 `model+framework`。冲突只记录，不改变
规则状态，也不阻断导入。

**冲突处理**：规则阶段不设人工裁决。`RuleConflict` 进入 A4 输入和用例审核文件；
A4 不得自行合并或改写冲突规则，引用冲突规则的用例带 `rule.conflict` warning；
审核人在唯一的用例审核节点决定接受、修改或拒绝相关用例。执行报告把冲突列为待确认项，
不作为 blocker。

#### 3.1.5 规则覆盖

```
规则覆盖率 = 被至少一条 approved 用例引用的 active 规则数 ÷ active 规则总数
```

- `BR`（业务规则）与 `CR`（契约规则）分别统计，**不得合并成一个百分比**：一个高、
  一个低，说明“业务覆盖好但契约边界没测”或相反。
- `gaps` 单列为“规则缺口”，不是分母；`questions` 单列为“待澄清”。
- 规则覆盖率是设计指标，不是通过率；它回答“需求里的约束有多少变成了用例”，
  不回答“系统对不对”。
- 需求 section 覆盖率仍按 3.2.5 统计，作为规则覆盖的补充视图。

#### 3.1.6 门禁

| 指标 | 阈值 / 条件 | 说明 |
| --- | --- | --- |
| 契约源 | 每个被支持端点至少一种：OpenAPI / Spring MVC 注解 / gRPC proto / Dubbo 2.7+ 接口 | 全缺失时 CR 为空、标 `contract_incomplete`，阻断契约断言用例；`unsupported` 端点单列 |
| 规则库可加载 | schema 受支持；`rule_key`/fingerprint 重算一致 | 否则拒绝加载 |
| 来源可定位 | `source.section` 解析成功 | 解析失败标 `ungrounded`，不得为 A |
| 规则值落地 | 数字/枚举/错误码能在 `source.quote` 找到 | 只在 section 其他位置：ungrounded/降 B；哪儿都没有：拒绝 |
| 结构化冲突 | 同 `(field, condition)` 的范围/枚举/错误码矛盾时记录 | 自动生成 `RuleConflict`，不阻断导入 |
| 冲突展示 | 所有冲突进入 A4 输入与用例审核文件 | 缺失时报告不完整；用例审核确认 |
| C/D 规则 | 不得单独作 oracle | approve 时阻断 |
| 规则覆盖 | 可选阈值（BR/CR 分开） | 未覆盖规则必须列出 |
| 规则缺口 | 报告展示；`--fail-on-rule-gaps` 可升级为 blocker | 默认 warning |
| 规则漂移 | 受影响用例 `stale`；full/delta 默认阻断 | `--allow-stale-cases` 可降级并标 `degraded` |
| 规则规模 | 单次导入规则数、单条文本/字段数设上限 | 防止模型灌水与失控 |

---

### 3.2 用例库（Case Store）——唯一的执行与判定资产

> **用例库是平台的一等执行资产。** 规则库（3.1）负责“需求说了什么”，用例库负责
> “怎么请求、断言什么”。C4 写入草案并组织审核，C6 从里取已批准用例编码，C7 拿同一批
> 用例判定——四者共用同一份，不得各自复制。

**规则库与用例库的分工（硬边界）**：

| 维度 | 规则库（3.1） | 用例库（3.2） |
| --- | --- | --- |
| 层次 | 设计期 | 执行期 |
| 单位 | 业务约束（条件 → 结果） | 请求 + 断言 |
| 生成 | A1 LLM 分析 + 导入校验；CR 确定性提取 | 业务级/代码级草案 + 人工审核 |
| 判定 | **不作 oracle** | **唯一 oracle** |
| 变更 | `rule_key` + fingerprint diff | `(endpoint, case_key)` + fingerprint diff |
| 覆盖 | 规则覆盖（BR/CR 分列） | 需求 section / 变更 / 执行覆盖 |
| 失效 | `deprecated` / `ungrounded` | `approved` / `deprecated` / `superseded` |

一句判据：**删掉生成它的 Skill/模型，已批准用例仍能独立执行并判定 → 合格；离开模型就
无法执行或判定 → 不合格。规则库可以丢，用例库不能丢。**

#### 3.2.1 数据模型

| 实体 | 关键字段 | 说明 |
| --- | --- | --- |
| `CaseSet` | schema_version, fingerprint_version, id, project, store_version, sequence, ruleset{version, hash, id}, openapi, cases[], history[], review_log[], doc_versions, requirements[], uncovered_reasons | 已批准用例库；唯一可移植、可版本化的审计源 |
| `CaseEntry` | id, case_key, level(business/code), transport, endpoint, coverage_role, category, priority, request(path_params/query/headers/body), preconditions[], mock_bindings{dep_id: branch}, asserts[{kind,target,op,expect}], assert_strength, rule_refs[], requirement_refs[], change_refs[], status, version, fingerprint, review, created_at, updated_at | 单条可执行用例；`id` 为 `TC-NNNN`，只在批准时分配 |
| `RuleRef` | rule_key, rule_id, authority, source_kind | 用例引用的规则；`rule_key` 稳定，`rule_id/authority` 为批准时快照 |
| `ChangeRef` | base, target, symbols[], changed_api | 代码级用例的变更来源；不进入 fingerprint |
| `CaseReview` | decision, by, at, note, fingerprint | 最近一次审核决定；`fingerprint` 必须与条目一致 |
| `ReviewEvent` | proposal_id, case_key, action, decision, reviewer, at, note, fingerprint | 追加式审核日志，不覆盖历史 |
| `RequirementDoc` | doc, version, sections[] | 需求文档版本与参与覆盖率计算的 section |

存储不变量：

- `cases` 只允许 `approved` / `deprecated`；`history` 只允许 `superseded`。
- 唯一键是 `(id, version)`，不是单独的 `id`：同一 `TC-NNNN` 可同时出现在 `cases` 与 `history`。
- `sequence >= max(cases + history 中的 TC 编号)`，写库后只增不减，编号永不复用。
- 加载时重算 fingerprint；不匹配即硬错误。已批准条目的 `review.fingerprint` 必须与 `fingerprint` 一致。
- `schema_version` / `fingerprint_version` 高于支持版本时拒绝加载，不做静默降级。

**两级用例（level）**：

| level | 来源 | 范围 | 必须携带 | 断言边界 |
| --- | --- | --- | --- | --- |
| `business` | 规则库 + Context + 契约源（C4a） | `full` 全量 / 需求变更 | `rule_refs` ≥1、`requirement_refs` ≥1 | 业务结果、状态码、关键字段；可声明前置状态 |
| `code` | ChangedAPI + Profile + RuleSet（C4b） | `delta` 增量，仅补 business 未覆盖的规则/分区 | `rule_refs` ≥1、`change_refs` ≥1 | 字段边界、错误分支、变更影响面；不得从 diff 反推断言 |

- **business 是权威层**：同一 `(endpoint, case_key)` 只能有一条用例；code 用例不得与
  business 用例的期望冲突，冲突时以 business 为准，code 用例必须修改或废弃。
- **code 只补缺口**：C4b 必须先计算“变更接口 × 规则 × 已有 business 用例”的覆盖矩阵，
  只为未覆盖的规则/分区生成用例；已覆盖的不重复生成。
- **没有规则就没有 code 用例**：变更行为找不到任何 A/B 规则或 CR 契约规则时，不生成
  用例，写入 `change_without_oracle`，报告单列——这是“文档/规则需要补充”的信号，
  不是让模型从 diff 猜期望。
- **级别可升级**：code 用例若揭示了文档未写的业务规则，应先把规则补进 `ruleset.json`、
  升级为 business 用例，而不是长期停留在 code 级。
- 两级用例共用同一套校验、审核、fingerprint、版本与 `TC-NNNN` 编号；`level` 进入
  fingerprint（级别变化必须重审）。

#### 3.2.2 生命周期与审核

| 用例状态 | 含义 | 允许转移 |
| --- | --- | --- |
| `approved` | 审核通过，参与编码与判定 | → `deprecated`（新版本进入 `history`） |
| `deprecated` | 业务下线或需求移除，保留编号与审计信息 | 终态；可经 `revive` 重新批准并升版本 |
| `superseded` | 被新版本替代，仅存在于 `history` | 终态 |

审核决定只有三种：`pending` / `approve` / `reject`。**审核前不进入 `caseset.json`**：
`cases import` 只生成 `.astsight/cases/proposals/<proposal_id>/` 下的 proposal、
`cases.review.md/json`、`validation.json` 和 `requirements_snapshot.json`。

`cases approve` 的正确性约束：

1. **重新推导，不信任审核文件内容。** 读取 proposal、原始 draft、需求快照和当前 store；
   review 文件只取 `decision/reviewer/note`；候选用例重新校验、重新分类、重新计算 fingerprint。
2. **所有 actionable 项必须有决定**，遗留 `pending` 直接拒绝批准。
3. **乐观锁**：`base_store_version` 与 `base_store_hash` 必须同时匹配当前 store；写入前再比较一次。
4. 批准 `new` 分配新 `TC-NNNN`；批准 `update`/`revive` 沿用原编号并 `version+1`，
   旧版本进入 `history` 并标 `superseded`；批准 `deprecate` 只把原用例标 `deprecated`。
5. **不提供批量废弃**：`deprecate` 必须逐项批准；reject 一律保持现状。
6. 写库前备份旧 `caseset.json`（保留最近 10 份），再原子替换；`store_version += 1`，
   所有决定追加 `review_log`，并写 `approval.json` 标记 proposal 已应用。

#### 3.2.3 与已批准用例库的 diff

`cases import` 以当前 `caseset.json` 为基线，按 `(endpoint, case_key)` 锚点分类：

| 情况 | 处理 |
| --- | --- |
| 锚点存在且 fingerprint 相同 | `unchanged`：自动沿用，不进审核文件；文档 hash 变化只记 `doc_version_drift` |
| 锚点存在但 fingerprint 不同 | `update`：生成待审变更；旧批准版本继续生效直到审核通过 |
| 锚点只存在于 `deprecated` 用例 | `revive`：生成待审恢复；批准后重新激活并升版本 |
| 新锚点 | `new`：生成待审新增 |
| store 中已批准用例在本次 scope 内、但草案中消失 | `deprecate`：生成待审废弃，必须显式批准 |
| store 中用例的文档不在本次 scope | `kept_out_of_scope`：原样保留，不自动废弃 |
| 草案为空但 store 非空 | 拒绝导入，防止一次空草案清空用例库 |
| 草案内 `(endpoint, case_key)` 重复 | 校验失败，不生成 proposal |
| 全部 `unchanged` | 不生成 proposal，直接报告“无变更” |

scope 判定：

- **business 草案**：显式 `scope_docs`；缺省取 cases + uncovered 实际引用的文档，**绝不默认
  全部 context 文档**。只有用例的**所有** requirement 文档都在 scope 内，才考虑废弃；
  混合文档用例保留并列入 `kept_out_of_scope`。
- **code 草案**：显式 `scope_changes`（base/target/变更接口）；只在该变更范围内做
  new/update/deprecate 判断，范围外的 code 用例原样保留。
- 不允许通过缩小文档或变更范围静默提高覆盖率；`kept_out_of_scope` 必须单列。

#### 3.2.4 fingerprint 与版本

fingerprint 只覆盖**会改变执行行为或需求归属**的内容：

- **包含**：`level`、`transport`、`endpoint`、`coverage_role`、`case_key`、`category`、`priority`、`method`、`path`、
  `path_params`、`query`、`headers`、`body`、`preconditions`、`mock_bindings` 的
  `(dep_id, branch)`、`asserts`、`assert_strength`、`requirement_refs` 的
  `(doc, section, quote)`，以及 `rule_refs` 的 `rule_key`。
- **不包含**：`id`、`source`、`title`、`note`、`rationale`、`SourceRef.version`、`version`、
  `change_refs`、`business_key`、`alias_of`、`adapter_fingerprint`、规则快照中的
  `rule_id/authority`、审批信息、时间戳。

`change_refs` 不进入 fingerprint：换了分支/提交但用例内容没变，不应要求重审；
但报告仍按最新 `change_refs` 做漂移与选择。

因此文档 hash 变化但 section/quote 未变时，不触发重新审核，只产生 `doc_version_drift` 警告；
改标题或说明文字也不要求重审。反过来，任何请求参数或断言变化都必须重新审核。

版本规则：

- `TC-NNNN` 只在批准写库时分配，从 `cases + history` 的最大编号递增，永不复用。
- 同一用例的更新/恢复沿用原编号并升 `version`，旧版本进入 `history`。
- `store_version` 每次成功批准 +1；proposal 必须绑定写入时的 `base_store_version` 与
  `base_store_hash`，防止并发批准覆盖。跨进程还需持有 `<case-base>.lock` 的 `flock`
  （见 5.5）；`base_store_hash` 只缩小 TOCTOU 窗口，不替代锁。
- store 以 canonical JSON + sha256 表示；写入采用同目录临时文件 + 原子替换。

#### 3.2.5 需求覆盖与断言强度

**需求覆盖率**以 Markdown section 为单位，不以用例条数为单位：

```
需求覆盖率 = 被至少一条 approved 用例引用的 section 数 ÷ 参与统计的 section 总数
```

- 元数据标题（版本、变更记录、目录、附录、术语、背景、范围、目标、参考资料、待确认）
  不计入分母，但仍出现在 context 中供生成参考。
- 同一 section 被多条用例引用只算一次；用例再多也不能补上一个从未被引用的 section。
- 无法自动化的 section 必须写入 `uncovered_reasons`（如 `needs_c5_state_setup`），
  **报告必须列出未覆盖 section 与原因，不得静默略过**。
- 覆盖率阈值默认不设，避免为了刷指标生成低价值用例；需要门禁时显式配置。

**变更覆盖率**（`delta` / code 级）：

```
变更覆盖率 = 命中至少一条 approved 用例（business 回归或 code 补充）的变更接口数
             ÷ ChangedAPI 总数
```

- 未命中的变更接口必须列入 `change_uncovered`，不得视为通过。
- `change_without_oracle` 单列：变更发生但找不到任何 A/B 规则或 CR 契约规则可依据，
  说明需要补文档/规则，**不允许模型从 diff 猜期望凑用例**。
- 变更覆盖、规则覆盖（BR/CR）、需求 section 覆盖三条轴分开报告，不合并成一个百分比。

用例断言保留强度档，审核时必须看清“这条用例到底验了什么”：

| 档 | 形态 | 例 | 能抓到什么 |
| --- | --- | --- | --- |
| L1 | 状态码 / 字段存在 | `status==200`、`data.id != null` | 崩溃、契约破坏 |
| L2 | 确定值 / 字段值 | `total == 108.00` | 计算错误（数据一变就误报） |
| L3 | 关系式 | `refund <= order.paid` | 计算口径错误 |
| L4 | 跨实体不变量 | 余额=流水和、库存≥0、状态机合法 | 资损、状态错乱 |

**实现现状**：当前 codegen 支持状态码、字段存在、字段等值/范围/包含（L1–L2）。
L3 关系式与 L4 跨实体不变量尚未实现；草案若只能给出 L1/L2 而需求要求更高强度，
必须在 `assumptions` 或 `uncovered_requirements` 中显式说明，**不得把降级断言标成已覆盖**。

#### 3.2.6 门禁

| 指标 | 阈值 / 条件 | 说明 |
| --- | --- | --- |
| 用例库可加载 | schema / fingerprint 版本受支持，fingerprint 重算一致 | 否则拒绝加载 |
| 两级用例完整性 | business：`rule_refs`+`requirement_refs` 非空；code：`rule_refs`+`change_refs` 非空 | 缺失拒绝导入 |
| 规则引用有效 | `rule_key` 可解析；非 `ungrounded`；C/D 不得单独作 oracle | approve 时阻断；引用冲突规则只告警 |
| 规则漂移 | 引用 `rule_drift` 规则的用例标 `stale` | full/delta 默认阻断，可 `--allow-stale-cases` |
| 无可执行用例 | `cases` 模式 blocker | 不编码、不执行 |
| 待审 proposal | warning + `degraded` | `--fail-on-pending-proposals` 升级为 blocker |
| approve 完整性 | 所有 actionable 项已有决定 | 遗留 `pending` 拒绝批准 |
| 乐观锁 | `base_store_version` + `base_store_hash` 匹配 | 不匹配要求重新 import |
| deprecate | 逐项批准 | 禁止批量废弃 |
| 需求覆盖 | 可选阈值 | 未覆盖 section 必须给出 reason |
| 规则覆盖 | 可选阈值（BR/CR 分列） | 未覆盖规则必须列出 |
| 变更覆盖 | `delta` 未命中接口与 `change_without_oracle` 单列 | 不得视为通过 |
| 契约/需求漂移 | endpoint 缺失 blocker；doc hash 变化 warning | `--allow-stale-cases` 可降级并标 `degraded` |
| 断言保真 | 不得把 L3/L4 需求降级成 L1/L2 而不声明 | 逐条列出（设计目标） |
| 变异存活率（`delta`） | 0% | 设计目标，尚未实现 |

---

### 3.3 接口形态与 transport（v4.2）

> 本节定义“测什么形态的接口、用什么通道调用”。`transport` 是端点的属性，在 C3 画像时确定并冻结进用例；C4/C6/C7 不临时猜测，也不在运行时静默切换。

**支持范围**：

| transport | 支持 | 契约来源 | 调用方式 | 明确不支持 |
| --- | --- | --- | --- | --- |
| `http` | ✅ | OpenAPI / Spring MVC 注解 | HTTP 客户端 | — |
| `grpc` | ✅ 仅 unary | `.proto` / descriptor；server reflection 仅作辅助 | gRPC 客户端 | streaming / 异步 / oneway；无 descriptor 且 reflection 关闭 |
| `dubbo` | ✅ Dubbo 2.7+ | 标准注解 / XML；接口类必须在仓库源码内 | Java sidecar 泛化调用 | Dubbo 2.6；接口类只在依赖 jar；动态代理/反射/`getBean`；非标准封装注解 |

**端点标识**：

| transport | `endpoint` |
| --- | --- |
| HTTP | `POST /api/orders` |
| gRPC | `grpc:/acme.order.v1.OrderService/Create` |
| Dubbo | `dubbo:com.acme.OrderService#create?group=order&version=1.0.0` |

HTTP 端点继续保留 `method/path`；gRPC / Dubbo 端点使用 `endpoint` 字符串，不伪造 `method/path`。

**transport 选择（C3 确定性判定）**：

1. HTTP 契约存在 → `transport=http`；
2. 原生 gRPC 服务且有 proto/descriptor → `transport=grpc`；
3. Dubbo 服务：
   - Dubbo 3 + **Triple 协议** + **protobuf 序列化** + descriptor + 无 group/version → `transport=grpc`；
   - 其余（Dubbo 2.7/3 非 Triple、Hessian2、有 group/version、无 descriptor）→ `transport=dubbo`，走 sidecar；
   - 证据不足（协议/序列化配置不在仓库、proto 存在但不能证明实际使用、同一服务暴露多协议）→ 默认 `transport=dubbo`，标 `transport_evidence=inferred`；
   - sidecar 也无法解析（接口只在 jar、2.6、动态代理）→ `unsupported`，不得降级硬跑。

> **proto 存在 ≠ 服务走 gRPC**。只有能证明“该服务实际以 Triple + protobuf 暴露”时才选 gRPC；仅仓库里有 proto 文件不够。

**transport 元数据**：

```json
{
  "transport": "http | grpc | dubbo",
  "transport_evidence": "verified | inferred",
  "transport_class": "http | grpc | dubbo"
}
```

- `transport_evidence`：区分“证明是 gRPC”与“默认 sidecar”的推断；
- `transport_class`：标记端点所属协议家族；只有 `transport_class=dubbo` 的端点允许 gRPC → sidecar 回退。

**gRPC 约束**：

- 首版只做 **unary**；streaming/异步/oneway 标 `unsupported`；
- 路径固定为 `/{package}.{Service}/{Method}`，必须与 descriptor 一致；
- descriptor 优先从仓库 `.proto` 读取，不假设服务端开启 reflection；
- 服务发现先解析地址（直连/注册中心），再建立 gRPC 连接；
- gRPC metadata 与 Dubbo attachments 显式映射，鉴权/租户头不能丢；
- gRPC status 归一为统一错误结构；
- gRPC 本身不携带 Dubbo 的 group/version；有 group/version 语义时不得走 gRPC。

**Dubbo sidecar**：

- 独立 Java 进程，通过 stdio JSON 接收调用请求；**不 Attach 被测 JVM、不使用 java agent**；
- 支持直连 URL、Nacos、ZooKeeper 发现；
- 使用 `GenericService` / `ReferenceConfig<GenericService>` 泛化调用，不要求 consumer 侧引入 api jar；
- 首版固定 Hessian2 序列化；Kryo/FST/自定义序列化标 `unsupported`；
- `param_types` 必须与 provider 签名完全一致；重载方法必须显式声明；
- 透传 timeout、attachments、鉴权/租户头；
- 返回统一结果：成功值、`RpcException`、超时、连接失败；
- sidecar 只调用目标服务，不执行被测仓库代码。

**Dubbo 识别（确定性，零 LLM）**：

- 支持的全限定注解：`org.apache.dubbo.config.annotation.DubboService/DubboReference`（2.7/3.x）、`com.alibaba.dubbo.config.annotation.Service/Reference`（2.6，识别后标 `unsupported`）；
- 禁止按简单名匹配 `@Service`/`@Reference`，避免与 Spring stereotype 冲突；
- Provider：实现类注解 + `implements` 接口；`interfaceClass`/`interfaceName` 覆盖默认接口；多接口未显式指定标 `ambiguous`；
- Consumer：字段注解 + 字段类型；`url=` 直连与注册中心两种目标；
- XML `dubbo:service` / `dubbo:reference` 作为注解缺失时的兜底；
- 服务键 = `interface + group + version`，缺省值显式补齐；多实现/多分组必须靠服务键消歧；
- 接口类必须在仓库源码内；只在依赖 jar 时标 `interface_in_jar`，不生成用例；
- 动态代理/反射/`getBean` 获取的服务标 `dynamic_lookup`，不生成用例；
- 所有不支持项进入报告缺口清单，不静默忽略。

**回退策略（仅 Dubbo 家族）**：

- 只有 `transport_class=dubbo` 且被判定为 `transport=grpc` 的端点，才允许 gRPC 失败后回退 sidecar；
- 默认**不回退**；显式 `--allow-transport-fallback` 才启用，且只重试一次，不循环；
- 触发条件：gRPC 返回 `UNIMPLEMENTED`、协议/序列化不匹配、gRPC 路径不存在；
- 回退成功也必须在报告标 `transport_fallback` 与 `degraded`，不得视为干净通过；
- 回退失败 → `unsupported` 或失败，不静默；
- 原生 gRPC 服务、HTTP 端点不存在 sidecar 回退。

**方向边界**：

- **测试发起方向**（AstSight → 被测服务）：Dubbo/gRPC 调用仍走 sidecar / gRPC 客户端，**不 Attach 被测 JVM、不装 agent**；
- **下游 Mock 方向**（被测服务 → 下游）：v4.3 起 SUT → Dubbo 2.7+ 下游支持 Mock，使用 **Java agent + ByteBuddy + Dubbo Filter**，不改 SUT 代码/构建；SUT → gRPC 下游仍 `mock=unsupported`（v4.4 决策改用 gripmock 独立服务器，见 C3.5.4）；
- 两个方向的 agent 边界不得混用：调用 SUT 不依赖 agent；Mock 下游的 agent 只拦截 SUT 的出站 Dubbo 调用，不参与调用 SUT；
- 由于下游 Mock 需要向被测 JVM 注入 agent，整体定位由“零侵入”调整为**低侵入**：对代码与构建零侵入，对运行期低侵入，且必须显式声明。

**端点覆盖角色（同一业务实现暴露多协议）**：

> 同一个业务实现可能同时通过 HTTP、Dubbo、gRPC 暴露。**同一个 service 不等于同一个被测接口**：业务场景可以复用，传输契约不能默认省略。C2/C3 必须保留每个 `transport + endpoint`，不按方法名去重。

| 情况 | 是否同一端点 | 处理 |
|---|---|---|
| 同协议多实例/多端口（HTTP 8080/8081、Dubbo 多实例） | 是 | 保留一个 `endpoint`，实例选择交给负载均衡/直连配置 |
| 同实现、不同协议（HTTP + Dubbo、HTTP + gRPC） | 否 | 按 `business_key` 归组，分配 `coverage_role` |
| 同协议不同 group/version（Dubbo） | 否，服务键不同 | 必须独立覆盖，不视为 alias |

- `business_key`：实现符号 + 方法（如 `com.acme.OrderServiceImpl#create`），用于把多协议端点归组；端点身份仍是 `transport:endpoint`。
- `coverage_role`：
  - `primary`：跑全量业务用例；
  - `alias`：只跑 transport 契约冒烟（成功 + 一个错误/异常分支），验证序列化、错误映射、鉴权/超时适配；
  - `independent`：适配差异大（请求/响应模型、序列化、错误映射、拦截器、参数校验位置不同，差异项超过阈值），跑全量。
- `primary` 选择：由 `transport_priority` 显式配置；未配置时默认 `http > grpc > dubbo` 取第一个可用，并在报告标 `primary_inferred`。生产入口已知时应显式指定，不以默认值假装确定。
- `adapter_fingerprint`：对请求模型、响应模型、序列化、错误映射、拦截器/鉴权做哈希。指纹相同 → alias 可标 `equivalent` 并显式跳过；指纹不同 → 至少冒烟，差异大则升 `independent`。指纹算不出时按冒烟处理，不得直接跳过。
- 覆盖模式 `--transport-scope`：
  - `all`（默认）：`primary` 全量 + `alias` 冒烟；
  - `primary`：只跑 `primary` 与 `independent`，alias 写入报告 `transport_alias_skipped`，并标 `degraded`；
  - `full`：所有端点全量，用于高风险变更或回归。
- alias 无法执行（无 descriptor、sidecar 不可用、接口在 jar）→ `unsupported`，不是“跳过”；`unsupported` 不计入已覆盖。

---

### C1 需求分析（文档 → 业务规则）

| 项  | 内容 |
| -- | -- |
| 职责 | 把需求/设计文档 + 契约源组织成分析上下文，调用 LLM（prompt=`a1_requirement_analysis.md`）产出业务规则；同时确定性提取契约规则；完成规则校验、diff 与版本化 |
| 输入 | 需求文档是入口；契约源为可选增强：OpenAPI（显式指定、项目内发现或 `--base-url` 拉取）、Spring MVC 注解、gRPC `.proto`/descriptor、Dubbo 2.7+ 接口（源码可见） |
| 输出 | `.astsight/rules/<analysis_id>/`：`context.json`、`prompt.md`、`rules.schema.json`、`rules.draft.json`、`README.md`；经 `rules extract`（框架内模型）或 `rules import`（框架外模型）写入项目级 `ruleset.json`，并渲染只读 `ruleset.md` |
| 入口 | `astsight rules extract --requirements ...`；模型在框架外运行时拆为 `rules prepare` + 外部产出草案 + `rules import` |

> 规则层的引入/删除/恢复过程见 [`决策记录.md`](决策记录.md) §2。

**分析上下文内容**：

```json
{
  "context_id": "sha256(docs + sections(path/text_hash/include) + contract/routes + schema_version + prompt_version)",
  "documents": [{"doc": "requirements.md", "version": "sha256", "sections": [{"path": "订单 > 创建", "text": "..."}]}],
  "excluded_sections": ["文档说明", "变更记录"],
  "endpoints": [
    {"transport": "http", "method": "POST", "path": "/api/orders", "params": {}, "request_body": {}, "success_codes": [201]},
    {"transport": "grpc", "endpoint": "grpc:/acme.order.v1.OrderService/Create", "request_type": "CreateOrderRequest", "response_type": "CreateOrderResponse"},
    {"transport": "dubbo", "endpoint": "dubbo:com.acme.OrderService#create?group=order&version=1.0.0", "param_types": ["com.acme.CreateOrderRequest"]}
  ],
  "limits": {"max_rules": 500, "max_rule_text": 500, "max_fields": 50}
}
```

**核心逻辑**：

1. 扫描文档，跳过 `.astsight`、`.git`、`__pycache__`；拒绝绝对路径、`..` 和指向项目外的 symlink
2. 按 Markdown 标题建立稳定 section 路径（如 `订单服务接口设计 > POST /api/orders`），保留正文、表格与代码块文本，计算文档与 section 的 sha256
3. 排除元数据标题（版本、变更记录、目录、附录、术语、背景、范围、目标、参考资料、待确认）：**不计入覆盖率分母，但保留在 context 中可见**
4. 按端点形态解析契约源并建立全部端点目录：
   - **HTTP**：`OpenAPI > Spring MVC 注解`；缺 OpenAPI 时按 Spring 路由注解拼接路径、参数绑定与 DTO 字段/校验注解提取。HTTP 客户端（含 Feign）只作为下游调用点，不作契约源
   - **gRPC**：读取仓库 `.proto` 或 descriptor；无 descriptor 且 server reflection 不可用 → `unsupported`
   - **Dubbo 2.7+**：接口类 + 标准注解/XML；接口类必须在仓库源码内，只在依赖 jar 或 Dubbo 2.6 → `unsupported`

   对应形态的契约源全部缺失时 `contract_incomplete=true`、CR 规则为空，报告显式标注；**任何情况下都不猜字段**。C1 不因缺 OpenAPI 中断：业务规则只依赖文档原文；接口事实可由 C3 从代码路由补，缺失字段留给 `contract_incomplete` 显式降级
5. 实例化 `a1_requirement_analysis.md` 为 `prompt.md`（注入文档 section 与端点目录），生成 `rules.schema.json`。**框架内入口 `rules extract`** 直接调用适配层模型产出 `rules.draft.json`；**框架外模式**由 `rules prepare` 只写上下文，模型由 Skill/人工/模型网关/离线批处理执行，产出同一份 `rules.draft.json`
6. `rules extract`/`rules import` 用同一套校验：schema/大小/路径安全；`rules/questions/gaps/conflicts` 四数组分离；`rule_id` 连续；`source.doc/section` 必须可解析到 context；`authority` 显式；`operator=in/not_in` 必须显式声明 `exhaustive`（未声明记 warning，枚举是否闭合并不能靠猜）；`text` 中的数字/枚举做 A 级落地检查（不通过降级 B）；重复 `rule_key` 拒绝
7. 确定性提取 `CR-NN` 契约规则（required/type/enum/min/max/format），与 BR 合并进 `RuleSet`
8. 按 `rule_key` 与当前 `ruleset.json` diff，计算 `rule_drift` / `rule_orphaned`，写新版本并渲染 `ruleset.md`；校验失败不改动现有 ruleset

**关键设计决策**：

- **提示词即契约**：`prompts/a1_requirement_analysis.md` 是 A1 的分析提示词；其输出字段（`id/text/type/entities/fields/source/authority/impact/priority/reasoning` + `questions/gaps/conflicts`）与 3.1.1 对齐，导入时 `id` → `rule_id`。`source.section` 按 context 的稳定 section 路径解释（可附原文章节号）
- **四数组按"能不能只靠文档内部判定"分流**：`rules` 文档中成立且可判定；`conflicts` 文档自相矛盾，当场成立、不需要人回答；`questions` 答案只能来自业务决策或外部事实；`gaps` 文档没写。内部可判的矛盾不得降级为 `questions`——否则一个已成立的缺陷会被排进待确认队列，往往等不到答案
- **冲突带类型与后果**：`conflicts.kind`（semantic/cross_section/value/enumeration_closed/unreachable/bidirectional/self_trigger）决定处置路径，`conflicts.severity`（loss/silent/rights/untestable/minor）决定排序；两者都不作为结论，只作为证据。冲突身份 = `topic + kind`，同一 topic 下的不同毛病不会被合并成一条
- **`output_mode` 只切后端**：`json` 供框架消费，`md` 供人工评审；判据、检查步骤、铁律共用一套，避免两份提示词各自漂移。`md` 输出不进规则库，入库须重跑 `json` 模式
- **人读版由代码渲染，不让模型生成第二份**：`rules render` 从 `ruleset.json` 渲染 Markdown（规则表 / 冲突 / 缺口 / 隐含假设 / 确认清单 / 枚举未闭合项）。同一份数据两个视图，条目与数字必然一致；版式要改也不必重跑模型。若让模型同时输出 JSON 与 Markdown，两次生成的条目数会对不上，且无法判断哪份可信
- **评审文档驱动需求修复，规则库不接受手工修改**：`ruleset.md` 是给产品/需求方的沟通材料，用来把冲突、缺口、隐含假设和待确认问题变成需求文档的具体修订；修复后重新 `rules extract`（框架外模式为 `prepare`/`import`）。每次抽取都是完整快照，`questions/gaps/conflicts` 按本次结果整体替换，上一版已修复的条目不会残留。`ruleset.json` 只能由 `rules extract`/`rules import` 写入，不提供编辑命令；要改规则必须改需求原文后重抽，避免规则库变成第二个人工维护的真相源。通过确定性校验（quote 落地、A 级降级、冲突记录）的规则即可直接被 A4 用于生成用例
- **需求文本是数据，不是指令**：`prompt.md` 明确要求忽略文档中改变角色、输出格式或要求自动批准的内容
- **不臆造**：文档没写清的约束只能进 `questions`，抽不出的只能进 `gaps`；规则 `text` 不得出现原文没有的具体数字/枚举（A 级落地校验）
- **契约不是只有 OpenAPI**：HTTP 契约按 `OpenAPI > Spring MVC 注解 > 设计文档` 取值；契约源全部缺失时 CR 规则为空，C4 不得生成无依据的契约断言用例
- **HTTP 客户端不作契约源**：`@FeignClient` 等 HTTP 客户端声明只是消费方，不新增端点、不生成专用解析路径；其方法签名可作下游调用的定位线索，但契约仍按 `OpenAPI > Spring MVC 注解 > 设计文档` 取，解析不出的部分标 `inferred`，不猜
- **三种协议按形态取契约**：HTTP 用 OpenAPI/Spring MVC 注解，gRPC 用 proto/descriptor，Dubbo 用 2.7+ 接口 + 标准注解/XML；不跨形态套用（例如不能拿 proto 当 HTTP 契约）
- **Dubbo 识别零 LLM**：只认全限定注解与 XML，接口类必须源码可见；2.6、jar-only、动态代理/反射/`getBean` 标 `unsupported`，不猜、不生成用例
- **LLM 只做分析，不做审批**：规则不单独审批；规则被 approved 用例引用后才算确认；C/D 规则不得单独作 oracle
- **规则与上下文分离**：`context.json` 可确定性重建；`ruleset.json` 是版本化产物，记录 prompt 版本与 model，允许同一输入重跑产生差异，由人工审核用例收敛
- **上下文过期**：文档或契约源 hash 变化后，`rules import` 拒绝使用旧 context，要求重新 prepare

**异常与边界**：扫描件 PDF 不支持（前置要求可提取文本）；标题变化会使旧 section 路径与 `rule_key` 失效，需要重新 prepare/import；上下文过期、路径越界、文件/规则/字段超限一律拒绝；模型输出非 JSON 或超出规则规模上限直接拒绝，不截断后继续；对应形态的契约源全部缺失时 CR 为空，明确标 `contract_incomplete`，不退化成猜字段；gRPC 无 descriptor/reflection、Dubbo 2.6/jar-only/动态代理一律标 `unsupported`，不生成契约断言用例

**门禁**：见 3.1.6。C1 自身保证：ruleset 可确定性加载；`questions/gaps` 不混入 rules；每条规则来源可解析；A 级数字/枚举有原文依据；规则 diff 可审计；每个被支持端点至少一种契约源，否则 CR 为空并在报告中阻断“契约断言用例”生成；`unsupported` 端点单列

---

### C2 变更感知

| 项  | 内容                                 |
| -- | ---------------------------------- |
| 职责 | 从代码库变更分支精确推导受影响的**提供方**接口列表，并识别纯契约变更 |
| 输入 | 仓库路径、base 分支、target 分支、base/target 契约源（OpenAPI、`.proto`/descriptor、Dubbo 接口源码）、代码图谱（可现场构建） |
| 输出 | `changed_apis.json`：变更接口列表；`contract_diff.json`：契约差异明细 |

**数据契约**：

```json
{
  "transport": "http | grpc | dubbo",
  "transport_class": "http | grpc | dubbo",
  "transport_evidence": "verified | inferred",
  "endpoint": "POST /api/orders",
  "method": "POST", "path": "/api/orders",
  "handler": "order_service.create_order",
  "business_key": "com.acme.OrderServiceImpl#create",
  "business_key_evidence": "verified | inferred",
  "change_kinds": ["code", "contract"],
  "confidence": "high | medium | low",
  "changed_symbols": ["create_order", "calculate_total"],
  "change_files": ["services/order_service.py"],
  "contract_change": {
    "source": "openapi | spring_mvc | grpc_proto | dubbo_interface",
    "base_ref": "main@abc123",
    "target_ref": "feature@def456",
    "status": "changed | added | removed | unchanged | unavailable",
    "details": ["request.qty: required true→false", "response.status: type string→integer"]
  },
  "external_dependency_change": false,
  "trace": "calculate_total ← create_order ← POST /api/orders",
  "unsupported_reason": null
}
```

> HTTP 端点保留 `method/path`；gRPC / Dubbo 端点只填 `endpoint`，不伪造 HTTP 字段。
> `change_kinds` 可同时含多个值：`code`（实现代码变更）、`contract`（契约文本变更）、`config`（仅配置/依赖版本变更）。纯契约变更时 `changed_symbols` 可为空，但 `contract_change.status=changed`。
> `contract_change.status=unavailable` 表示 base 或 target 契约无法解析，必须显式报出，不得当作“无变更”。
> `unsupported_reason` 取值：`dubbo_2_6` / `interface_in_jar` / `dynamic_lookup` / `no_descriptor` / `streaming` / `unsupported_serialization` / `language_unsupported` / `implementation_missing` / `external_contract_out_of_scope`。

**核心逻辑**：

1. `git diff base..target` 取变更文件与函数级变更符号（AST diff）
2. 代码图谱沿调用链**反向追溯**至端点层（追溯深度可配，默认 5）
3. 端点发现与匹配（框架插件化），**只收提供方**：
   - HTTP：FastAPI/Flask/Django/Spring 路由注册表；HTTP 客户端（RestTemplate、WebClient、Feign 等）是消费方，不是端点，归 C3 `downstream_deps`
   - gRPC：仓库 `.proto`/descriptor 中 **service 实现注册**的 service/method；仅生成 stub 的消费方不算
   - Dubbo 2.7+：`@DubboService`/`dubbo:service`（提供方）建端点；`@DubboReference`/`dubbo:reference`（消费方）只作为下游依赖
   - 2.6、接口只在依赖 jar、动态代理/反射/`getBean` → 标 `unsupported`，不进下游，不静默
4. **追溯范围限定在被测仓库**：`git diff` 之外的仓库、jar 内实现、外部服务均不追溯。变更仅涉及依赖版本时，标 `external_dependency_change=true`，不生成该外部端点的用例；若仓库内消费方接口签名随之变化，按消费方处理，不作为被测接口
5. **配置/依赖变更**：仅配置项或依赖版本变化时，若能静态绑定到某个端点/下游客户端，按 `change_kinds=config` + 低置信度列出；绑定不到端点则只报告 `external_dependency_change` / 配置变更清单，不生成用例。配置变更不得把全部端点都标为受影响
6. **契约 diff**（与代码 diff 并列，解决纯契约变更缺口）：
   - 分别解析 base 与 target 的契约源（HTTP：OpenAPI/Spring MVC 注解；gRPC：`.proto`/descriptor；Dubbo：接口源码 + 标准注解/XML）
   - 按端点身份对齐后比较：新增/删除端点、方法/路径、参数名与类型、必填性、请求/响应字段、状态码/错误定义
   - 差异命中某端点 → `change_kinds` 追加 `contract`；端点被删除 → `contract_change.status=removed`，只报告不生成用例
   - 契约新增端点但仓库内**没有对应提供方实现**（只有 stub/接口声明）→ `unsupported_reason=implementation_missing`，只报告不生成用例，不把消费方 stub 当实现
   - base 契约缺失/解析失败 → `status=unavailable`，报告 `contract_diff_unavailable` 警告，退回纯代码 diff，不假装“契约未变”
7. 置信度判定：
   - 高：变更符号即路由处理函数，或契约 diff 命中该端点
   - 中：路由调用的服务/数据访问层变更
   - 低：仅配置、工具类、或被多个路由共享的公共函数变更
8. 语言不支持 → 该变更符号标记 `unsupported`，从变更集剔除，报告显式列出（不降级兜底）
9. 同一实现暴露多协议时，按 `business_key` 归组（角色在 C3 结合画像与 `adapter_fingerprint` 确定，见 3.3「端点覆盖角色」）；端点本身全部保留，不按方法名合并

**实现进展（2026-10-27，以 `engine/` 为准）**：

- 第 1 步已扩展为「Python AST + CodeGraph 文件符号表」：非 Python 文件用
  `getNodesInFile` 的 `startLine/endLine` 做行区间相交，框架内不另写 Java 解析器；
- 第 2 步把 CodeGraph 节点 id 传给 `impact(node_id=...)`，避免 Java 同名重载合并；
- 第 3 步的 Spring 路由由 CodeGraph 原生 Java resolver 提供；AstSight 尚未实现
  Spring 路由插件第二来源，因此 Spring 路由证据为 `inferred`；
- 类级/文件级/纯删除变更按 API 级兜底并标 `inferred`，不假装方法级精度；
- **未完成**：`low` 置信度隔离、20 个真实分支的召回率/误报率基准、Spring
  真实仓库路由覆盖分母。

**关键设计决策**：

- **低置信度不进入自动用例生成**，只在报告列出供人工勾选——噪音隔离
- `trace` 字段必须保留完整追溯链，供人工判断影响真实性
- Java/Spring 路由识别精度仍是已知风险：CodeGraph 1.6.2 的类级 `@RequestMapping`
  拼接已在合成项目验证，但 Spring 路由只有单源（无 AstSight 插件交叉校验），
  真实仓库覆盖率仍需按 20 个变更分支做基准后再定全量启用
- **C2 仍零 LLM**：Dubbo/gRPC 端点识别按全限定注解、XML、proto 确定性提取；不引入模型猜接口，否则变更感知不可复现
- **非 HTTP 端点不伪装成 HTTP**：gRPC/Dubbo 只产出 `transport + endpoint`，`method/path` 留空；下游消费者按 `transport` 分派
- **只测提供方**：消费方（HTTP 客户端、`@DubboReference`、gRPC stub）不是被测接口；把它当端点会测到别人的服务，且无法从本仓库追溯其实现
- **不追溯仓库外变更**：依赖 jar、外部仓库、注册中心里的实现不在 `git diff` 内，不做猜测性追溯；需要覆盖时由用户显式指定外部契约
- **纯契约变更必须走 diff**：只改 `.proto`、OpenAPI、接口签名而不改实现时，代码调用链是断的；不解析 base/target 契约就会漏测
- **契约解析失败必须显式降级**：`contract_diff_unavailable` 是警告而非“无变更”，报告需说明本次只做了代码 diff

**异常与边界**：分支不存在/无差异 → 明确报错退出；rename/移动文件按内容相似度归并；不支持的 Dubbo/gRPC 端点写入 `unsupported_reason` 并在报告单列，不参与用例生成；端点被删除只报告不生成用例；外部依赖变更写入 `external_dependency_change`，不计入本次覆盖

**门禁**：变更接口召回率 ≥90%、误报率 ≤30%（人工标注 20 个真实变更分支验证）；HTTP/gRPC/Dubbo 分形态统计召回率；**纯契约变更分支必须能产出端点**，否则该项为 0 分；`unsupported`、`removed`、`external_dependency_change` 端点必须显式列出，不得计入已覆盖

---

### C3 接口画像

| 项  | 内容                                              |
| -- | ----------------------------------------------- |
| 职责 | 为每个变更接口建立唯一事实源档案                                |
| 输入 | ChangedAPI[]、契约源（OpenAPI / Spring MVC 注解 / gRPC proto/descriptor / Dubbo 2.7+ 接口）、DDL/库直连、需求上下文 `context.json`、术语表 |
| 输出 | `api_profiles.json`                             |

**画像数据契约**：

```json
{
  "transport": "http",
  "transport_class": "http",
  "transport_evidence": "verified",
  "endpoint": "POST /api/orders",
  "business_key": "com.acme.OrderServiceImpl#create",
  "business_key_evidence": "verified",
  "coverage_role": "primary",
  "alias_of": null,
  "adapter_fingerprint": "sha256:...",
  "contract_source": {"kind": "openapi | spring_mvc | grpc_proto | dubbo_interface | mixed", "files": ["openapi.json"], "evidence": "verified"},
  "params": [{
    "name": "qty", "in": "body", "type": "integer", "required": true,
    "constraints": {"min": 1, "max": 999},
    "constraint_source": "design_doc",
    "db_mapping": {"table": "orders", "column": "qty", "db_type": "int(11)"},
    "biz_meaning": "购买数量",
    "requirement_refs": [{"doc": "requirements.md", "section": "订单 > 创建", "quote": "购买数量不得超过库存"}]
  }],
  "responses": {"200": {"fields": ["order_no", "status"]}},
  "dependencies": {"produces": ["order_no"], "consumes": ["sku_id"]},
  "downstream_deps": [{
    "id": "http:svc:pay-service POST /gateway/pay",
    "type": "http | grpc | dubbo | mq | cache | third_party_api | storage",
    "call_sites": ["com.acme.order.service.OrderService#create"],
    "service": "pay-service",
    "endpoint": "POST /gateway/pay",
    "request_type": "com.acme.pay.dto.PayRequest",
    "response_type": "com.acme.pay.dto.PayResult",
    "contract_source": "openapi | doc | inferred",
    "target": "http://pay-service/gateway/pay",
    "trigger": "支付提交时",
    "side_effect": {"has": true, "desc": "真实扣款"},
    "control": "mock",
    "mock": {"strategy": "url_override | discovery_override | http_proxy | unsupported", "property": "SUT 中实际被覆盖的配置键", "route_key": "r1a2b3c4d5e6f", "injection": {"kind": "base_url", "original": "http://pay-service", "injected": "http://127.0.0.1:41001/_dep/r1a2b3c4d5e6f"}, "branches": ["success", "timeout"]},
    "mock_response": {"success": true, "trans_no": "T{run_scope}_{case_id}_PAY"},
    "fallback": "none | sentinel | hystrix | custom",
    "evidence": "verified"
  }],
  "conflicts": [{"field": "qty", "contract": "无上限", "design": "≤999", "resolved": 999}]
}
```

**按 transport 的请求/响应字段**：

| transport | 必填字段 | 留空 | 约束 |
|---|---|---|---|
| `http` | `method`、`path`、`params`、`responses` | — | 端点标识 `http:{METHOD} {path}`；**下游** `id` 另加下游身份：`http:{downstream_key} {METHOD} {path}`，同一下游端点的多个调用点合并为同一个 `dep_id`（见 C3.5.5） |
| `grpc` | `service`、`method`、`grpc_path`、`request_type`、`response_type`、`descriptor_source`、`streaming` | `path`、HTTP 语义的 `params`/`responses` | 仅 `streaming="unary"`；`grpc_path` 固定 `/{package}.{Service}/{Method}`；`request_type`/`response_type` 必须是 proto 中存在的 message 全限定名 |
| `dubbo` | `interface`、`method`、`param_types`、`return_type`、`serialization`、`registry` 或 `direct_url` | `path`、HTTP 语义的 `params`/`responses` | `interface` 为 FQN 且必须在仓库源码内；`param_types` 按方法声明顺序，用于重载消歧；`serialization` 固定 `hessian2`（本版本） |

gRPC 附加字段：

```json
{
  "service": "acme.order.v1.OrderService",
  "method": "CreateOrder",
  "grpc_path": "/acme.order.v1.OrderService/CreateOrder",
  "request_type": "acme.order.v1.CreateOrderRequest",
  "response_type": "acme.order.v1.CreateOrderResponse",
  "descriptor_source": {"kind": "proto | descriptor_set", "file": "proto/order.proto", "package": "acme.order.v1"},
  "streaming": "unary"
}
```

Dubbo 附加字段：

```json
{
  "interface": "com.acme.order.api.OrderService",
  "method": "createOrder",
  "param_types": ["com.acme.order.api.CreateOrderRequest"],
  "return_type": "com.acme.order.api.CreateOrderResult",
  "group": "order",
  "version": "1.0.0",
  "serialization": "hessian2",
  "registry": {"type": "nacos | zookeeper | direct", "address": "nacos://..."},
  "direct_url": null,
  "timeout_ms": 3000,
  "attachments": {}
}
```

> `group`/`version` 非空即视为依赖 Dubbo 注册中心语义，禁止路由到 gRPC；`transport=grpc` 时二者必须为空。`transport_evidence` 只在判定来自推断时写 `inferred`（见 3.3），不得用 `inferred` 掩盖 `unsupported`。

**多协议端点归组与覆盖角色**：

- `business_key`：从端点实现（controller / gRPC service impl / Dubbo impl）沿调用链找到**共享业务实现方法**的 FQN，如 `com.acme.OrderServiceImpl#create`。找不到共享实现或存在分支实现时，退化为 `endpoint` 本身并标 `business_key_evidence="inferred"`，**不强行归组**。
- `coverage_role`：
  - 组内只有一个端点 → `primary`，`alias_of=null`；
  - 组内多个端点 → 按 `transport_priority`（未配置默认 `http > grpc > dubbo`）选 `primary`，其余为 `alias`，`alias_of` 写 primary 的 `endpoint`；
  - 若同组端点的请求/响应模型、序列化、错误映射、参数校验位置差异过大（`adapter_fingerprint` 不同且差异项超过阈值），该端点升为 `independent`，跑全量；
  - Dubbo 的 `group`/`version` 区分的是不同服务键，**不参与 alias 归组**，各自独立覆盖。
- `adapter_fingerprint`：对 `transport`、请求模型、响应模型、序列化、错误映射（状态码/异常类型）、鉴权/拦截器、参数校验位置做 canonical hash。相同 → alias 可标 `equivalent` 并在 `--transport-scope=all` 下显式跳过；不同 → 至少跑 transport 契约冒烟。指纹算不出时按冒烟处理，不得直接跳过。
- 角色在 C3 确定，C4 只消费；`primary` 由配置覆盖时记录 `primary_inferred` / `primary_override`，报告可追溯。

**核心逻辑**：

1. **transport 判定**（确定性，零 LLM）：按 3.3 矩阵从端点证据判定 `http | grpc | dubbo`。Dubbo 家族中满足「Dubbo 3 + Triple + protobuf + 可用 descriptor + 无 group/version」→ `grpc`；否则 → `dubbo`（sidecar）。证据只够判定“属于 Dubbo 家族”但不足以区分时 → `dubbo` + `transport_evidence="inferred"`。无任何可用调用路径 → `unsupported` + `unsupported_reason`，不生成画像
2. 契约提取（按 transport 分派）：
   - **HTTP**：按 `OpenAPI > Spring MVC 注解 > 设计文档` 取值。OpenAPI 缺失时从代码 AST 提取（框架插件），只认提供方路由注解；HTTP 客户端声明（含 Feign）不参与契约提取。全部契约源缺失时标 `contract_incomplete`，不猜字段；C3 仍产出 method/path/handler 画像，流程不因缺契约中断
   - **gRPC**：从仓库 `.proto` / descriptor 提取 service/method 与 message 字段、类型、`repeated/optional`。无 descriptor 且 server reflection 不可用 → `unsupported`（`no_descriptor`），不靠猜
   - **Dubbo**：接口源码 + `@DubboService`/`@DubboReference`（全限定名）或 XML `dubbo:service`/`dubbo:reference` 提取 `interface/method/param_types/return_type/group/version`；接口只在依赖 jar 或动态代理/反射/`getBean` → `unsupported`（`interface_in_jar` / `dynamic_lookup`）。Dubbo 2.6 → `unsupported`（`dubbo_2_6`）
3. DB 映射：字段名匹配 + 命名约定（驼峰/下划线归一）+ 设计文档表映射章节
4. 三路约束校验：契约源 / DB DDL / 需求原文。契约源与 DDL 的确定性约束取最严；需求原文只作为审核依据，不自动合并，冲突记入 conflicts 交人工确认
5. 依赖标注：写操作响应中的业务标识（`*_no`、`*_id`、`*_code`）标为 produces；入参中引用外部标识的标 consumes
6. **下游依赖识别**：从代码图谱沿调用链**正向追溯**（路由→服务→外部客户端），识别所有出站 HTTP 调用点（RestTemplate、WebClient、Feign、自封装客户端等一视同仁）、gRPC stub、Dubbo 引用（`@DubboReference`/XML）、MQ 生产者、Redis/缓存、第三方 SDK，标注类型（副作用判定已降级为可选的安全护栏标记，见下文）。HTTP 下游统一 `type="http"`，不按客户端框架分类、不新增 transport；`id` 取 `http:{downstream_key} {METHOD} {path}`（如 `http:svc:pay-service POST /gateway/pay`）：`downstream_key` 按 `svc:`（逻辑服务名）> `cfg:`（base URL 配置键）> `host:`（解析后的主机，回环地址带端口）> `call:`（调用点）取值，`path` 做占位符规范化；同一下游端点的多个调用点**合并为一个 `dep_id`**，不按调用点拆分；两个不同目标归一后仍撞 id 时追加 `~<target hash>` 并记 `dep_collision_resolved`，仍冲突则 `dep_collision` 阻断；方法+地址都无法静态确定时标 `unidentified:<call-site id>`，进入缺口清单。完整唯一性规则见 **C3.5.5**
7. **下游 Mock 边界（v4.3）**：SUT → HTTP 与 Dubbo 2.7+ 下游可 Mock（Dubbo 标 `mock.strategy="agent_filter"`，需执行期 agent 自检）；SUT → gRPC 下游仍 `control="observe"`、`mock="unsupported"`、`evidence="verified"`，依赖它的用例必须显式排除或标 `blocked` 并在报告单列（v4.4 起改为 gripmock 假服务器，见 C3.5.4）。测试环境（注册中心/provider）不由框架保证，`No provider available` 报 `env_error`
8. **DB 读写血缘**：从代码图谱沿调用链正向追溯，识别接口对数据库的读/写访问，产出"接口→服务→ORM/SQL→表→列"的血缘链（详见下方"DB 读写血缘"）
9. 业务语义附着：术语表 + 需求 section/quote + 已审核用例中的业务假设 + DB comment
10. **多协议归组与角色**：按实现方法回溯 `business_key`；同组按 `transport_priority` 选 `primary`，其余计算 `adapter_fingerprint` 后定 `alias` 或 `independent`。禁止按方法名直接合并端点——不同 transport 的序列化、错误映射、字段命名都可能不同

**DB 读写血缘（接口对数据库的访问关系，C5 造数的前置输入）**：

> 画像不仅要知道"下游有哪些外部服务"，还要知道"这个接口读了/写了哪些表、哪些列"——这是 C5 造数（`fk_links` 闭包、拓扑排序）的准确依据。本能力由代码图谱正向追溯 + DDL 匹配确定性完成，零 LLM。

| 血缘边 | 含义 | 来源 | 证据等级 |
|---|---|---|---|
| `READS_TABLE` / `READS_COLUMN` | 接口路径上的 SELECT 访问 | AST 提取 SQL/ORM 读 | `verified` |
| `WRITES_TABLE` / `WRITES_COLUMN` | 接口路径上的 INSERT/UPDATE/DELETE/`.add()`/`.save()` | AST 提取 SQL/ORM 写 | `verified` |
| `MAPS_TO_TABLE` / `MAPS_TO_COLUMN` | ORM 模型字段 → 物理表列 | 框架模型映射（SQLAlchemy/Django ORM 等） | `verified` |
| `FOREIGN_KEY_TO` | 表间外键关系 | DDL 解析 | `verified` |

**血缘链示例**（与下游依赖识别同向，供 C5 直接消费）：

```
POST /orders → OrderService.create_order → OrderRepository.insert_order
  → WRITES_TABLE: orders
  → FOREIGN_KEY_TO: orders.user_id → users.id
  → FOREIGN_KEY_TO: orders.sku_id  → products.sku_id
```

**关键设计决策（血缘）**：

- 血缘是 C5 造数的**事实源输入**：C5 的 `fk_links` 直接由 `FOREIGN_KEY_TO` 生成，不再依赖字段名启发式
- **证据分级**：ORM/SQL 能静态证明的标 `verified`；动态 SQL（字符串拼接、`getattr` 表名）标 `inferred`，推不出的标 `unmapped`——**不猜测**
- 与 C2/C3 下游识别共用同一条正向追溯链路，不重复建图

**证据等级（全局口径，对齐 CodeGraph 诚实标记）**：

| 等级 | 含义 | 示例 |
|---|---|---|
| `verified` | AST/DDL 静态可证明 | 直接 ORM 调用、字面量 SQL |
| `inferred` | 推断但未证实 | 命名约定补推、动态 SQL |
| `unmapped` | 无法判定 | 反射、逻辑外键缺失 |

> 全框架所有画像/血缘字段统一采用三级证据等级，取代分散的 `constraint_source`/`inferred`/`unmapped` 混杂表达；`conflicts` 仍独立记录多源矛盾。

**术语定义：副作用（side_effect）——安全护栏标记，非策略选择依据**：

> AstSight采用**全 Mock**（读写下、有副作用与否，一律 Mock，不透传），因此"副作用"不再用于决定"要不要 Mock"。它退化为**安全护栏标记**：决定该下游要求多强的出站隔离，以及漏出时的告警优先级。

**降级后的定位**：

| 原用途（已废弃） | 新用途（保留） |
|---|---|
| 决定下游"要不要 Mock" | ❌ 不需要了——全 Mock，读/写都 Mock |
| 决定 Mock 安全等级 | ❌ 不需要了——无透传场景 |
| **出站隔离与漏出告警优先级** | ✅ 保留——`side_effect.has=true` 的下游要求网络出站隔离，且漏出告警优先级最高 |

**字段语义（收敛后）**：

| side_effect.has | 含义 | 作用 |
|---|---|---|
| `true` | 该下游有真实业务影响（扣款/发消息等） | 要求**网络出站隔离**；会话证据出现该 dep 的未授权/晚到调用时最高优先级告警 |
| `false` | 只读/无业务影响 | 仍要求 Mock；漏出危害小，但同样计入 `egress_guard` 评估 |

**关键结论**：

1. **C3 下游识别不再需要精确判定"读/写"**——只需"识别出所有下游"这一个动作，副作用判定从"必须"降为"可选增强"
2. **`side_effect` 字段保留但语义改变**：从"策略选择输入"变为"安全护栏标记"，服务于"出站隔离"而非"Mock 决策"
3. **应用层无法单独保证"零透传"**：执行层只能看到**到达 Mock 的请求**；配置错误、硬编码 URL、自定义代理导致的请求根本不会出现在 Mock 侧。强保证来自**网络出站隔离**（测试环境无法路由到真实下游），执行层负责会话内未授权/晚到调用的检测与告警。有隔离报 `egress_guard=network`，无隔离报 `egress_guard=none` + `degraded`

> 这符合测试领域的一贯原则：**资损防控的护栏不因"正常流程不需要"而省略**。全 Mock 是正常态，但护栏要防的是异常态；而"护栏"必须是网络层，不是应用层日志。

**字段口径**：`side_effect.has` 为布尔（`true`=有真实业务影响），`desc` 为人工可读说明。因已降级为护栏标记，判定精度要求降低——仅需标出"高危写操作"（扣款/发消息/发货等资损类），其余可标 `false` 或省略；无法判定的标 `unknown` 亦不影响主流程。

**关键设计决策**：

- 画像聚合器是**纯确定性逻辑，不放 LLM**——它是下游一切的事实源，错一处全盘错
- 术语表是独立资产文件，可持续积累，跨项目复用
- 下游依赖识别与 C2 的变更追溯**方向相反**：C2 是"谁影响这个接口"（反向），这里是"这个接口影响谁"（正向）
- **HTTP 客户端一律按普通出站调用处理**：不按客户端框架分支，不对任何客户端（含 Feign）做专用解析或专用配置生成。识别目标只有三件事：调用点、目标地址/端口、可覆盖的配置或注入点；识别不出就标 `unidentified`/`unsupported`，不做尽力而为的兜底
- **下游契约不取自客户端声明**：下游契约按 `OpenAPI > 接口文档 > 运行时观测` 取值；客户端接口的 DTO/校验注解只作线索，标 `inferred`，不作为 oracle
- **下游 `dep_id` 与客户端实现解耦**：`dep_id` 由“下游身份 + 规范化方法/路径”生成（`http:{downstream_key} {METHOD} {path}`），不依赖客户端接口简单名、类名或方法名；同一下游端点的多个调用点合并，避免客户端重命名或多调用点制造无谓的身份漂移。唯一性规则见 **C3.5.5**
- **transport 路由只做一次、结果可审计**：C3 依据 3.3 矩阵确定 `transport`，写入 `transport_evidence` 与 `contract_source`；下游执行器不得二次猜测。`grpc → dubbo sidecar` 回退只对 `transport_class="dubbo"` 生效，且必须显式开启（见 3.3）
- **Dubbo 画像只覆盖可静态证明的调用**：`interface` 必须能在仓库源码中定位；接口类型、方法签名、`param_types` 全部来自源码或 XML，不通过运行时注册中心反推。接口在依赖 jar、动态代理、反射或 `getBean` 的一律 `unsupported`，不尝试“尽力而为”
- **gRPC 画像不含 HTTP 语义**：`params`/`responses` 按 proto message 字段建模，不套用 HTTP 状态码；`grpc_status` 是独立断言维度
- **下游按类型分档**：HTTP 与 Dubbo 2.7+ 可 Mock；Dubbo 下游写入 `mock.strategy="agent_filter"` 并记录兼容矩阵，gRPC 下游仍 `mock="unsupported"`，受影响的用例必须显式排除或标 `blocked`，不静默打真实下游（v4.4 起 gRPC 下游标 `mock.strategy="grpc_server"`，见 C3.5.4）
- **多协议不按方法名合并**：`business_key` 只用于归组，端点身份仍是 `transport:endpoint`；`primary/alias/independent` 必须写进画像并可审计。alias 不是“少测”，而是把全量业务场景留在 primary、在 alias 上补 transport 契约冒烟
- **alias 跳过必须有证据**：只有 `adapter_fingerprint` 相同才允许标 `equivalent` 跳过；指纹缺失按冒烟处理。禁止因为“业务逻辑一样”就默认所有协议等价
- **`primary` 选择可配置可追溯**：未配置 `transport_priority` 时按 `http > grpc > dubbo` 推断并标 `primary_inferred`；生产入口明确时应显式配置，报告写出每个 `business_key` 的实际选择依据

**异常与边界**：字段无法映射 DB 时标记 `unmapped` 而非猜测；OpenAPI/Spring MVC 注解全部缺失时标记 `contract_incomplete` 并显式告警（不降级兜底）；下游地址来自运行时拼接、SpEL 表达式、配置中心或动态注册时，`target` 标 `inferred` 并在报告中单列，不静默当作已知；调用点无法静态定位（反射、`getBean`、SDK 内部发起）→ 标 `unidentified`，进入缺口清单，不阻断主链路；gRPC 无 descriptor 且 reflection 关闭 → `unsupported`（`no_descriptor`）；Dubbo 2.6 / 接口在 jar / 动态查找 → `unsupported`（对应 `unsupported_reason`）；任何 `unsupported` 都不得产出可执行画像或静默降级为 HTTP；`business_key` 无法回溯时不归组，避免把不同实现误当 alias

**门禁**：字段约束提取准确率 100%；映射冲突检出率 = 人工基准；下游依赖识别召回率 ≥85%（对照人工梳理的调用清单）；HTTP 下游按“识别成功 / `unidentified`”分列统计，`unidentified` 必须出现在缺口清单，不得静默；HTTP/gRPC/Dubbo 分形态统计端点召回与 `unsupported` 清单，`unsupported` 不得计入已覆盖；**多协议组必须 100% 有明确 `coverage_role`**，每个 `business_key` 只能有一个 `primary`，`alias` 必须带 `alias_of` 与 `adapter_fingerprint`（或显式 `fingerprint_unavailable`）

---

### C3.5 下游依赖控制策略

> 被测接口内部的对外调用（下游依赖）是接口测试最易失真的环节。**AstSight的测试数据由数据库直插构造，上下游并未打通**——下游服务里不存在与我们造数匹配的业务上下文（账号、余额、订单状态等），因此连下游和放行都不可行，**Mock 是唯一主策略**。
>
> **范围（v4.6.1）**：Mock 覆盖 SUT → **明文 HTTP** 与 SUT → **Dubbo 2.7+** 下游。HTTP 在出站 HTTP 边界（URL/服务发现/HTTP 代理覆盖 + 会话），首版**不处理 HTTPS**：`https://` 目标及需要 TLS 的代理链路统一标 `mock="unsupported"`（`unsupported_reason="tls_not_supported"`），不降级、不 MITM。Dubbo 用 **agent + ByteBuddy + Filter** 拦截（见 C3.5.3）。SUT → **gRPC** 下游在 C3.5.4 落地前仍标 `mock="unsupported"`，依赖它的用例必须显式排除或标 `blocked`，不允许静默透传。
>
> **HTTP 注入的 v1 承诺**：只承诺**下游专属配置的 `url_override`** 与**显式 `http_proxy`**（均为明文）；`discovery_override` 与透明代理仅条件支持，环境不具备时 `discovery_not_supported`，不计入承诺。共享 base URL、HTTPS、同 JVM bean mock、动态 URL、请求匹配/序列/流式均不支持。完整矩阵与选择顺序见 **C3.5.5**。

**核心结论：Mock 唯一主策略**

| 策略              | AstSight适用性      | 原因                                          |
| --------------- | ----------- | ------------------------------------------- |
| **Mock（打桩）**    | ✅ **唯一主策略** | 出站请求处截断，注入编排好的响应，根本不触达下游，规避"下游查无数据"与"真实副作用污染" |
| Passthrough（透传） | ❌ 不可用       | 下游无对应业务数据 → 假红；有副作用 → 污染真实环境（全 Mock 已杜绝透传）                |
| Sandbox（测试替身）   | ⚠️ 暂不启用     | 假下游同样需要匹配我们的造数，数据对不上；需额外维护替身服务              |
| Contract（契约校验）  | ⚠️ 暂不启用     | 仅作为将来打通上下游后的可选项，当前不做                        |

**Mock 分级（按依赖类型，全部落在 Mock 内）**：

| 下游类型            | Mock 方式                       | 断言重点                 |
| --------------- | ----------------------------- | -------------------- |
| http | 内嵌 HTTP Mock 服务（仅明文），按会话返回编排响应（成功/超时/降级/异常码）；通过通用 URL/服务发现/HTTP 代理覆盖把出站调用指向该服务 | 我方调用参数 + 响应处理结果 + route_key 命中证据 |
| `dubbo`（SUT→下游，2.7+） | **agent + ByteBuddy + Filter**，按会话返回语义分支；`serialization_verified=false` | 我方调用参数 + 响应处理结果 + Filter 命中证据 |
| `grpc`（SUT→下游） | ❌ v4.3 不 Mock | 不执行真实下游：依赖它的用例显式排除或标 `blocked` |
| mq（消息队列）        | 生产者消息拦截，断言消息内容                | 消息是否发出、内容是否正确        |
| cache（Redis 等）  | 测试实例 or 出站拦截                  | 缓存读写行为（可用测试实例，非下游服务） |
| third_party_api | 出站拦截，注入编排响应                   | 调用参数 + 降级/重试处理       |
| storage（对象存储）   | 出站拦截，注入成功/失败响应                | 上传/下载调用正确性           |

**核心机制**：

1. **注入点**：Mock 发生在"被测服务的出站请求"处，按通用 HTTP 机制实现，不区分客户端框架——
   - **配置 URL 覆盖（首选）**：覆盖 SUT 中该出站客户端的 base URL 配置项，指向 `http://<mock_host>:<port>/_dep/<route_key>`。配置键由 C3 从 SUT 配置/启动参数中提取并写入 `mock.property`，不预设框架键名；客户端追加的原路径作为前缀后的余量，不参与路由
   - **服务发现覆盖**：把服务名解析到 Mock 实例（测试注册中心 / 静态服务列表），Mock 按 `Host` 头命中 `route_key`；只对走服务发现的客户端有效
   - **Dubbo Filter（SUT → Dubbo 下游首选）**：agent 经 ByteBuddy 加载 filter jar，Filter 按消费端会话返回；不改代码、不改注册中心
   - **HTTP 出站代理（兜底，仅明文）**：URL 覆盖不可用（硬编码地址、自定义拦截器、动态路由）时，把 SUT 的明文 HTTP 出站指向本地代理：显式代理读 absolute-form 请求行，透明重定向读 origin-form + `Host`；HTTPS 的 `CONNECT` 直接拒绝并标 `tls_not_supported`
   - SDK 注入点 / 依赖注入（框架原生支持时最干净）
   - MQ 消费端不启动、生产者指向测试 topic

   > **禁止在同一 JVM 内 mock 任何 HTTP 客户端 bean**：那会绕过编解码、重试、降级与拦截器链，测出的不是真实调用路径。HTTP 下游 Mock 必须在 HTTP 边界上完成。

   > **通用机制覆盖不了就不支持**：地址在运行时动态生成、客户端不走可覆盖配置且无法经代理到达、或自定义传输实现绕过标准 HTTP 栈 → 该下游标 `mock="unsupported"`，依赖它的用例 `blocked`。不为特定客户端（含 Feign）追加专用兜底。
   >
   > **首版明确不支持 HTTPS**：`https://` 下游不通过“改写成 http”或安装测试 CA 的方式绕过 TLS；标 `mock="unsupported"`、`unsupported_reason="tls_not_supported"`，依赖用例 `blocked` 并进入报告 `unsupported` 清单。是否在后续版本引入 MITM 由单独决策记录决定。
2. **响应编排**：Mock 响应按用例编排（成功/超时/降级/异常码），响应体用数据契约变量（`T{run_scope}_{case_id}_PAY`）保证用例间隔离
3. **调用断言**：Mock 记录"是否被调用、调用几次、请求参数"，供用例断言（如"取消订单后必须发送退款消息"）
4. **Mock 数据与 DB 造数对齐**：Mock 响应中的业务标识（如 `trans_no`）若需回写被测库，须与 DB 造数计划共享同一变量域（`T{run_scope}_{case_id}_` 前缀），保证"我方 DB 状态 ↔ Mock 返回"逻辑自洽
5. **出站隔离与漏出检测**：执行层只能观测到达 Mock 的请求，无法观测"根本没发往 Mock"的透传；因此强保证是**网络出站隔离**（测试环境不可路由到真实下游），并在报告标注 `egress_guard=network|none`。应用层检测范围限于 Mock 会话内的未授权 dep、会话关闭后的晚到调用、以及画像已有但应触发却从未命中的下游（弱信号）。`side_effect.has=true` 的下游（资损类）优先要求隔离，漏出告警优先级最高。HTTP 与 Dubbo 下游都受此约束；gRPC 下游不在 Mock 范围内，只能通过用例排除/`blocked` 保证不执行

**用例级 Mock 会话（方案 A，v4.2 定稿，v4.3 延续）**：

> HTTP URL 覆盖是 SUT 启动期配置，不能按用例改地址；Dubbo agent 同样是进程级加载。因此 **SUT 启动时统一指向 Mock（HTTP）并加载 agent（Dubbo），执行期用“会话”决定当前用例命中哪个分支**。一个 Mock 命名空间同一时刻只服务一个用例，HTTP 与 Dubbo 下游共用同一会话。

| 阶段 | 动作 | 失败处理 |
|---|---|---|
| 用例开始 | `POST /_admin/sessions`，提交 `{session_id, namespace, case_id, bindings:[{dep_id, branch}], ttl_ms}` | 已有 active session → `mock_session_conflict`，阻断 |
| 执行中 | SUT 出站请求先按 `route_key`/Host/`METHOD+path` 解析到 `dep_id`，再命中会话；只接受会话内 binding | 无会话 → `mock_session_missing`；dep 不在 binding → `mock_unexpected_dep`；branch 不存在 → `mock_branch_not_found`；路由未知/歧义 → `mock_route_unknown`/`mock_route_ambiguous` |
| 用例结束 | 先等待 drain（默认 200ms，可配），再 `DELETE /_admin/sessions/{id}` | 关闭后到达 → `mock_late_call`，报告告警 |
| 异常/TTL | fixture `finally` 必须关闭；超过 `ttl_ms` 自动过期 | 自动关闭 → `mock_session_expired` |

- 一个命名空间同一时刻只允许一个 active session；创建是原子的，禁止覆盖已有会话。
- `mock_bindings` 在 import 时补全：有下游但未显式绑定的，按默认 `success` 生成 `binding_source="default"` 并告警；**运行时会话里始终是显式 binding，不存在“未绑定就默认成功”的隐式路径**。
- C7.1 把 `mock:<namespace>` 作为独占资源；C7.2 只有拿到 mock lease 的用例才能执行。**同一命名空间内所有 mock-bound 用例串行**；无 mock 依赖的用例仍可并行，但若其调用到已 Mock 的下游，按 `mock_unexpected_dep` 处理。
- Admin API 只绑 `127.0.0.1`，要求 token，限制请求体大小；不接受任意 URL、文件路径或表达式。
- **实例归属（v4.5）**：`namespace = sut_id`，禁止用字面量 `"default"` 作为多 SUT 环境的路由键；Mock 与 agent admin 的端口按 **5.5** 原子分配，每实例随机 token；建会话前必须校验心跳中的 `sut_id/namespace/admin_endpoint/pid/jvm_start_time` 与本 run 一致，不符即 `mock_identity_mismatch`，不得继续。
- 每个用例记录 `session_id`、命中的 dep/branch、调用次数、未命中/晚到调用，作为执行证据写入报告。
- **不采用**测试专用透传头把 `case_id` 带进 SUT（黑盒不侵入），也不把请求体里的 `T{run_scope}_{case_id}_` 前缀作为主路由（仅可作兜底）。


**关键设计决策**：

- **Mock 覆盖全部可 Mock 的 HTTP 与 Dubbo 2.7+ 下游，无一透传**——这是 AstSight 的前提约束（数据未打通），不是可选优化；gRPC 下游在 v4.3 仍无拦截手段，只能显式排除或 `blocked`，**不能以“已 Mock”记入覆盖**
- **Dubbo 下游用 agent 拦截，不用注册中心替身**：注册中心/provider 由测试环境负责，框架不部署、不伪造；`No provider available` → `env_error` 并阻断，不兜底。agent 只加载 Filter，不增强 Dubbo 内部类，版本耦合最小
- **低侵入要如实声明**：对代码/构建零侵入，但需要向 SUT 注入 agent；报告必须写 `agent_mode` 与 `serialization_verified=false`，不能宣称“零侵入”或“已验证编解码”
- **MQ 类依赖的断言在"消息内容"而非"下游结果"**——消息是否发出、内容是否正确由 Mock 断言，下游消费结果不属于本接口职责范围
- **Mock 响应与 DB 造数共享变量域**——两者必须对齐，否则会出现"Mock 返回的流水号落不了库"这类割裂
- Mock 响应和调用断言都是确定性编排，不放 LLM
- Cache（Redis）属特例：可用独立测试实例而非 Mock，因为它不是"未打通的下游服务"，而是可随测随建的基础设施
- **优先用配置覆盖，而非字节码/代理劫持**：配置覆盖稳定且可审计；只有在覆盖不到（硬编码地址、自定义拦截器、动态路由）时才退回明文 HTTP 代理，并在画像里标 `mock.strategy="http_proxy"`
- **Mock 分支选择走会话，不靠“当前用例隐式上下文”**：一个命名空间单 active session，session 里显式登记 `mock_bindings`；未命中、未授权 dep、晚到调用都必须报错，不能回退到默认成功或真实下游
- **v1 接受 mock-bound 用例串行**：正确性优先；并行需要每 worker 独立 SUT + Mock 命名空间，作为后续增强，不在 v1 假装支持
- **超时/降级/重试必须一起测**：客户端存在 `fallback` / Sentinel / Hystrix 等降级机制时，Mock 分支至少要包含 `success`、`timeout`、`error`；只测成功路径会把"降级吞异常"当成通过（假绿）。若 Mock 响应可延迟，用延迟触发真实超时而非改代码
- **Mock 是否生效要有证据**：每个 HTTP 下游都必须记录"Mock 被调用次数"，用例断言里同时检查"Mock 命中 + fallback 未触发"；仅凭状态码通过不算
- **零透传只能由网络隔离强保证**：应用层看不到未到达 Mock 的请求，报告用 `egress_guard=network|none` 如实标注；`none` 时标 `degraded`，不允许宣称"已验证无透传"

**异常与边界**：下游依赖无法识别（动态调用、反射、配置化 URL）→ 标记 `unidentified` 并在报告告警，交由人工补画像；全 Mock 原则下不存在"透传放行"的正常例外——任何真实出站请求都触发告警（唯一例外：Cache/Redis 等可随测随建的基础设施，不作为"下游服务"处理）；SUT → gRPC 下游仍 `mock="unsupported"`，用例显式排除或 `blocked`，报告单列，不得当作已覆盖（v4.3 口径；v4.4 决策见 C3.5.4）；Dubbo 下游若 agent 未激活或 Filter 未进入调用链 → `agent_not_active`，整批阻断，不得继续执行。HTTP 专项边界：地址在运行时动态生成、配置项不可覆盖或客户端绕过标准 HTTP 栈 → `mock="unsupported"`，不尝试专用兜底；`https://` 目标 → `unsupported`（`tls_not_supported`）；多个调用点命中同一下游端点时**合并为同一 `dep_id`**，运行时共享同一分支；若确实需要按调用点返回不同分支，HTTP 边界无法区分，标 `mock_binding_ambiguous`，不按调用点拆分 `dep_id`（避免制造重复身份）；`fallback` 吞掉异常会导致"假绿"，必须用"Mock 调用记录 + fallback 未触发"双断言识别

**门禁**：全部 HTTP 与 Dubbo 2.7+ 下游 Mock 覆盖率 100%（每个 `type=http|dubbo` 的 downstream_dep 必有 Mock 编排）；存在未 Mock 且未显式声明的下游 → 阻断执行；gRPC 下游必须标 `mock="unsupported"` 且依赖它的用例已显式排除或 `blocked`，否则阻断执行；Dubbo 下游必须通过 agent 自检（心跳 + Filter 激活）且有命中证据，`serialization_verified=false` 必须可见；每个 HTTP 下游必须给出 `mock.strategy`、唯一 `dep_id`、唯一 `route_key` 与实际配置键（或发现/代理覆盖证据），标注 `unsupported` 的下游必须显式声明且依赖用例 `blocked`；`https://` 下游必须标 `tls_not_supported` 且不得执行；`dep_id` 或 `route_key` 冲突、路由歧义 → 阻断画像/执行；只断言状态码、没有 Mock 命中证据的用例 → 标记 `weak_oracle` 并进入人工复核

#### C3.5.1 Mock 语义模型（Mock 响应如何符合用例需求）

> Mock 不是"随便返回一个 200"——Mock 响应必须承载正确的业务语义，否则测出的结果是假的。本节定义 Mock 语义的**三层来源**与**确定性生成规则**。

**问题本质**：Mock = 一个"假的下游"，其行为必须真实可信。语义错了会导致两种失真——① 被测接口对错误语义的响应"正确处理"→ 假绿（没测到真实逻辑）；② 被测接口走了不该走的分支 → 断言失真。

**Mock 语义三层来源**：

| 层        | 来源              | 决定什么                         | 缺失后果             |
| -------- | --------------- | ---------------------------- | ---------------- |
| 契约层（形）   | 下游契约：OpenAPI / Spring MVC 注解 / 接口文档 | 响应**骨架**：字段、类型、枚举、错误码结构 | Mock 字段对不上 → 跑不通 |
| 业务知识层（神） | 需求/设计文档 + 术语表   | **什么条件返回什么**：成功/余额不足/超时等语义分支 | 语义瞎编 → 假绿        |
| 用例意图层（选） | 测试用例要验证什么       | 本次执行**返回哪个语义**               | 语义与用例错位 → 断言失真   |

**关键：三层缺一不可**。契约定"形"、业务知识定"神"、用例意图定"选"。

> 本节适用于 `type=http`与 `type=dubbo`（2.7+）的下游；Dubbo 的契约层从接口源码/共享 api jar 的返回类型提取。`type=grpc` 下游在 v4.3 仍 `mock="unsupported"`，不生成语义模板，也不参与 Mock 覆盖率统计。

**Mock 语义模板（资产，可复用）**：

每个下游依赖维护一份语义模板，枚举其所有业务分支：

```json
{
  "dep_id": "http:svc:pay-service POST /gateway/pay",
  "semantic_branches": [
    {"name": "success",  "condition": "账户余额充足且请求合法",
     "response": {"code": 200, "trans_no": "T{run_scope}_{case_id}_PAY", "status": "paid"}},
    {"name": "insufficient_balance", "condition": "余额不足",
     "response": {"code": 40001, "message": "余额不足"}},
    {"name": "timeout", "condition": "下游超时（网络抖动）",
     "response": {"timeout": true}},
    {"name": "degraded", "condition": "下游降级",
     "response": {"code": 503, "message": "服务降级"}}
  ]
}
```

**语义模板的推导规则（确定性）**：

1. **契约层**：从下游 OpenAPI / Spring MVC 注解 / 接口文档提取响应 schema → 填充每个分支的 response 骨架。客户端接口声明里的 DTO 只作线索，标 `inferred`，不去猜字段
2. **业务知识层**：从需求/设计文档提取该下游的**业务分支规则**（"余额不足返回 40001"这类），映射为 condition + response
3. **语义覆盖完整性校验**：模板必须覆盖被测接口**可能消费到的所有分支**——从被测接口代码的正向追溯中，找出它对下游响应做的所有判断（如 `if resp.code == 40001`），反推出必须 Mock 的分支集合。**漏一个分支，就有一条被测代码路径永远测不到**

**用例 → 语义分支绑定**：

每个用例在生成时**显式声明**它要驱动哪个语义分支：

```json
{
  "case_key": "cancel_order_refund",
  "mock_bindings": {"http:svc:pay-service POST /gateway/pay": "insufficient_balance"}
}
```

- 正向用例绑定 `success`，负向/降级用例绑定对应异常分支
- 无绑定的下游 → 默认 `success`，但必须在 import 时补成 `binding_source="default"` 的显式绑定并告警；运行时不存在“未绑定就默认成功”的隐式回退
- **一条用例可绑定多个下游的不同分支**（下单接口同时 Mock 支付 + 库存两个下游）

**关键设计决策**：

- **语义模板是资产不是一次性配置**——它随下游契约/业务规则演化，需与下游接口文档同步维护（类似术语表）
- **语义分支完整性由"被测接口消费面"反推校验**——这是防止"漏测分支"的唯一可靠手段，比人工枚举更准
- **用例显式绑定语义分支**——禁止"默认返回一个笼统成功"，因为未绑定的语义分支是测试盲区
- Mock 语义推导的**契约层和业务知识层**是确定性提取（文档解析），**语义理解中的复杂分支条件**可用 LLM 辅助提取但需人工复核（此处 LLM 属 C1 文档接入范畴，非运行时）

**异常与边界**：下游无接口文档 → 语义模板无法建形，降级为"从被测接口的消费面反推最小语义集合"（只保证跑通，不保证语义准确，标记 `semantic_inferred`）；被测接口对下游响应存在无法静态识别的判断（动态分支）→ 该下游标记 `semantic_incomplete`，报告提示人工补全

**门禁**：每个下游的语义模板覆盖被测接口全部消费分支（覆盖率 100%）；每条用例显式绑定语义分支（默认成功也须显式声明）

#### C3.5.2 下游信息获取（要不要下游 git）

> Mock 需要下游的"契约 + 业务语义"，不是"实现"。按信息充分度分三档，决定是否需要下游源码。

**信息充分度三档**：

| 档位                  | 需要的输入                         | 能 Mock 到的程度                             |
| ------------------- | ----------------------------- | --------------------------------------- |
| **档 1：只读下游契约**      | 下游 OpenAPI / Spring MVC 注解 / 接口文档（swagger、wiki） | 契约层✅（响应骨架/字段/错误码）；业务语义层⚠️（靠文档描述推断，可能不全） |
| **档 2：加下游 git（只读）** | 下游源码（零侵入静态分析）                 | 契约层✅ + 业务语义层✅（从代码逻辑精确提取"什么条件返回什么"）      |
| **档 3：运行时观测**       | 下游运行环境/流量录制                   | 语义层最准，但成本与侵入性最高，**AstSight不依赖**              |

**关键判断：何时必须给下游 git**：

- 下游 OpenAPI 文档写清了**错误码 + 触发条件**（如"40001 = 余额不足"）→ **档 1 足够，不需 git**
- 下游文档只有字段结构、**没有语义规则**（多数内部服务 swagger 只列 response schema）→ 业务语义层缺失，**需下游 git**，用 CodeGraph 静态分析下游代码，从其判断逻辑反推语义分支
- 下游只提供接口文档或客户端接口声明、没有 OpenAPI → 契约层可由文档 DTO 建响应骨架，仍属档 1；只有需要精确分支条件时才补下游 git。客户端接口里的注释、枚举与校验注解可以作为语义线索，但标 `inferred`，不作为 oracle

**核心结论**：

1. **下游 git 不是"必须依赖"，是"语义精确度的增益"**——能拿到文档就先用文档，语义不全再补源码
2. **即便给下游 git，也是只读静态分析、零侵入**——与被测应用共用同一套 CodeGraph，下游无需安装任何东西、无需运行
3. **下游信息获取是"渐进增强"的**：档 1 先行（跑通），档 2 补语义（跑准），避免一开始就被"要下游代码"卡住落地

**信息缺口标记**：下游语义分支无法从文档或源码获取时，标记 `semantic_inferred`（仅保证跑通）或 `semantic_incomplete`（人工补全），并明确记录"该下游的 Mock 语义未经精确验证"——**不允许在报告里假装语义完整**。

**门禁**：每个下游至少达到档 1（有契约骨架）；业务语义层缺失的下游必须显式标记缺口，不得静默

#### C3.5.3 Dubbo 下游 Mock：agent + ByteBuddy + Filter（v4.3）

> 本节把 v4.2 的“SUT → Dubbo 下游不支持 Mock”改为支持。注入方式是 **Java agent + ByteBuddy（Attach / classloader 注入）+ Dubbo `Filter` SPI**，不部署 Mock Provider、不做注册中心替身。

**边界（先划清）**：

| 项 | 规则 |
|---|---|
| 测试环境 | 注册中心、provider、SUT 部署由环境保证，**不是框架职责** |
| provider 不可用 | `No provider available`、发现超时 → `env_error`，用例 `blocked`，不伪造 provider、不重试兜底 |
| 侵入级别 | 对 SUT 代码/构建零侵入；对运行期需要加载 agent，因此整体定位为**低侵入**，报告必须显式声明 |
| 方向 | agent 只拦截 SUT 的**出站 Dubbo 调用**；调用 SUT 仍走 sidecar/gRPC，不用 agent |
| gRPC 下游 | v4.3 仍 `mock="unsupported"`，不因为 Dubbo 支持而顺带承诺；v4.4 改用 gripmock 独立服务器（C3.5.4），不复用本 agent |

**注入链路**：

```
AstSight 启动 SUT（-javaagent 或 classpath + 配置）
        │
        ▼
astsight-agent：ByteBuddy Agent 加载 → 把 Mock Filter jar 追加进 classloader
        │  Filter 通过 Dubbo SPI @Activate(group=CONSUMER) 自动进入消费端调用链
        ▼
用例开始：获取 mock 租约 → POST /_admin/sessions（bindings 来自 mock_bindings）
        │
        ▼
SUT 调用下游：Cluster/Directory 选中 provider → Filter 命中会话
        │  ├─ 命中 binding → 用 Jackson 按方法返回类型构造对象，返回 Result
        │  ├─ dep 未绑定 → mock_unexpected_dep
        │  └─ branch 不存在 → mock_branch_not_found
        ▼
用例结束：读命中证据 → DELETE session → 释放租约
```

**复用与自研边界**（不自己造字节码底座）：

| 能力 | 选择 |
|---|---|
| Attach / agent 加载 | 复用 **ByteBuddy Agent**（同时支持 `-javaagent` 与运行期 attach） |
| 字节码能力 | 复用 **ByteBuddy**；只在 Filter 覆盖不到时使用，不增强 Dubbo 内部类 |
| 注入点 | 自研 `org.apache.dubbo.rpc.Filter` 实现，走公开 SPI |
| JSON → DTO | 复用 **Jackson**，按 `Invocation` 的接口方法返回类型构造 |
| Admin API | JDK `com.sun.net.httpserver`，只绑 `127.0.0.1`；端口按 5.5 原子分配，每实例随机 token |
| 会话/证据/失败子码 | 自研，复用方案 A 的会话模型 |

> 落选的 Dubbo Mock 实现方式（自带 `mock` 属性、TestableMock、Byteman、jvm-sandbox-repeater、ChaosBlade）及原因见 [`决策记录.md`](决策记录.md) §3。

**Filter 实现约束**：

- 同步与异步必须分别处理：异步调用要返回 `AsyncRpcResult`，不能直接返回裸对象；
- 重载按 `methodName + parameterTypes` 精确匹配，不能只看方法名；
- 泛化调用（`GenericService`）返回 `Map`/`GenericObject`，单独分支；
- 返回对象由 Jackson 按接口方法返回类型构造，构造失败 → `mock_response_invalid`；
- Filter 未生效（应用用 `filter=...` 且带 `-default` 把默认 filter 排除）必须能被检测到，检测不到就阻断整批。

**加载形态（同一模块两种入口）**：

| 形态 | 做法 | 适用 |
|---|---|---|
| `-javaagent`（主推） | SUT 启动参数加 agent，框架自行控制 | AstSight 拉起 SUT 的常规场景 |
| classpath + 配置 | agent jar 进 classpath，配 `dubbo.consumer.filter=astSightMock` + `-Dastsight.mock.heartbeat=<abs path>`（缺则 `agent_not_active`，不猜路径） | 禁止 agent 或已有固定启动脚本的环境 |

**失败子码**：

| 子码 | 含义 | 处理 |
|---|---|---|
| `env_error` | provider 不可用、注册中心不可达、发现超时 | 用例 `blocked`，不计 SUT 缺陷 |
| `agent_not_active` | 心跳文件缺失/残留/过期，或 Filter 未实例化（未进 consumer 链） | 整批阻断，绝不允许继续跑（否则会真实调用下游） |
| `mock_session_missing` / `mock_session_conflict` | 会话生命周期错误 | 阻断，归 `mock_error` |
| `mock_unexpected_dep` / `mock_branch_not_found` | 未绑定依赖 / 分支不存在 | 阻断，归 `mock_error` |
| `mock_response_invalid` | 响应对象构造失败 | 阻断，归 `mock_error` |
| `mock_not_used` | 绑定 dep 在本用例中命中 0 次 | 用例失败（防假绿），不按通过处理 |
| `mock_late_call` / `mock_session_expired` | 关闭后调用 / TTL 过期 | 告警 + `degraded` |

**自检（必须全部通过才执行）**：

1. agent 版本与 SUT 的 Dubbo 版本匹配（2.7.6+ / 3.0+）；
2. 心跳文件存在、`pid/jvm_start_time` 与本次 SUT 匹配、`last_heartbeat_at` 新鲜、`filter_instantiated>0`（Filter 已进入 consumer 链）；
3. 会话命名空间 = 本 run 的 `sut_id`；心跳文件中的 `sut_id/namespace/admin_endpoint/pid/jvm_start_time` 与本 run manifest 全部一致（任一不符 → `mock_identity_mismatch`，整批阻断）；
4. `serialization_verified=false` 写入报告，依赖序列化验证的用例标 `unsupported`。

**兼容矩阵**（指 **agent Mock 的实测矩阵**，不是“支持 2.7+”这一句）：

- **支持边界**：Dubbo 2.7.6+（含 3.x）。低于 2.7.6 直接 `unsupported`，不尝试适配。
- **矩阵维度**：`Dubbo 版本 × JDK 版本 × 调用形态 × DTO 形态`，每个格子是一组实测结论（`verified` / `unsupported`），由 agent 启动自检上报，落到 `MockAgent.supported_matrix` 并随报告出报。

| 维度 | 取值 |
|---|---|
| Dubbo | 2.7.6 / 2.7.22 / 3.0.x / 3.2.x |
| JDK | 8 / 11 / 17 / 21 |
| 调用形态 | 同步 / 异步（`AsyncRpcResult`）/ `oneway` |
| DTO 形态 | typed DTO / 泛化调用（`GenericService`）/ 重载方法 |

- **判定口径**：“在 2.7+ 范围内”≠“已验证”：只有矩阵内且实测通过的组合才允许绑定；矩阵外组合在 A4.5/C4 阶段就标 `unsupported`，不生成 Mock 绑定，也不在运行期试探。新增组合先补实测再写进矩阵。

**`agent_not_active` 探测：本地心跳文件（定稿）**

- **路径由框架指定**，不由 agent 猜：`-javaagent:astsight-agent.jar=heartbeat=<abs path>`；classpath 形态用 `-Dastsight.mock.heartbeat=<abs path>`，**缺该属性直接报 `agent_not_active`，不回退到 `user.dir` 默认路径**（路径不明确时回落容易读到错误目录的文件）；两种形态写同一套文件协议。路径必须含 `sut_id`（如 `/tmp/astsight/<sut_id>.heartbeat.json`），多 SUT 环境不得共用同一路径；启动前只清理本实例路径（见 5.5）。路径随报告出报。
- **文件内容**（原子写：临时文件 + rename）：`agent_version / mode / sut_id / namespace / admin_endpoint / token_fingerprint / pid / jvm_start_time / dubbo_version / filter_instantiated（按 consumer reference 计数）/ filter_hits / supported_matrix / last_heartbeat_at / state`。
- **写入时机**：agent 启动即写；每个 consumer reference 建立、Filter 被 SPI 实例化时更新 `filter_instantiated`；daemon 线程按固定间隔（如 1s）刷新 `last_heartbeat_at`。
- **读取与判定**（SUT 启动 fixture 轮询，超时报 `agent_not_active`；启动前先删旧文件）：

| 观测 | 结论 |
|---|---|
| classpath 形态未设置 `-Dastsight.mock.heartbeat` | 路径未知 → `agent_not_active`，不回退默认路径 |
| 清理后启动仍无文件 | agent 未加载 |
| 文件存在但 `pid` / `jvm_start_time` 与本次 SUT 不符 | 读到上次运行残留 → 按未加载处理 |
| 文件存在但 `sut_id` / `namespace` / `admin_endpoint` 与本 run manifest 不符 | 读到其他 SUT 实例的心跳 → `mock_identity_mismatch`，整批阻断 |
| `last_heartbeat_at` 超过 2 个刷新周期未更新 | agent 挂起或已退出 |
| 文件正常但 `filter_instantiated=0` 且 SUT 已建立 consumer reference | Filter 被 `filter=...,-default` 排除或 SPI 未扫到 → `filter_not_in_chain` |
| 全部通过 | `active=true`，允许开跑 |

- 只写本地文件：不开端口、不走网络；框架只读不执行文件内容，文件字段不参与任何路径/命令拼接。

**门禁**：agent 未激活 → 阻断整批；每个绑定 dep 必须命中 ≥1 次，否则 `mock_not_used`；`serialization_verified=false` 必须在报告中可见；gRPC 下游仍必须显式 `unsupported` 且用例已排除/`blocked`（v4.3；v4.4 改由 C3.5.4 的 `mock_server_unavailable` / `mock_stub_invalid` / `mock_unexpected_dep` 门禁接管）。

**v4.4 预留：gRPC 下游用 `ClientInterceptor` 拦截（已被 C3.5.4 取代为主方案，保留为 target 不可重定向时的兜底；v4.3 不实现）**

- 注入点：复用同一个 agent，ByteBuddy 增强 `io.grpc.ManagedChannelBuilder#intercept`，把 Mock interceptor 放到数组首位（保持最外层）；SUT 不调用 `intercept` 时回退到增强 `build()`、用委托 `ManagedChannel` 包装；两条钩子都未命中 → 归 `agent_not_active`（同一心跳文件增加 `grpc_hooked` 字段，判定口径不变）。
- 拦截：`interceptCall(MethodDescriptor, CallOptions, Channel)` 命中绑定时返回假 `ClientCall`，`start()` 内按序回调 `onReady → onHeaders → onMessage → onClose`，不发网络调用；未命中转 `next.newCall(...)`；属于 Mock 范围却未绑定 → `mock_unexpected_dep`。
- 响应构造：JSON → `DynamicMessage`（descriptor 来自仓库 `.protoset`）先用 stub 自己的 `method.getResponseMarshaller().parse(...)` 编码再解析回来，强制走真实 protobuf 编解码且类型与 stub 泛型一致——直接返回 `DynamicMessage` 会在生成的 blocking stub 里 `ClassCastException`。
- 错误分支：用 `onClose(Status.X, trailers)` 表达 `grpc-status` 分支，比 Dubbo 侧更自然。
- 边界：仅 unary；server/client/bidi streaming 仍 `unsupported`（与 transport 侧一致）；缺 descriptor 需新增子码（如 `mock_descriptor_missing`）；`serialization_verified` 可标为 `marshaller_roundtrip`，仍不等于链路验证。

#### C3.5.4 gRPC 下游 Mock：gripmock 假服务器 + target 重定向（v4.4 决策，v4.3 不实现）

**结论**：SUT → gRPC 下游的 Mock 不再走进程内拦截，改为**独立 Mock 服务器**：每个 Mock 命名空间拉起一个 gripmock 实例，SUT 的 gRPC target 指向它；用例通过 gripmock 的 REST stub API 装桩，通过请求 history 取证。v4.3 的实现边界不变（gRPC 下游仍 `mock="unsupported"`），本节只定 v4.4 的落地约束。

选型依据、候选对比、实测证据、落选原因与待复验项见 **[决策记录.md §4](决策记录.md)**，本文不复述。

**已知缺陷与框架侧处置**：

| 缺陷 | 影响 | 处置 |
|---|---|---|
| int64 请求匹配失效（字符串与数字都不命中） | 拿 int64 字段做桩匹配会误报 `mock_unexpected_dep` | 生成 stub 时**不把 int64 放进 matcher**；改用其余稳定字段（id / 枚举 / 字符串）+ 空 matcher 兜底，命中后在框架侧断言 int64 |
| 未知 method 返回 `INTERNAL / Exception serializing request!`，而非 `UNIMPLEMENTED` | 不能靠 status 区分“调了没测到的方法”与“请求构造错误” | `mock_unexpected_dep` 的证据取自 history 的未命中记录 + 框架侧调用清单，不看 status |
| int64 以 number、enum 以 number 均被接收 | Mock 服务器会放宽框架的严格性 | 框架发送前做 schema 校验（int64 必须字符串、enum 必须名字），不依赖服务器拒绝 |
| 匹配字段命名同时接受 snake_case 与 camelCase | 同一字段两种写法都能匹配，掩盖契约错误 | stub 只用 proto 字段名生成，字段名契约由框架校验 |

**二进制分发（平台专用 wheel，v4.5）**：

- 发布物：`astsight-gripmock-bin`，每个平台一个 wheel（linux/amd64、linux/arm64；平台标签按构建基线的 manylinux 标准确定），内含 `gripmock` 二进制、`manifest.json`（gripmock 版本、sha256、GOOS/GOARCH、源码 commit）与 LICENSE/NOTICE。
- 版本绑定：`astsight[grpc-mock]` extra 以精确版本（`==`）依赖该发行包；基础包不依赖它。Linux 部署与 CI 安装 extra，容器镜像预装同一 extra；**用户不需要单独下载、解压或配置 gripmock**。基础包保持纯 Python，非 Linux 开发机不会因解析不到平台轮子而安装失败。
- 构建：发布流水线从固定 commit 交叉编译（`CGO_ENABLED=0`，`GOOS=linux`，`GOARCH` 取 `amd64` 或 `arm64`），可复现参数；默认不使用 PATH 上的 `gripmock`、不在运行期联网下载、不要求 Docker。
- 解析与校验：引擎只从包资源目录解析二进制（不查 PATH、不读环境变量）；首次启动校验 `manifest.json` 的版本与 sha256，结果写入 `MockServer.binary` 与 run manifest；不一致即 `mock_binary_missing`。
- 平台矩阵：默认只承诺 `linux/amd64`、`linux/arm64`；矩阵外平台（含 Windows 开发机）使用 gRPC Mock 时报 `mock_binary_missing`，提示安装 extra 或使用容器镜像，并允许 `--gripmock-path` 仅供调试。
- 覆盖路径：`--gripmock-path` 仅用于开发/应急，报告 `binary.source=override` 并标 `degraded`，不得作为生产默认。
- 体积：按架构分别出包，不把所有架构打进一个 wheel，避免安装无用二进制。

**运行形态（Linux 基线）**：

- 每个 Mock 命名空间一个 gripmock 进程/容器，按 `sut_id` 归属；端口按 **5.5** 原子分配（`bind(:0)` 后写回 run manifest），只绑回环或容器内网；
- 装桩走 REST `POST /api/stubs`（或启动时挂载 stub 目录）；SUT 通过 `--target` / 配置指向该地址，改写必须显式写入启动参数与报告；
- 用例开始：清理并装桩 → 调用 SUT → 读 `/api/history` 取证 → 用例结束清理；
- 健康检查走管理端接口，不靠端口探测；停止用 SIGTERM（容器 stop），超时 SIGKILL；
- descriptor 由框架从仓库 `.proto` / `.protoset` 生成并挂载，缺失 → `mock_descriptor_missing`；
- stub 与 history 落 run 目录，作为可追溯证据。

**失败子码（在 C3.5.3 基础上扩展）**：

| 子码 | 含义 | 处理 |
|---|---|---|
| `mock_server_unavailable` | gripmock 未启动、健康检查失败 | 整批阻断 |
| `mock_binary_missing` | 随包二进制缺失、平台不在矩阵、版本或 sha256 不符 | 整批阻断；提示安装 `astsight[grpc-mock]` 或使用容器镜像，不回退 PATH/联网下载 |
| `port_alloc_failed` | 原子端口分配/绑定失败（见 5.5） | 环境类，整批阻断 |
| `mock_identity_mismatch` | 心跳或会话命名空间与本 run 的 `sut_id`/endpoint/pid 不一致（见 5.5） | 框架/配置类，整批阻断 |
| `mock_descriptor_missing` | 缺 proto / protoset，无法建桩 | 相关下游 `unsupported`，用例排除 |
| `mock_stub_invalid` | 桩被服务器拒绝（JSON 非法、字段不存在） | 阻断，归 `mock_error` |
| `mock_unexpected_dep` | 请求未命中任何桩（以 history 为准） | 阻断，归 `mock_error` |
| `mock_branch_not_found` | 用例要求的语义分支没有对应桩 | 阻断 |
| `mock_not_used` | 绑定桩命中 0 次 | 用例失败（防假绿） |

**证据等级**：`serialization_verified=wire_roundtrip`；仍未验证 provider 实现，报告中与 Dubbo 的 `agent_filter` 并列声明。`MockServer` 必须记录 `sut_id`、`namespace` 与 `binary`（版本/sha256/平台/来源），供 5.5 的实例归属校验与供应链审计。v4.3 的报告口径继续写 `mock="unsupported"`，直到 v4.4 落地。

**门禁**：随包二进制版本与 sha256 校验通过、平台在承诺矩阵内才执行；Mock 服务器健康检查通过才执行；每个绑定桩必须命中 ≥1 次；未命中必须有 history 证据；不得把“没匹配上”当成通过；target 改写必须显式、可审计。

#### C3.5.5 HTTP 下游 Mock：内嵌假服务器 + route_key（v4.6.1，仅明文 HTTP）

**结论**：SUT → HTTP 下游的 Mock 由引擎**内嵌 HTTP Mock 服务**完成。每个 `sut_id` 一个实例，SUT 启动前按 `mock.strategy` 把对应出站调用指向它；用例通过 C3.5 的会话 API 选择语义分支；请求按 `route_key` 命中下游，调用记录写入 `mock_calls.jsonl` 作为证据。**首版只支持明文 HTTP**：HTTPS 目标、需要 TLS 的代理链路、TLS 证书固定/mTLS 一律 `unsupported`，不降级、不 MITM、不安装测试 CA。

**运行形态**：

- 组件为引擎进程内的 FastAPI/uvicorn（`fastapi`、`uvicorn` 从 M1 示例依赖提升为运行时依赖），`MockServer.mode="embedded"`、`transport="http"`；不引入独立二进制，也不复用 C3.5.4 的 gripmock 进程。
- 每个 `sut_id` 一个实例；端口按 **5.5** `bind(127.0.0.1:0)` 原子分配，实际端口写回 run manifest（跨容器部署时绑容器网卡，manifest 记录可达地址）。默认只监听回环。
- 每实例随机 admin token，随启动参数注入；token 只落指纹，不落报告。管理面与数据面同端口，`/_admin/*` 必须带 token。
- 启动顺序：分配端口 → 构建路由表 → 健康检查 → 注入 SUT 配置 → 启动 SUT → 执行用例 → drain → 停止 → 落证据。健康检查失败即 `mock_server_unavailable`，整批阻断。
- 资源上限：请求体 1 MB、响应体 4 MB、请求头 64 KB、并发 64（均可配）；超限返回 413 并记 `mock_response_invalid`。只支持 HTTP/1.1 明文；h2c、WebSocket、流式响应不在首版范围。
- 健康接口：`GET /_admin/health` 返回 `{status, sut_id, namespace, route_count, active_session_id, inflight, uptime_ms}`；`inflight` 为正在处理的数据面请求数。
- 实例信息接口：`GET /_admin/server` 返回 `MockServer`（含 `history_path`、路由、token 指纹），供执行链路定位证据；不含 admin token。
- 会话历史接口：`GET /_admin/sessions?run_scope=<scope>&limit=<n>` 返回最近创建的会话快照（`session_id`、`case_id`、`bindings`、`active`、关闭原因）。执行链路默认用本次 `run_id` 作为 `run_scope`，运行结束后据此核对“绑定的 dep 是否至少命中一次”；`limit` 上限与内存历史上限一致。

**`dep_id` 唯一性规则（本版核心）**：

格式固定为：

```
http:{downstream_key} {METHOD} {path_key}
```

`downstream_key` 标识**下游系统**，不标识调用方；按以下优先级取第一个可静态证明的值：

| 优先级 | 取值 | 来源 | 规范化 |
|---|---|---|---|
| 1 | `svc:<name>` | 服务发现 id、`@FeignClient(name/value)`、下游 `spring.application.name`、注册中心服务名 | 小写、trim、非 `[a-z0-9._-]` 字符替换为 `-` |
| 2 | `cfg:<key>` | 决定 base URL 的配置键（即 `mock.property`） | 小写、保留 `.`/`_`/`-` |
| 3 | `host:<host>[:port]` | 解析后的目标主机；回环/私网地址且端口非默认时带端口；IPv6 用 `[::1]` | 小写、去默认端口、去 scheme |
| 4 | `call:<call-site id>` | 目标地址无法静态解析时的调用点（如 `com.acme.OrderService#create`） | 去空白 |
| — | `unidentified:<call-site id>` | 方法与地址都无法静态确定 | 不进 Mock，进缺口清单 |

`path_key` 是**下游完整路径**（base URL 自带路径 + 端点 path）的规范化结果：

1. 路径占位符统一为 `{}`：`/orders/{id}`、`/orders/{id:\\d+}`、`/orders/:id` 均归一为 `/orders/{}`；
2. 去掉 query、fragment；
3. 合并重复 `/`，去掉末尾 `/`（根路径 `/` 保留）；
4. 保留大小写（HTTP 路径大小写敏感）。

唯一性算法（画像构建期确定性执行）：

1. 每个调用点解析出 `(METHOD, normalized target URL, path_key, identity evidence{svc|cfg|host|call}, mock.property)`；
2. **先按 `(METHOD, normalized target URL)` 分组**：同一物理端点被多个调用点调用时合并为一组，`call_sites[]` 记录全部调用点；这样同一端点的不同调用点即使证据不同（一个有服务名、一个只有配置键）也不会被拆开；
3. 每组从全部调用点的证据中按 `svc > cfg > host > call` 取最高优先级值作为 `downstream_key`，计算 `dep_id`；无法得到 normalized target 的调用点按 `call:` 单独成组，标 `inferred`；
4. 两个不同目标组得到相同 `dep_id`（同一服务名/配置键复用到不同目标）时，追加 `~<sha256(normalized target)[:8]>` 并记 `dep_collision_resolved`（含 `from/to/targets`）；同一逻辑服务的多实例（同服务名、同路径、不同主机）默认合并为一个 dep，由运行期 URL 覆盖统一指向 Mock；
5. 加哈希后仍冲突 → `dep_collision`，画像构建失败，交人工修配置，不自动合并；
6. Profile 级约束：`downstream_deps[].id` 全局唯一；每个 case 的 `mock_bindings` 的 key（`dep_id`） 唯一（重复即 `mock_binding_duplicate`）；`MockRoute.route_key` 在 namespace 内唯一；
7. `route_key = "r" + sha256(dep_id.encode("utf-8")).hexdigest()[:12]`；若两个 `route_key` 意外相同则延长到 16/20 位。`route_key` 只用于 URL 前缀与路由，**不从 URL 反解 `dep_id`**，映射关系固定在 run manifest。

示例：

| 场景 | `dep_id` |
|---|---|
| pay-service，`POST /gateway/pay` | `http:svc:pay-service POST /gateway/pay` |
| stock-service，`GET /api/v1/status` | `http:svc:stock-service GET /api/v1/status` |
| 同一下游端点被两个调用点调用 | 仍为 `http:svc:notify-service POST /notify`，`call_sites` 两条 |
| 无服务名，配置键 `pay.client.base-url` | `http:cfg:pay.client.base-url POST /gateway/pay` |
| 硬编码 `http://127.0.0.1:9001/health` | `http:host:127.0.0.1:9001 GET /health` |
| `/orders/{id}` 与 `/orders/{orderId}` | 都归一为 `http:svc:order-service GET /orders/{}` |
| 两个服务都暴露 `GET /api/v1/status` | `svc:` 不同，天然不撞 |
| 无法静态确定 | `unidentified:com.acme.OrderService#create`，不生成 route |

> **为什么不只用 `http:{METHOD} {path}`**：该写法不含下游身份。两个服务都暴露 `GET /api/v1/status` 时，URL 覆盖后它们都命中同一个 Mock 主机、同一个 Host、同一个 method+path，Mock 无法区分；`dep_id` 会重复，用例绑定也会串。加入 `downstream_key` 后，同一路径在不同下游之间天然不同；同一下游的多个调用点又合并，避免另一个方向的重复。

> **为什么 `dep_id` 不包含 `case_id`**：`dep_id` 回答“这是哪个下游端点”，跨用例稳定；用例要的“同一个 URL 返回不同结果”由会话 binding 的 `branch` 回答。`route_key` 只由 `dep_id` 生成，SUT 启动时注入一次；若把 case 编进 `dep_id`，同一 SUT 进程内就必须按用例改 URL 配置，只能重启 SUT 或反复注入，且会把同一端点拆成 N 个身份，导致画像、覆盖率与证据碎片化。需要新返回时在 `semantic_branches` 增加分支；按用例变化的业务标识用 `T{run_scope}_{case_id}_` 变量替换；同一用例内的调用顺序语义标 `mock_sequence_unsupported`，都不通过拆 `dep_id` 解决。

同一 `dep_id` 在不同用例中的绑定示例：

| 用例 | `dep_id` | `branch` |
|---|---|---|
| TC-B-001 | `http:svc:pay-service POST /gateway/pay` | `success` |
| TC-B-002 | `http:svc:pay-service POST /gateway/pay` | `insufficient_balance` |
| TC-B-003 | `http:svc:pay-service POST /gateway/pay` | `timeout` |

**请求路由（resolve order）**：

1. `/_admin/*` → 管理面，校验 token；
2. 路径前缀 `/_dep/<route_key>` → 直接命中对应 dep，前缀后的原路径只作证据（`url_override` 专用配置）；
3. `Host` 头（小写，含可选端口）→ 命中 host 组，再在该组内按唯一 `(METHOD, path_key)` 命中 dep（`discovery_override` 与 `http_proxy`）；
4. 全表唯一 `(METHOD, path_key)` 匹配 → 命中（仅当路由表内恰好一条）；
5. 无命中 → `mock_route_unknown`；多条命中 → `mock_route_ambiguous`；**任何情况都不透传真实下游**。

Mock 错误响应统一为 502 + JSON `{error, route_key?, dep_id?}` 并写调用记录；该响应对 SUT 只是普通下游错误，**不得被当作有效语义分支**，框架侧直接把用例标 `blocked`。

**注入矩阵（仅明文 HTTP）**：

| `mock.strategy` | 适用 | 注入动作 | 路由依据 | 失败处理 |
|---|---|---|---|---|
| `url_override` | 有可覆盖的、且**只服务该 dep** 的 base URL / endpoint URL 配置 | 把 `mock.property` 的值改为 `http://<mock_host>:<port>/_dep/<route_key>` | 路径前缀 | 配置键缺失/不可写/值不是 http → `mock_config_not_overridable`；一个键被多个 dep 共享 → `mock_config_ambiguous`，转 `http_proxy` 或 `unsupported` |
| `discovery_override` | 客户端走服务发现，且测试环境可重定向注册中心/静态服务列表 | 测试注册中心/静态服务列表把服务名指向 Mock 主机端口；`Host` 保持服务名 | `Host` | 发现机制不可替换 → `discovery_not_supported`，下游 `unsupported`（条件能力，不计入 v1 承诺） |
| `http_proxy` | 硬编码地址、自定义拦截器、动态 URL，且为明文 HTTP | 显式代理：`HTTP_PROXY` / `-Dhttp.proxyHost` 指向 Mock；透明代理：环境用 iptables REDIRECT 到 Mock | 显式代理读 absolute-form；透明代理读 origin-form + `Host` | 客户端忽略代理、目标为 HTTPS、需要 root 但环境不提供 → `unsupported` |
| `unsupported` | HTTPS、动态地址无法覆盖、自定义传输 | 不注入 | — | 用例 `blocked`，报告 `unsupported` 单列 |

- `url_override` 必须记录 `injection.kind="base_url" | "endpoint_url"`、`original`、`injected`、`path_mode`。配置值是 base URL 时注入 `http://<mock_host>:<port>/_dep/<route_key>`，由客户端追加原路径；是完整 endpoint URL 时注入 `http://<mock_host>:<port>/_dep/<route_key><original_path>`，保留原路径仅作证据。两种都靠路径前缀路由，**不依赖路径是否被网关改写**。
- `discovery_override` 的 Host 可能带端口或被网关改写；只接受 manifest 中登记的 `host_routes`，不做模糊匹配。Host 无法保持 → `unsupported`。
- `http_proxy` 只处理明文。收到 `CONNECT` 直接返回 405 并记 `tls_not_supported`；不建立 TLS 隧道。
- 同一 SUT 内不同下游可以混用策略；策略由 C3 按配置可用性选择，C3.5 只执行，不在运行期改选。

**v1 支持矩阵（承诺边界）**：

| 能力 | v1 状态 | 条件/说明 |
|---|---|---|
| `url_override`（专用配置） | **承诺** | 配置键只服务一个 dep：一个下游专属 base URL，或一个完整 endpoint URL |
| `url_override`（共享 base URL） | **不支持** | 一个配置键服务多个 dep 时无法注入多个 `route_key`；应转 `http_proxy`，否则 `unsupported`（`mock_config_ambiguous`），不猜测 |
| `discovery_override` | **条件支持** | 仅当测试环境提供可重定向的注册中心或静态服务列表；否则 `discovery_not_supported`，不计入 v1 承诺 |
| `http_proxy`（显式代理） | **承诺** | 仅明文 HTTP；客户端必须遵循 `HTTP_PROXY` / `-Dhttp.proxyHost` 等显式代理配置 |
| `http_proxy`（透明代理） | **条件支持** | 需要环境提供 iptables REDIRECT 与权限；框架只输出规则，不作为 v1 承诺 |
| HTTPS | **不支持** | `tls_not_supported`，不降级、不 MITM |
| 同 JVM 内 mock HTTP 客户端 bean | **不支持** | 绕过编解码/拦截器/降级链，测不到真实调用路径 |
| 动态 URL / 自定义传输 | **不支持** | 除非可经明文代理覆盖；否则 `unsupported` |
| 请求匹配、调用序列、h2c、WebSocket、流式 | **不支持** | v1 明确边界 |

**选择规则**：C3 按 `url_override`（专用配置）> `discovery_override`（仅环境支持时）> `http_proxy`（显式）> `unsupported` 选择；一个 dep 只选一种策略，运行期不自动降级、不改选。选不出即 `unsupported`，依赖用例 `blocked`。

**桩响应模型**：

```json
{
  "status": 200,
  "headers": {"Content-Type": "application/json"},
  "body": {"code": 0, "trans_no": "T{run_scope}_{case_id}_PAY", "status": "paid"},
  "delay_ms": 0,
  "drop_connection": false
}
```

- `body` 支持 JSON 值、字符串、`{"raw_base64": "..."}`；默认 `Content-Type: application/json`；响应长度由框架计算，禁止桩自定义 `Content-Length`、`Transfer-Encoding`、`Connection` 等逐跳头。
- 分支只由**会话 binding** 决定，不做请求体/头匹配。同一用例内同一 dep 的多次调用返回同一分支；需要“第一次成功、第二次失败”的顺序语义时标 `mock_sequence_unsupported`，不猜。
- `delay_ms` 用于触发真实超时（0–60000）；`drop_connection` 为尽力而为：服务端关闭连接且不回响应，若运行环境无法强制 reset，退回“延迟 + 客户端超时”，在证据中记 `drop_mode="reset" | "timeout"`，不宣称已 reset。
- 响应体中的 `T{run_scope}_{case_id}_` 变量由框架确定性替换，不执行表达式；替换后仍超限 → `mock_response_invalid`。
- 未绑定/未知/歧义路由不返回桩内容；不得用默认 200 兜底。

**会话与请求处理**：

- 复用 C3.5 的 `POST/DELETE /_admin/sessions`；`bindings[].dep_id` 使用上述新格式；`namespace` 必须等于当前 `sut_id`。
- `POST /_admin/sessions/{id}/drain` 接受 `{timeout_ms}`（默认 200，0–60000），等待当前数据面 `inflight` 归零；返回 `{session_id, drained, inflight, waited_ms}`。超时返回 200 且 `drained=false`，由 fixture 决定告警/失败；会话不存在或已关闭返回 `mock_session_not_found`。
- 单请求处理顺序：解析 method/path/query（限长）→ 路由解析 → 检查 active session（否则 `mock_session_missing`）→ 检查 binding（否则 `mock_unexpected_dep`）→ 检查 branch（否则 `mock_branch_not_found`）→ 生成响应 → 按 `delay_ms` 延迟 → 记录调用。
- 会话关闭后的请求 → `mock_late_call`；会话 TTL 过期 → `mock_session_expired`；晚到调用仍按最近关闭会话的 `session_id/case_id` 归因，错误响应与晚到调用都进报告，不静默丢弃。
- 每个 case 记录命中的 route/dep/branch、调用次数、未命中与晚到调用；绑定 route 命中 0 次 → `mock_not_used`，用例失败。

**证据（`mock_calls.jsonl`，一行一次调用）**：

| 字段 | 说明 |
|---|---|
| `call_id/session_id/case_id` | 调用身份 |
| `route_key/dep_id/route_match` | 命中的路由与依据（`path_prefix` / `host_path` / `method_path`） |
| `method/original_path/query_keys` | 请求行信息；query 只记 key，不记值 |
| `request_headers` | 允许列表：`content-type`、`accept`、`user-agent`、`x-request-id`；`authorization`/`cookie`/`token` 等一律脱敏 |
| `body_sha256/body_bytes` | 默认只记哈希与长度；`--capture-mock-bodies` 时对 JSON/text 且 ≤64 KB 的请求体留存，仍按配置脱敏 |
| `branch/response_status/delay_ms/drop_mode` | 本次返回的语义分支与响应 |
| `result` | `ok` / `unknown` / `ambiguous` / `unbound` / `branch_missing` / `late` |
| `at` | 时间戳 |

证据汇总：报告读取 `mock_calls.jsonl`，按 `result` 与 `case_id` 聚合。`unknown` / `ambiguous` / `unbound` / `branch_missing` 为阻断类；`late`（含 TTL 过期）、损坏行、无法归因调用为告警并使报告 `degraded`。`--mock-evidence` 可显式指定路径；缺省从 `GET /_admin/server` 的 `history_path` 获取。报告同时读取 `GET /_admin/sessions?run_scope=...` 的会话快照：逐会话比较 `bindings[].dep_id` 与实际调用，零命中的绑定计入 `unused_binding_count` 并作为 `mock_not_used` blocker；会话接口不可达、未配置 admin、或证据中的 `session_id` 不在历史中时标 `sessions_reason`/`sessions_missing`、报告 `degraded`，不静默跳过。

**P0 落地（当前代码）**：`MockPlan`（`contracts/mock_plan.py`）描述 `sut_id`、监听地址/端口、路由、分支与注入；`astsight/mock/plan.py` 负责加载/校验并在 `run --mock-plan` 时启动内嵌服务。计划校验覆盖：`dep_id` 唯一且以 `http:` 开头、方法白名单、路径归一化、HTTPS `tls_not_supported`、`route_key` 必须等于确定性计算值、注入 `dep_id` 必须存在且不重复、`url_override` 必须有 `property/original`、`unsupported` 必须有 `reason`；计划文件上限 4 MB、1000 条 route、每条 64 个分支、1000 条 injection。服务停止由 `finally` 保证。运行产物 `run_manifest.json` 记录实际端口、route/injection 与 token 指纹，`mock_injection.json` 只含注入清单；两者都不含 token。内嵌服务未产生调用时 `mock_calls.jsonl` 可能不存在，plan 模式将其视为“零调用”并继续用会话历史做 `mock_not_used` 校验。C3 自动识别调用点、把注入写入 SUT 启动配置仍由外部启动器完成。

**P1 落地（当前代码，仅 Python）**：`astsight/profile/downstream.py` 从路由 handler 出发做有界 AST 扫描（不执行被测代码），识别 `requests`/`httpx` 模块函数、`Session`/`Client` 实例、`.request(...)` 与 `urllib.request.urlopen`；支持字面量 URL、环境变量/配置键拼接、f-string、`str.format`、`urljoin` 和局部变量解析，并沿本地调用与 CodeGraph `callees` 传递（上限：40 符号、深度 3、200 调用点、2000 文件/单文件 1 MB/总 32 MB）。`dep_id` 仍统一走 `build_dep_id_plan`：目标可静态证明时用 `host:`，只有路径可证明时进入 `call:` 并标 `inferred`；`svc:` 需要服务发现证据、`cfg:` 需要配置值解析，当前不猜，因此 Python MVP 主要产出 `host:`/`call:`。无法解析的调用点写 `downstream_unsupported`（`target_dynamic`/`target_unresolved`/`tls_not_supported`/`http_method_missing`）。`python -m astsight mock plan` 产出 `MockPlan` 草案：分支留空；有配置键但原值未知的注入标 `unsupported` + `injection_original_unknown`（不伪造 `original`），有确定目标的用 `http_proxy`。自动解析配置值、补语义分支和把注入写进 SUT 启动环境仍属后续工作。

**失败子码（在 C3.5.3/C3.5.4 基础上扩展）**：

| 子码 | 含义 | 处理 |
|---|---|---|
| `mock_server_unavailable` | 内嵌服务未启动或健康检查失败 | 整批阻断 |
| `mock_route_unknown` | 请求没有命中任何 route | 阻断该用例，保留调用记录 |
| `mock_route_ambiguous` | 同一请求命中多条 route | 阻断；要求画像消歧 |
| `mock_route_conflict` | `dep_id` 或 `route_key` 在 namespace 内重复 | 画像构建期阻断 |
| `dep_collision` | 不同 target 降级后仍归一到同一 `dep_id` | 画像构建期阻断，人工修配置 |
| `mock_binding_duplicate` | 同一 case 对同一 `dep_id` 绑定多次 | 导入期阻断 |
| `mock_binding_ambiguous` | 同一 dep 的不同调用点需要不同分支，HTTP 边界无法区分 | 阻断该 case，不拆 dep_id |
| `mock_config_not_overridable` | 配置键不存在/不可写/值非 http | 下游 `unsupported`，依赖用例 `blocked` |
| `mock_config_ambiguous` | 一个配置键被多个 dep 共享，无法注入多个 `route_key` | 转 `http_proxy`；不可用则 `unsupported` |
| `discovery_not_supported` | 环境没有可重定向的注册中心/静态服务列表 | 下游 `unsupported`，不计入 v1 承诺 |
| `mock_proxy_unsupported` | 客户端绕过代理或代理链路不可用 | 下游 `unsupported` |
| `tls_not_supported` | 目标为 HTTPS、`CONNECT` 或需要 TLS | 下游 `unsupported`，依赖用例 `blocked` |
| `mock_response_invalid` | 桩响应头/体/长度非法或超限 | 阻断，归 `mock_error` |
| `mock_sequence_unsupported` | 需要按调用次数切换分支 | 阻断该 case |
| `mock_not_used` | 绑定 route 命中 0 次 | 用例失败（防假绿） |
| `mock_late_call` | 会话关闭后到达 | 报告告警；严格模式阻断 |

**数据模型**：`MockServer` 扩展为 `transport(http|grpc)`、`mode(embedded|gripmock)`、`admin_endpoint`、`route_modes[](path_prefix/host_group/method_path)`、`route_count`、`routes[]`、`history_path`、`health`、`token_fingerprint`；新增 `MockRoute(route_key, dep_id, method, path_key, host, target, strategy, source, route_match)` 与 `MockCall`（字段同上表）；`ports[]` 增加 `component="http_mock"`。

**5.5 归属**：HTTP Mock 与 gripmock 一样按 `sut_id` 一实例；`namespace=sut_id`；admin token 每实例独立；端口与 owner 写 `run_manifest.json`；会话请求的 namespace 必须等于当前 `sut_id`，否则 `mock_identity_mismatch`。

**门禁**：

- 画像期：每个 `type=http` 下游必须有 `mock.strategy`、唯一 `dep_id`、唯一 `route_key` 与 `injection` 证据；`url_override` 必须给出只服务该 dep 的 `mock.property`（共享键 → `mock_config_ambiguous`），`discovery_override`/`http_proxy` 必须给出等价的发现或代理覆盖证据；`discovery_override`/透明代理必须标注为条件能力，环境不满足时 `discovery_not_supported`；`unsupported` 必须显式声明且依赖用例 `blocked`；`https://` 必须标 `tls_not_supported`；`dep_collision` / `mock_route_conflict` 直接阻断。
- 执行期：健康检查通过；每个绑定 route 命中 ≥1 次；未知/歧义路由、Mock 错误响应、晚到调用按上表处理，不得当作通过；任何真实出站都不得透传。
- 报告：`mock_sessions` 增加 HTTP route 命中、未命中、`unsupported` 与 `tls_not_supported` 明细；只断言状态码且无 Mock 命中证据的用例标 `weak_oracle`。

**边界**：

- 不做请求匹配、不做调用序列、不做 HTTPS/MITM、不做 h2c/WebSocket/流式、不做动态 URL 重写。
- 内嵌服务崩溃会终止引擎进程；因此启动前必须健康检查，运行期异常写 `mock_server_unavailable`，不假装用例通过。
- 透明代理依赖环境提供 iptables REDIRECT 与相应权限；框架只输出规则/说明，不默认要求 root。拿不到就 `unsupported`。
- 同一下游端点的多个调用点共享分支；需要按调用点区分时标 `mock_binding_ambiguous`，不通过拆 `dep_id` 制造重复身份。

---

### C5 数据准备（C5a 数据目录 / C5b 造数计划）

C5 拆成两个阶段，消除 "A4 需要 data_plan、data_plan 又需要已批准用例" 的循环依赖：

| 阶段 | 时点 | 输入 | 输出 | LLM |
|---|---|---|---|---|
| **C5a 数据目录** | C4 之前 | DDL/库结构、seed、profiles、context、ruleset | `data_catalog.json`（有哪些表/列/可用业务态） | 是（字段业务语义与候选状态，适配层执行；结构解析/FK/拓扑确定性） |
| **C5b 造数计划** | 用例批准后 | `caseset.json`/`cases.json`、`data_catalog.json`、`ruleset.json`、profiles | `data_plan.json`（逐用例 fixtures/回收） | 是（业务态与取值候选；**SQL 不由模型输出**） |

**C5a 数据契约**：

```json
{
  "schema_version": 1,
  "catalog_id": "dc-...",
  "project": "orders",
  "context_id": "...",
  "dialect": "sqlite",
  "sources": {"ddl_sha256": "...", "seed_sha256": "...", "profiles_sha256": "...", "ruleset_hash": "..."},
  "tables": [{
    "name": "orders",
    "primary_key": ["id"],
    "columns": [
      {"name": "id", "type": "INTEGER", "nullable": false, "primary_key": true, "evidence": "verified"},
      {"name": "status", "type": "TEXT", "nullable": false, "enum_values": ["pending", "paid"],
       "semantic_type": "status",
       "value_semantics": [{"value": "pending", "meaning": "待支付", "evidence": "verified"}],
       "evidence": "verified"}
    ],
    "evidence": "verified"
  }],
  "foreign_keys": [{"child_table": "orders", "child_columns": ["user_id"],
                    "parent_table": "users", "parent_columns": ["id"], "evidence": "verified"}],
  "tables_order": ["users", "orders"],
  "states": [{"state_key": "order_pending", "table_values": {"orders": [{"id": 10, "status": "pending"}]},
              "target_state": {"orders": {"status": "pending"}}, "evidence": "inferred"}],
  "limits": {"max_tables": 500, "max_rows_per_table": 1000, "max_columns_per_table": 500},
  "gaps": [], "questions": [], "warnings": []
}
```

**C5b 数据契约**（`DataPlanBundle`）：

```json
{
  "schema_version": 1,
  "run_scope": "run-20261102T...",
  "plans": [{
    "plan_id": "dp-...",
    "catalog_id": "dc-...",
    "ruleset_hash": "...",
    "case_id": "TC-0001",
    "case_key": "cancel_order",
    "run_scope": "run-...",
    "tables_order": ["users", "orders"],
    "fixtures": {"users": [{"id": 1}], "orders": [{"id": 10, "user_id": 1, "status": "pending"}]},
    "target_state": {"orders": {"status": "pending"}},
    "fk_links": ["orders.user_id -> users.id"],
    "preconditions": [{"kind": "db_state", "state_key": "order_pending"}],
    "statements": [{"kind": "insert", "table": "users",
                    "sql": "INSERT INTO \"users\" (\"id\") VALUES (?)", "params": [1]}],
    "teardown": [{"kind": "delete", "table": "orders",
                  "sql": "DELETE FROM \"orders\" WHERE \"id\" = ?", "params": [10]}],
    "warnings": [], "evidence": ["inferred"]
  }],
  "rejected": [{"case_id": "TC-0002", "reason": "data_plan_invalid", "details": "..."}],
  "warnings": []
}
```

**核心逻辑**：

1. **关联闭包**：从画像涉及的主表出发，沿外键双向扩展（上游必须存在、下游级联影响），直到闭包收敛。**外键关系优先取自 C3 的 DB 血缘（`FOREIGN_KEY_TO`）或 DDL 物理外键**；两者都缺失时标 `inferred`，不靠字段名硬猜
2. **拓扑排序**：按外键依赖确定插入顺序；检测到循环依赖时断开最弱边（可空外键优先）并告警
3. **业务态构造**：C5b 从已批准用例的 `preconditions.state_key` 展开 `DataCatalog.states`，并合并 LLM 的 `fixtures`/`target_state` 候选；模型只能引用 catalog 中已有的表/列/枚举值
4. **读写表区分**：依据 C3 血缘，`WRITES_TABLE` 的表需**构造前置状态**（被测接口会改它），`READS_TABLE` 的表需**构造存在性**（被测接口会查它）——两者造数语义不同
5. **字段业务语义理解**：造数前必须先理解每个字段的**业务语义**（`status=0` 是"已取消"还是"待支付"），而非仅看类型。**语义分三层，来源与"可自动推导性"各不相同**，必须显式区分，否则会误以为"有了 DBML 就不用需求上下文和人工审核了"：

   | 语义层次 | 例子 | 来源 | 能否自动推导 |
   |---|---|---|---|
   | **① 结构语义**（是什么字段）| `amount`=金额、`status`=状态、`created_at`=时间 | 命名 + 类型 + semantic-field-types 12 类分类法 | ✅ 能（确定性 + LLM 辅助）|
   | **② 取值语义**（枚举每个值什么意思）| `status` 0/1/2 = 待支付/已支付/已取消 | DBML `enum`/`records`、DB `COMMENT ON`、代码常量折叠 | ⚠️ 部分能，缺失标 `inferred` |
   | **③ 业务语义**（在业务规则里的含义）| `status=0 且超时30min` = 可取消订单 | **需求原文 section/quote + 已审核用例中显式声明的假设** | ❌ 不能从 DB/代码自动推导 |

   **关键红线**：① 结构语义靠分类法，② 取值语义靠 DBML/DB comment/常量折叠，但 **③ 业务语义的根来源是需求文档与业务知识，只存在于需求上下文和已审核用例的假设里，数据库和代码给不了**。DB 能告诉你 `status` 有 0/1/2 三个值，但给不了"status=0 且超时 30 分钟 ⇒ 可取消"这条业务判断——**DBML/DB comment 只是取值语义的输入，不能替代需求原文与人工审核**。枚举取值从语义源取，不从 DB 现有数据猜
6. **确定性校验（LLM 之后）**：表/列白名单、类型/枚举/NOT NULL、FK 闭包、拓扑顺序、`preconditions` 全覆盖、行数/列数上限；未知表/列直接拒绝，失败用例写入 `rejected`，不生成计划
7. **SQL 渲染**：模型**不输出 SQL**；`data/render.py` 只从通过校验的 fixtures 生成参数化 `INSERT`/`DELETE`，标识符按白名单校验，值只走 `params`
8. **隔离与回收**：每个用例一个 `data_state` fixture，请求前在事务内 apply，用例结束（含失败/异常）按主键 teardown；`run_scope` 记录到计划里用于多 run 审计。`keep-data` 调试模式保留数据

**执行边界**：

- 支持 **SQLite / PostgreSQL / MySQL**：分别使用 stdlib `sqlite3`、`psycopg`、`PyMySQL`；
  方言决定 SQL 占位符（`?` / `%s`）与标识符引用（双引号 / 反引号），计划方言与
  `--db-url` 不一致时显式拒绝，不改写 SQL
- 只覆盖用例级 fixture apply/teardown；建库、迁移、连接池与跨库事务不在范围内
- 有 `preconditions` 但无 `data_plan` 或未配置 `ASTSIGHT_DB_URL` 时，用例标 **`blocked` / `data_unavailable`**，既不算通过也不是 SUT 缺陷
- SQL 执行失败整事务回滚；teardown 失败显式 `data_teardown_failed`，不静默吞掉

**关键设计决策**：

- 两条造数通道并存：**SQL 直插**（覆盖历史脏数据、边界状态）与**接口造数**（保证业务一致性）。v1 实现 SQL 直插；接口造数作为后续扩展，策略按表配置
- 字段级约束 ≠ 业务约束（`qty ≤ 库存`）——业务约束从需求原文与已审核用例的假设推导，推不出的标记 `uncovered`/待人工补充，**不猜**
- **造数计划以 C3 血缘/DDL 为事实源**：`fk_links` 直接生成，`tables_order` 由拓扑排序得出；血缘字段缺失时显式标 `inferred`/`unmapped`
- **一次失败不污染运行时**：C5a 只写 catalog，C5b 只写计划；两者都不自动批准用例，也不修改已审核断言

**异常与边界**：外键缺失（逻辑外键）时从设计文档/命名约定补推，标记 `inferred`；时间敏感数据统一用相对表达式，执行期求值；**C3 血缘标 `unmapped` 的表，造数计划须人工介入确认，不得自动直插**

**门禁**：造数执行成功率 ≥99%；外键闭包完整率 100%（对照人工分析）

**开源能力参照（复用而非重造）**：

> C5 的"理解业务语义 + 造数"两块，已有成熟开源工具各覆盖一部分。AstSight**不必从零实现通用造数**，但需看清"可复用"与"护城河"的边界：

| 环节 | 现成开源参照 | 与AstSight的关系 |
|---|---|---|
| 取值语义表达 | **DBML**（enum 枚举 + note 注释 + records 样例数据 + TableGroup 业务分组）| **是"表达语义的 DSL，不是推导语义的分析器"**——若被测项目提供 DBML，作为 C5 取值语义（②层）的最高优先级输入；未提供则回退 DB comment。它给不了③业务语义 |
| 字段语义分类字典 | **semantic-field-types**（12 大类语义分类法）| 作为 LLM 判定字段语义时的分类依据，替代凭空猜测 |
| 业务术语 → 字段映射 | **schemagate**（术语表 + MCP）| 对齐AstSight"术语表 + 需求上下文"长期记忆资产 |
| 外键闭包 + 拓扑排序造数 | **SeedKit** / **bloviate** | 通用造数机制（外键拓扑、环检测、种子可复现）可借鉴 |
| 逻辑外键补推 | **generate_synthetic_data**（`logical_fks`）| 与"物理外键/逻辑外键分离"同一红线，可借鉴其处理方式 |

**护城河（开源均未覆盖，须自研）**：上述工具只做"通用造数"——造出**合法、可复现**的数据，但**不携带业务意图**。它们造不出"`orders.status=0` 且 `created_at` 超 30 分钟且关联已实名用户"这种**针对特定测试场景、语义精确**的数据。AstSight的**业务态构造**（第 3 步：从用例目标状态反推字段取值）+ **变更驱动**（只造变更接口所需数据）正是别人没做的护城河，与通用造数器是"语义层"与"机制层"的分工。

---

### C4 两级用例设计、审核与版本化

| 项  | 内容 |
| -- | -- |
| 职责 | 从规则库生成业务级用例草案，从变更缺口生成代码级用例草案；统一完成校验、与用例库 diff、人工审核与写库 |
| 输入 | `ruleset.json`、`context.json`、`profiles`（含 transport/business_key/coverage_role）、`mock_semantics.json`、`data_catalog.json`（有 DDL 时）、`changed_apis.json`（含 transport/change_kinds；代码级）、`caseset.json`（可为空）、`cases.draft.json` |
| 输出 | proposal 目录（`proposal.json`、`cases.review.md/json`、`validation.json`、`rules_snapshot.json`、`requirements_snapshot.json`）+ 更新后的 `caseset.json` |

**两条生成路径**：

| 路径 | 触发 | 输入 | 生成方式 | 产出 |
| --- | --- | --- | --- | --- |
| C4a 业务级 | 需求/规则变更、`full` | RuleSet + Context + Profile + MockSemantic + DataCatalog | 规则驱动（`prompts/a4_case_design.md`，LLM 适配层可选）；导入/校验/审核零 LLM，全量 | `level=business` 草案 |
| C4b 代码级 | 代码变更、`delta` | ChangedAPI + Profile + RuleSet + MockSemantic + DataCatalog + 已有 business 用例 | 差分补齐（模型/人工），只补未覆盖分区；导入/审核零 LLM | `level=code` 草案 |

**草案模型**：

```json
{
  "level": "business",
  "case_key": "create_order_stock_insufficient",
  "title": "库存不足创建订单被拒绝",
  "rule_refs": [{"rule_key": "RK-1a2b3c4d5e6f", "rule_id": "BR-01"}],
  "requirement_refs": [{
    "doc": "requirements.md",
    "section": "订单 > 创建",
    "quote": "库存不足时返回 40001"
  }],
  "change_refs": [],
  "transport": "http",
  "endpoint": "POST /api/orders",
  "coverage_role": "primary",
  "category": "negative",
  "priority": "P0",
  "path_params": {},
  "query": {},
  "headers": {},
  "body": {"sku_id": "SKU-1", "qty": 999},
  "preconditions": [{
    "kind": "db_state",
    "state_key": "stock_empty",
    "table": "products",
    "where": {"sku_id": "SKU-1"},
    "set": {"stock": 0}
  }],
  "mock_bindings": {
    "http:svc:pay-service POST /gateway/pay": "success"
  },
  "asserts": [
    {"kind": "status_code", "op": "eq", "expect": 400},
    {"kind": "body_field", "target": "error_code", "op": "eq", "expect": 40001}
  ],
  "assert_strength": "L2",
  "rationale": "BR-01：库存不足返回 40001",
  "assumptions": [],
  "note": null
}
```

代码级草案把 `level` 改为 `code`，`change_refs` 填
`[{"base": "main", "target": "feature", "symbols": [...], "changed_api": "POST /api/orders"}]`；
`requirement_refs` 可沿用规则来源的 section/quote，`preconditions` 可空。
`mock_bindings` 是 `dict[dep_id, branch]`，有下游依赖时应显式声明；缺失时按默认 `success` 处理并在审核文件标注
（不静默）。`dep_id` 必须与 C3 画像一致（`http:{downstream_key} {METHOD} {path}`，见 C3.5.5），
不得在草案里另取 URL、类名或方法名，也不得按调用点自行拆分。

草案不填 `TC-NNNN`，也不重复写 `method/path`：由 import 从 context 的 `endpoint` 解析并回填，
避免两套路径写法。

**按 transport 的请求与断言模型**：

| transport | 请求字段 | 允许的断言 `kind` | 禁止 |
|---|---|---|---|
| `http` | `path_params`、`query`、`headers`、`body` | `status_code`、`body_field`、`header` | 混用 RPC 断言 |
| `grpc` | `service`、`method`、`request`、`metadata`、`deadline_ms` | `grpc_status`、`response_field`、`metadata` | `status_code`、`body_field`；streaming |
| `dubbo` | `interface`、`method`、`param_types`、`arguments`、`attachments`、`timeout_ms` | `rpc_status`、`result_field`、`exception_type` | `status_code`、`body_field`；无 `param_types` 的重载调用 |

gRPC 草案片段：

```json
{
  "transport": "grpc",
  "endpoint": "grpc:/acme.order.v1.OrderService/CreateOrder",
  "service": "acme.order.v1.OrderService",
  "method": "CreateOrder",
  "request": {"sku_id": "SKU-1", "qty": 1},
  "metadata": {"authorization": "Bearer ${TOKEN}"},
  "deadline_ms": 3000,
  "asserts": [
    {"kind": "grpc_status", "op": "eq", "expect": "OK"},
    {"kind": "response_field", "target": "order_no", "op": "exists"}
  ]
}
```

Dubbo 草案片段：

```json
{
  "transport": "dubbo",
  "endpoint": "dubbo:com.acme.order.api.OrderService#createOrder?group=order&version=1.0.0",
  "interface": "com.acme.order.api.OrderService",
  "method": "createOrder",
  "param_types": ["com.acme.order.api.CreateOrderRequest"],
  "arguments": [{"sku_id": "SKU-1", "qty": 1}],
  "attachments": {},
  "timeout_ms": 3000,
  "asserts": [
    {"kind": "rpc_status", "op": "eq", "expect": "OK"},
    {"kind": "result_field", "target": "order_no", "op": "exists"}
  ]
}
```

> `endpoint` 必须与 context 中的端点标识完全一致（HTTP 为 `http:{METHOD} {path}`，gRPC 为 `grpc:/{package}.{Service}/{Method}`，Dubbo 为 `dubbo:{interface}#{method}?group=&version=`）。gRPC/Dubbo 的 `request`/`arguments` 字段必须能在 proto message / 接口源码中定位；`param_types` 顺序必须与方法声明一致。断言至少一条 transport 原生状态断言（`status_code` / `grpc_status` / `rpc_status`）。

**覆盖角色约束（生成与校验的前置口径）**：

| coverage_role | business 用例 | code 用例 | 说明 |
|---|---|---|---|
| `primary` | 全量（该 `business_key` 的业务规则都落在这里） | 全量 | 每个 `business_key` 有且只有一个 primary |
| `alias` | **不生成全量**；只生成 transport 契约冒烟：成功 1 条 + 一个错误/异常分支 1 条（异常分支有契约依据时） | 只生成 transport 适配相关的回归，不重复业务分区 | 用于验证序列化、字段映射、错误映射、鉴权/超时；不计入业务规则覆盖 |
| `independent` | 全量 | 全量 | 适配差异大，按独立接口对待 |

- 业务场景按 `business_key` 去重：同一条规则/需求只归属 primary，alias 冒烟不重复计入覆盖率。`--transport-scope=full` 时允许在 alias 上生成全量，但报告必须区分“业务覆盖”与“transport 覆盖”，不得把 alias 的全量用例算成额外业务覆盖。
- `--transport-scope`：
  - `all`（默认）：primary 全量 + alias 冒烟 + independent 全量；
  - `primary`：只生成/执行 primary + independent，alias 端点写入 `transport_alias_skipped` 并标 `degraded`；
  - `full`：所有端点全量，用于高风险变更。
- alias 冒烟的最低要求：一条 transport 原生成功断言 + 一条错误/异常断言（无契约依据时写 `expect_unknown`，不得臆造）；`adapter_fingerprint` 相同且标 `equivalent` 的 alias 可整体跳过，但报告必须列出跳过的端点与指纹。
- transport 特有的规则/字段（如 gRPC `metadata`/错误码、Dubbo `exception_type`）仍应在 alias 冒烟中体现；它们属于协议适配覆盖，不并入 primary 的业务规则覆盖。
- 草案的 `coverage_role` 必须与画像一致；不一致时 import 直接判错，不允许用例自行把 alias 升级为 primary。

**核心逻辑**：

1. 结构校验：JSON/Pydantic schema；文件大小、用例数、断言数、body 深度、preconditions 数量
   上限；拒绝绝对路径、`..`、指向项目外的 symlink
2. 规则校验：`rule_refs` 非空且 `rule_key` 存在于当前 ruleset；不得引用 `ungrounded`；
   C/D 规则不得作为唯一依据；引用冲突规则只告警并在审核文件中展示；business 至少 1 条
   A/B 规则，code 至少 1 条 A/B 或 CR 规则
3. 端点校验：`endpoint` 必须存在于 context（先精确匹配，再按 `normalize_path` 对齐参数写法），
   `transport` 必须与画像一致；HTTP 校验 `path_params` 覆盖全部占位符且无多余，query/header/body
   顶层字段在契约中；gRPC 校验 `service/method` 与 proto 一致、`request` 字段在 message 中、
   `deadline_ms` 为正数；Dubbo 校验 `interface/method/param_types` 与接口源码一致、`arguments`
   数量与 `param_types` 相等且顺序对应；跨 transport 字段（如 gRPC 带 `body`）直接判错
4. 断言校验：kind/op 必须是该 transport 支持的组合（见上表）；至少一条原生状态断言
   （`status_code` / `grpc_status` / `rpc_status`）；响应字段未知只告警；gRPC streaming/oneway
   用例拒绝；Dubbo `exception_type` 断言必须有接口源码或契约中的异常声明依据，否则
   `assertion.not_grounded`
5. 需求校验：business 的 `requirement_refs` 非空；doc 在 scope 内且 section 存在；`quote`
   至少 4 个非空白字符且必须是 section 原文子串；quote 过短或重复出现给 warning
6. 唯一性与级别：`(endpoint, case_key)` 不重复；`case_key` 符合
   `^[a-z][a-z0-9_]{2,63}$`；code 用例不得与同锚点 business 用例的期望冲突
7. 缺口校验（code）：`change_refs` 非空；对应变更接口/符号必须有规则依据，否则写
   `change_without_oracle`，**不生成用例**
8. Mock 绑定校验：`mock_bindings` 的 key（`dep_id`） 必须存在于该 endpoint 画像的 `downstream_deps`，
   `branch` 必须在该下游的 `semantic_branches` 中；有下游却未声明的按默认 `success` 并在审核文件标注；
   声称验证超时/降级却只绑 `success` 的写 `weak_oracle` 警告；HTTP 下游的 `dep_id` 必须稳定且唯一
   （`http:{downstream_key} {METHOD} {path}`，见 C3.5.5），不得使用会随包名/客户端类名变化的标识，
   也不得为同一端点按调用点制造多个 id。`mock="unsupported"` 的下游
   （仅 SUT → gRPC）不允许出现在 `mock_bindings` 中；依赖它的用例必须显式排除或标
   `blocked`，审核文件单列“无法 Mock 的下游”。Dubbo 下游可绑定，但必须在执行计划中声明
   agent 加载方式与 `serialization_verified=false`
9. **transport 回退不在用例层发生**：草案与 store 记录画像判定的 `transport`；`grpc → dubbo
   sidecar` 只允许执行层按 3.3 显式开启、单次生效并回写 `transport_fallback`，用例内容不因回退改写
10. 与已批准库 diff（见 **3.2.3**），生成 proposal；校验失败写 `validation.json`，退出非 0，
    **store 字节不变**
11. 人工审核后由 `cases approve` 重新推导、检查乐观锁并写库；只有 approved 用例进入 C6
12. **角色与范围校验**：`coverage_role` 必须与画像一致；alias 用例超出冒烟范围时写
    `alias_over_coverage` 警告并在审核文件展示（不默认删除，但不得计入业务覆盖）；
    `--transport-scope=primary` 时 alias 草案项不进入 proposal，汇总为
    `transport_alias_skipped`；无 primary 的 `business_key` 直接判错

**用例类别（审核清单）**：

| level | 类别 | 驱动源 | 断言设计 |
| --- | --- | --- | --- |
| business | 功能正向 | 规则 + 契约源 | 状态码 + 关键字段 + 业务结果 |
| business | 业务约束正/负/边界 | BR 规则 | 规则 `text` 中的条件与结果；quote 必须可查 |
| business | 契约边界/负向 | CR 规则 | required/type/enum/min/max 边界 ±1 |
| code | 变更回归 | changed_symbols + business 用例 | 复用 business 断言口径，补变更影响面 |
| code | 变更边界/错误分支 | CR + 未覆盖的 BR 分区 | 只补 business 未覆盖的输入分区，不得改期望 |

> 表中的“状态码”按 transport 取原生状态：HTTP `status_code`、gRPC `grpc_status`、Dubbo `rpc_status`。三者不可互相替代，也不做“非 HTTP 也用 200 判断”的归一。

**关键设计决策**：

- **单请求边界**：v4 的 business 用例允许声明 `preconditions`（经 C5 造数），但仍是一条请求；
  多步骤流程、跨用例捕获、鉴权链路属后续里程碑。需要前置状态但无法造数的需求写入
  `uncovered_requirements`（如 `needs_c5_state_setup`），不伪造可执行用例
- **代码级用例不以 diff 为 oracle**：diff 只决定“测哪里”和“哪些已批准 business 用例要回归”；
  断言值只能来自规则、契约源或已批准 business 用例。找不到依据就写
  `change_without_oracle`，不生成用例
- **business 优先**：同一 `(endpoint, case_key)` 只允许一条；code 用例与 business 期望冲突时
  以 business 为准，code 必须修改或废弃
- **Mock 绑定是用例的一部分**：`mock_bindings` 写入用例并进入 fingerprint（换分支必须重审）；
  `dep_id` 只认画像里的稳定 id（`http:{downstream_key} {METHOD} {path}`），不写客户端类名、方法名或全限定类名，
  避免包名或客户端实现变化导致用例无意义 churn。审核文件必须展示每条下游的绑定分支与 Mock 命中预期
- **规则确认隐含在用例批准中**：审核文件展示规则原文、`authority`、`source.kind` 与
  `reasoning`；批准引用 B 级规则的用例即视为确认该规则。未被任何 approved 用例引用的
  B 级规则在报告中列为“未确认规则”
- **审核不可绕过**：框架不提供自动批准；`cases approve` 必须显式 `--reviewer`；demo 的自动
  批准只用于演示，且只批准 `new/update/revive`，不批准 `deprecate`
- 拒绝项不进入 store；需要改用例时修改草案后重新 import，**不能在审核文件里直接改用例内容**
- 每条用例携带 `rule_refs`、`requirement_refs`、`change_refs`，报告可从失败用例反查到规则、
  文档 section 与变更
- **transport 是画像事实，不是用例选项**：用例不能自行声明或改写 `transport`；gRPC/Dubbo 用例
  只使用各自的原生请求/断言字段，不把 RPC 结果塞进 HTTP `body_field`
- **用例不承载回退语义**：`grpc → dubbo sidecar` 是执行期策略，若发生，报告记录
  `transport_fallback` 与 `degraded`；用例库保持原 `transport`，避免一次回退污染长期资产
- **gRPC 下游不可 Mock 是不可执行边界**：依赖 gRPC 下游的用例不得通过审核进入执行，
  只能显式排除或标 `blocked`；审核文件必须展示被排除原因，不能只写“无下游”。Dubbo 下游可进入执行，
  但必须带 agent 前提与 `serialization_verified=false` 声明
- **业务覆盖归 primary，transport 覆盖归 alias**：alias 冒烟只证明“这个协议能按契约调通”，
  不重复证明业务规则；把 alias 冒烟算进业务覆盖会虚高指标
- **默认不放弃任何协议端点**：`all` 模式下 alias 至少跑冒烟；只有显式 `--transport-scope=primary`
  才跳过，且报告写 `transport_alias_skipped` + `degraded`，不静默省略
- **`equivalent` 跳过必须有指纹**：`adapter_fingerprint` 相同才允许，且跳过项在报告中可查；
  指纹缺失时按冒烟执行

**门禁**：见 **3.1.6 / 3.2.6**。C4 自身保证：校验失败不落库；proposal 不改变 store；approved
用例原生状态断言非空率 100%（HTTP `status_code` / gRPC `grpc_status` / Dubbo `rpc_status`）；
未覆盖需求/规则/变更必须有 reason；`change_without_oracle` 单列；`mock="unsupported"` 的下游
不得被任何 approved 用例绑定；gRPC streaming/oneway、Dubbo 2.6、接口在 jar、动态查找用例
不得进入 store（应停留在 `unsupported`）；每个 `business_key` 只有一个 primary；alias 至少
一条成功冒烟，`transport_alias_skipped` 与 `equivalent` 跳过项均需在报告列出；alias 用例
不得计入业务规则覆盖

---

### C6 用例编码

| 项  | 内容                                  |
| -- | ----------------------------------- |
| 职责 | 将用例集编译为可读、可改、可入库的 pytest 代码资产       |
| 输入 | cases.json（approved 用例运行投影）、api_profiles.json（transport 与调用字段）、data_plan.json |
| 输出 | `tests/` 目录：conftest.py + 每接口一个测试文件 |

**生成物结构约定**：

- `conftest.py`：
  - HTTP 客户端 fixture、造数器 fixture、DB 断言 fixture、下游 Mock fixture
    （HTTP 优先配置 URL 覆盖，代理仅作兜底）、用例级回收
  - Mock 会话 fixture：先取 `mock:<namespace>` lease，再 `POST /_admin/sessions` 注册该用例的
    `mock_bindings`；用例结束（含断言失败/异常）在 `finally` 中读取会话证据、drain、
    `DELETE /_admin/sessions/{id}` 并释放 lease。会话建立必须早于请求、关闭必须晚于断言，
    不能颠倒；拿不到 lease 时等待，不覆盖他人的 active session
  - gRPC fixture：按 `descriptor_source` 加载 descriptor、建立 channel、调用 unary 方法；
    不依赖运行时 reflection
  - Dubbo fixture：以子进程启动 sidecar，JSON over stdio 传 `interface/method/param_types/
    arguments/attachments/timeout`，回收结构化结果；**调用 SUT 时不 Attach、不装 Java agent**
  - SUT 启动 fixture（仅 Dubbo 下游 Mock）：启动前删除旧心跳文件，启动时注入
    `-javaagent:astsight-agent.jar=heartbeat=<abs path>`，轮询心跳文件直到 `active=true`
    （`pid/jvm_start_time` 匹配、心跳新鲜、`filter_instantiated>0`）再开始用例；超时/残留/过期
    → `agent_not_active`，整批阻断。agent 只拦截 SUT 出站 Dubbo 调用，不参与调用 SUT
- `test_<接口>.py`：类=接口，方法=用例；docstring 携带 `TC-NNNN`、`level`、`transport`、
  `rule_key` 与需求 quote 溯源
- 用例体按 transport 生成：HTTP 走 `requests`/`httpx`；gRPC 走生成 stub 或动态 unary 调用；
  Dubbo 走 sidecar 客户端。断言使用各自原生 kind，不把 RPC 结果转成 HTTP 语义
- 依赖链**代码化**：上游响应值通过 fixture 上下文传递（可读优先），不做运行时字符串替换
- **人工保护段**：`MANUAL BEGIN/END` 标记内的内容在二次生成时保留
- 生成代码头部带生成时间、源契约版本、transport、框架版本，便于审计

**关键设计决策**：

- **代码是资产不是黑盒**：生成后必须过人工 review 才能进用例库（幻觉在管道中放大，审核门不可省）
- **Mock 配置由 approved 用例的 `mock_bindings` 推导**：只为用例实际绑定的下游生成覆盖配置；
  画像中有、用例未绑定的下游按默认 `success` 显式生成，不允许"漏配即透传"。配置键取自画像的
  `mock.property`（C3 从 SUT 实际配置中提取），不硬编码任何客户端框架的键名；提取不到则走
  发现覆盖或代理，仍不可行标 `unsupported`
- **只生成已批准的 transport 调用方式**：`transport=http|grpc|dubbo` 来自画像；codegen 不做
  协议探测、不做 gRPC→sidecar 回退。回退只在执行期由显式开关触发，见 C7
- **Dubbo 调用只走 sidecar 泛化调用**：`ReferenceConfig<GenericService>` + Hessian2 +
  精确 `param_types`；重载必须在用例中显式给出参数类型。sidecar 是独立 Java 进程，
  通过 stdio 交互，不与 pytest 进程共享 JVM
- **gRPC 代码只支持 unary**：descriptor 缺失的端点不会进入用例库，codegen 不生成
  “尝试 reflection”的兜底代码
- 二次生成策略：契约未变的用例不重写；契约变化的用例生成 diff 报告，由人决定合并

**异常与边界**：LLM 参与的代码片段（复杂场景编排）必须经语法校验+导入校验双重自检，不通过则回退模板生成；sidecar 不可用/版本不兼容时该接口用例整体标 `blocked`，不回退成 HTTP；gRPC descriptor 与 proto 版本不一致时拒绝生成并在报告列出；`mock="unsupported"` 的下游不生成任何 Mock fixture

**门禁**：生成代码可运行率 ≥95%（语法+导入+fixture 引用）；断言非空率 100%；三种 transport 的 fixture 必须能通过最小 smoke 调用（HTTP 200 / gRPC OK / Dubbo sidecar 返回结构化结果）；任何生成物不得包含 `Attach`/`javaagent`/字节码改写

---

### C7 执行验证与运行编排

> 运行层 = 数据冲突检测 → 并行调度 → 失败归因与重试 → 报告合并。目标是把"能跑"升级为"生产级稳定执行"：既不因数据竞争产生假失败，也不因偶发环境抖动污染门禁结论。

| 项  | 内容                                               |
| -- | ------------------------------------------------ |
| 职责 | 检测用例数据冲突 → 规划并行执行 → 执行 → 归因 → 有界重试 → 合并报告        |
| 输入 | cases.json（approved 用例运行投影）、data_plan.json、pytest 用例代码、运行环境配置 |
| 输出 | `report.json`（合并后的最终报告）+ 原始分片执行日志                |

#### C7.1 数据冲突检测

**目的**：并行前判断多个用例是否争用同一份数据，避免数据竞争导致的假失败。

**冲突判定维度**（数据维度，非资源维度）：

| 维度     | 判定                              | 冲突处理          |
| ------ | ------------------------------- | ------------- |
| 同一主键行  | 多用例操作同一 `(表, 主键值)`              | 冲突            |
| 同一唯一键值 | 共享种子数据中的同一唯一键（如固定 `sku_id`）     | 冲突            |
| 同一关联数据 | 多用例 share 同一上游 fixture（如共用同一订单） | 冲突            |
| 同一 Mock 命名空间 | 用例带 `mock_bindings` 且共享同一个 `mock:<namespace>` | 冲突：mock-bound 用例串行（v1 单 active session） |
| 只读共享   | 多用例仅读同一数据、无写                    | **不冲突**，可安全并行 |

**冲突数据契约** `conflicts.json`：

```json
{
  "case_id": "TC-B-001",
  "resources": [
    {"table": "orders", "key": "order_no=ORD001", "mode": "write"},
    {"table": "products", "key": "sku_id=S001", "mode": "read"}
  ],
  "group": "share-orders-001",
  "mock_namespaces": ["order-svc"],
  "parallel_group": "G1"
}
```

**核心逻辑**：

1. 从 DataPlan 与用例 setup 提取每个用例的**数据资源清单**（表 + 键 + 读写模式），从 `mock_bindings` 提取 `mock:<namespace>` 资源
2. 两两比对：任一 `写` 资源交集，或共享同一 Mock 命名空间且双方都有 `mock_bindings` → 判冲突
3. 冲突用例聚合成**分组**（`group`），组内串行、组间并行；无冲突的用例进入默认并行池
4. 冲突分组的执行顺序按外键拓扑排序（保证造数前置）；Mock 串行组内的顺序确定且可重放
5. `mock_namespaces` 取本 run 的 `sut_id`（不写字面量 `"default"`）；冲突图只在单个 run 内比较，跨 run 的隔离由 5.5 保证

**关键设计决策**：

- 冲突判定只看**数据维度**，不看接口维度——两个不同接口操作同一订单号同样是冲突
- 只读共享不冲突，这是并行度的关键来源：多数查询/列表用例可安全全并行
- **Mock 命名空间是独占资源**：v1 一个命名空间只有一个 active session，因此带 `mock_bindings` 的用例必须串行；这不是数据冲突，但同样是竞争资源，必须进入冲突图
- 无 `mock_bindings` 的用例可并行；若其意外调用到已 Mock 的下游，由 `mock_unexpected_dep` 捕获，不静默成功
- 冲突检测是纯确定性逻辑（基于数据契约比对），不放 LLM

#### C7.2 并行调度

**调度模型**：

```
用例全集
   │ 冲突检测
   ▼
┌─────────────┬─────────────┬─────────────┐
│ 并行组 G1    │ 并行组 G2    │ 串行组 S1    │  ← 组间并行
│ (只读用例池) │ (独立写用例)  │ (冲突用例串行)│
└─────────────┴─────────────┴─────────────┘
```

**调度策略**：

- **组间并行、组内串行**：冲突用例在同一串行组内按序执行，组与组之间并行
- **Mock 租约**：执行 mock-bound 用例前必须先获取 `mock:<namespace>` lease；拿到 lease 才能创建 session，执行结束（含失败/异常）在 `finally` 释放。拿不到 lease 的用例等待，不得抢占/覆盖已有 session。租约按 `sut_id` 分域（`namespace = sut_id`），不同 SUT 的并发 run 不共享租约
- 并发度可配（`--workers N`，默认 = CPU 核数）；受外部资源限制时降级（数据库连接池上限、被测服务压力、Mock 命名空间独占）
- 依赖链（produces→consumes）约束优先于并行度——有依赖的用例必须排在产出方之后

**关键设计决策**：

- 并行度不是越高越好：需按**被测服务的并发承受能力**设上限，避免把被测系统打挂后误判为失败（这本身就是一种需要归因的场景）
- **v1 不假装 mock-bound 用例能并行**：串行是方案 A 的明确取舍；后续用“每 worker 独立 SUT + Mock 命名空间”提升并行度，届时 lease 粒度从全局命名空间改为 `worker:namespace`
- 调度结果是**可重放的**（确定性分组），保证失败重跑时分组与顺序不变

#### C7.3 失败归因与自动重试

**统一错误信封**（执行层把三种 transport 的失败归一后再归因，归因规则不直接读原始协议对象）：

```json
{
  "transport": "http | grpc | dubbo",
  "status": "OK | ERROR",
  "code": "HTTP 500 | gRPC UNAVAILABLE | dubbo TIMEOUT",
  "message": "normalized text",
  "retryable_hint": false,
  "adapter": "grpc_descriptor | dubbo_sidecar | http_client"
}
```

| transport | 原始信号 | 归一字段 |
|---|---|---|
| `http` | HTTP status、连接异常、读取超时 | `code=HTTP {status}` 或 `HTTP timeout/connect` |
| `grpc` | `status.code`（`OK`/`UNAVAILABLE`/`DEADLINE_EXCEEDED`/`UNIMPLEMENTED`/`INVALID_ARGUMENT`…）、`status.details`、metadata | `code=gRPC {CODE}` |
| `dubbo` | sidecar 返回的 `rpc_status`（`OK`/`TIMEOUT`/`RPC_EXCEPTION`/`SERIALIZATION`）、`exception_type`、`message` | `code=dubbo {rpc_status}`，异常类型单列 |

**归因分类**（区分"被测系统问题"与"测试侧问题"）：

| 归因类别            | 判定信号                     | 是否可重试       |
| --------------- | ------------------------ | ----------- |
| 被测系统缺陷（SUT bug） | 5xx、断言失败且响应符合契约、业务码非预期；gRPC `UNIMPLEMENTED`/`INVALID_ARGUMENT`（契约内）；Dubbo `RPC_EXCEPTION` 且异常为业务异常 | **否**（真实缺陷） |
| 测试侧数据问题         | 4xx 且源于造数缺失/脏数据          | 是（修复后重试）    |
| 测试侧用例问题         | 断言写错、参数构造错；Dubbo `SERIALIZATION`/`NoSuchMethod` 且由 `param_types` 或 payload 不匹配导致 | 是（修复后重试）    |
| 环境抖动            | 连接超时、DNS、网关 502、DB 锁等待超时；gRPC `UNAVAILABLE`/`DEADLINE_EXCEEDED`；Dubbo `TIMEOUT` 且下游健康检查异常 | 是（原样重试）     |
| 数据竞争残留          | 并发下偶发冲突、死锁回滚             | 是（降级串行重试）   |
| 适配器错误（framework） | gRPC descriptor 加载失败、sidecar 启动失败/版本不兼容、stdio 协议错误 | 否（回填框架缺口，**不计入 SUT 缺陷**） |

**transport 回退归因**：

- 回退只在 `transport_class="dubbo"` 且显式 `--allow-transport-fallback` 时发生，`grpc → dubbo sidecar` 单次、不可链式；报告写 `transport_fallback` 并标 `degraded`
- 回退后通过：计为通过，但保留 `degraded`，不得用来证明 gRPC 路径正确
- 回退后失败：先归因 `adapter_error`（framework），除非响应证明是 SUT 业务缺陷；不得把适配器失败算成 SUT bug
- 禁止 `grpc → http`、`dubbo → http` 等跨形态回退；无可用路径时保持 `unsupported`

**重试规则**：

1. 归因 → 仅"非被测系统问题"进入重试
2. 修复类（数据/用例）：应用修复动作后重试
3. 环境抖动类：原样重试，可加退避
4. **重试次数上限 3**（含首次执行共 4 次机会）；第 4 次仍失败 → 定为终态失败
5. 每次重试记录 `attempt`、`reason`、`action`，全程留痕

**重试数据契约**（报告内嵌）：

```json
{
  "case_id": "TC-B-003",
  "final_status": "pass",
  "attempts": [
    {"n": 1, "status": "fail", "reason": "env_flake", "action": "retry_as_is"},
    {"n": 2, "status": "pass"}
  ]
}
```

**关键设计决策**：

- **重试有界（≤3）且全程留痕**——重试是手段不是兜底，无限重试会把门禁结论拖成"永远在跑"
- 归因判定本身是确定性规则 + 阈值（如超时类错误码集合、DB 锁等待关键词），不放 LLM
- 归因不确定时**不重试**，标记 `unknown` 交人工——错误的重试会掩盖真实缺陷
- 数据竞争残留类重试**降级为串行**，验证是否并发导致的假失败
- **三种 transport 先归一、后归因**：归因规则只消费统一错误信封；gRPC/Dubbo 的原始错误码不直接映射成 HTTP 语义，避免把 `UNAVAILABLE` 当成 5xx 计入 SUT 缺陷
- **适配器错误不算 SUT 缺陷**：descriptor/sidecar/stdio 失败属于框架缺口，回填 `action_target=framework`；把它算成 SUT bug 会污染门禁结论
- **回退不改变用例的 transport**：回退是一次执行策略，报告记录 `from/to`，用例库与画像保持原判定

**归因双视角（重试视角 vs 根因归属视角）**：

本节的"归因分类"是从**重试视角**划分（决定"要不要重试、怎么重试"）。A5 诊断 Agent 另有一套**根因归属视角**的六类根因（决定"问题归谁、流向何处"）。两者不是一回事，是同一个失败的**两个正交切面**，映射如下：

| 重试视角分类（C7.3） | 根因归属视角（A5 六类根因） | action_target | 说明 |
|---|---|---|---|
| 被测系统缺陷（SUT bug） | `code_defect` | user | 真实缺陷，反馈用户修代码 |
| 测试侧数据问题 | `data_error` | framework | 造数能力缺口，回流框架升级 |
| 测试侧用例问题 | `case_error` | user | 用例/断言写错，反馈用户改用例 |
| 环境抖动 | （非根因，是重试理由） | — | 只决定"原样重试"，不参与归属 |
| 数据竞争残留 | （非根因，是重试理由） | — | 只决定"降级串行重试"，不参与归属 |
| 适配器错误（gRPC descriptor / Dubbo sidecar / 回退失败） | `adapter_error` | framework | transport 适配能力缺口，回流框架升级；不计入 SUT 缺陷 |
| —（C7.3 未单列） | `mock_error` | framework | Mock 语义模型/会话/agent 覆盖不足，回流框架升级 |
| 环境不满足（`No provider available`、注册中心不可达、agent 未激活） | `env_error` | framework | 测试环境不满足前提，用例 `blocked`；不计入 SUT 缺陷 |
| —（C7.3 未单列） | `change_induced` | user | 变更导致行为变化，反馈用户确认 |

> 关键：**重试视角的"环境抖动/竞争残留"不是根因，而是重试的触发信号**——它们不需要归属到 user/framework，重试通过即消除。真正的根因归属只看六类：`code_defect/case_error/data_error/mock_error/change_induced/adapter_error`，这六类才映射到 `action_target`（user/framework），进入 6.5 缺陷案例库的只有 `framework` 侧（`data_error`/`mock_error`/`adapter_error`）。详见 `agent-architecture.md` A5 章节与 `prompts/a5_result_diagnosis.md`。

#### C7.4 报告合并

**目的**：把多分片、多 attempt 的执行结果合并为一份终态报告。

**合并规则**：

1. 以 `case_id` 为聚合键，取最后一次 attempt 的终态
2. 统计口径分离：
   - **原始通过率**：首次执行通过数 / 总数
   - **最终通过率**：重试后通过数 / 总数（含重试恢复的用例）
   - **真实缺陷数**：归因为 SUT bug 的用例数（**门禁只认这一项**）
3. 报告输出维度：按用例类别、按接口、按归因类别三个切面汇总
4. 报告必须携带 `instance` 块（`sut_id/run_id/namespace/ports[]/locks/isolation`，见 5.5）；一次 run 只汇总一个 SUT，跨 SUT 的汇总由 CI 按 `sut_id` 自行合并，框架不做多租户合并报告

**报告数据契约**：

```json
{
  "summary": {
    "total": 40,
    "final_pass": 38,
    "final_pass_rate": 0.95,
    "first_pass_rate": 0.88,
    "sut_defects": 2,
    "retried_recovered": 3,
    "unknown_attribution": 1
  },
  "by_category": {"functional": {"pass": 10, "fail": 0}, "...": {}},
  "by_transport": {
    "http": {"total": 30, "pass": 29, "fail": 1},
    "grpc": {"total": 6, "pass": 5, "fail": 1},
    "dubbo": {"total": 4, "pass": 4, "fail": 0, "sidecar": 4}
  },
  "by_endpoint": {"POST /api/orders": {"pass": 8, "fail": 1}},
  "by_reason": {"sut_bug": 2, "env_flake": 0, "data_issue": 0, "adapter_error": 0},
  "unsupported": {
    "count": 2,
    "items": [{"endpoint": "grpc:/pkg.Legacy/Stream", "reason": "streaming"},
              {"endpoint": "dubbo:com.acme.LegacyApi#call", "reason": "dubbo_2_6"}]
  },
  "transport_fallback": {
    "used": 1,
    "items": [{"case_id": "TC-B-012", "from": "grpc", "to": "dubbo_sidecar",
               "reason": "descriptor_unavailable"}],
    "degraded": true
  },
  "transport_coverage": {
    "scope": "all",
    "business_keys": 6,
    "primary": 6,
    "alias": 3,
    "independent": 1,
    "alias_smoke": {"total": 3, "pass": 3, "fail": 0},
    "alias_skipped": [{"endpoint": "dubbo:com.acme.OrderApi#create?group=order&version=1.0.0",
                       "role": "alias", "reason": "transport_scope_primary"}],
    "equivalent_skipped": [{"endpoint": "grpc:/acme.order.v1.OrderService/CreateOrder",
                            "alias_of": "POST /api/orders",
                            "adapter_fingerprint": "sha256:..."}],
    "degraded": false
  },
  "instance": {
    "sut_id": "order-svc",
    "run_id": "20261101T101500123Z-a1b2",
    "namespace": "order-svc",
    "ports": [
      {"component": "http_mock", "endpoint": "127.0.0.1:43127", "owner": "order-svc/48120/http_mock"},
      {"component": "dubbo_agent_admin", "endpoint": "127.0.0.1:43128", "owner": "order-svc/48120/dubbo_agent_admin"}
    ],
    "heartbeat_path": "/tmp/astsight/order-svc.heartbeat.json",
    "admin_token_fingerprint": "sha256:...",
    "locks": {"codegraph": "acquired", "case_store": "not_required"},
    "isolation": {"port_allocation": "ephemeral", "namespace": "sut_id", "db_scope": "run", "degraded": false}
  },
  "mock_sessions": {
    "sut_id": "order-svc",
    "namespace": "order-svc",
    "egress_guard": "network",
    "agent": {
      "mode": "javaagent",
      "version": "0.1.0",
      "active": true,
      "heartbeat_path": "/tmp/astsight/mock-agent/order-svc.heartbeat.json",
      "pid": 48213,
      "filter_heartbeat": true,
      "filter_instantiated": 2,
      "dubbo_version": "3.2.10",
      "serialization_verified": false
    },
    "opened": 12,
    "closed": 12,
    "conflicts": 0,
    "missing": 0,
    "expired": 0,
    "hits": 18,
    "env_errors": 0,
    "not_used": [],
    "unexpected_dep": [{"case_id": "TC-B-007", "dep_id": "sms-client#send"}],
    "branch_not_found": [],
    "late_calls": [{"case_id": "TC-B-003", "session_id": "s-003",
                    "dep_id": "http:svc:pay-service POST /gateway/pay", "delay_ms": 640}],
    "evidence": [{"case_id": "TC-B-001", "session_id": "s-001", "status": "closed",
                  "bindings": [{"dep_id": "http:svc:pay-service POST /gateway/pay", "branch": "success",
                                "binding_source": "case", "calls": 1},
                               {"dep_id": "dubbo:com.acme.StockApi#deduct", "branch": "insufficient",
                                "binding_source": "case", "calls": 1, "mock_mode": "javaagent"}]}],
    "degraded": false
  },
  "flaky_candidates": ["TC-B-003"],
  "token_usage": {
    "total_prompt": 182400,
    "total_completion": 96300,
    "total": 278700,
    "by_node": {
      "draft_generation": 272700,
      "A5_result_diagnosis": 6000
    },
    "by_model": {"deepseek-v3": 278700},
    "estimated_cost": {"currency": "CNY", "amount": 0.84}
  },
  "gate": {"threshold": 85, "verdict": "pass"}
}
```

**关键设计决策**：

- **门禁只认"真实缺陷数"**，不认"最终通过率"——重试恢复的用例不计入通过率水分，SUT 缺陷不能被重试掩盖
- `flaky_candidates`（首次失败、重试通过且原因为环境抖动/竞争残留）单独列出，作为**被测系统稳定性信号**上报，而非简单归入通过
- **token 消耗统计**：按节点、按模型两个切面汇总，供成本治理与用量预警。统计范围覆盖 A1 规则抽取、C4 草案生成、C5a/C5b 语义候选与 A5 结果诊断；C1 的文档切分/哈希/契约解析、C2/C3、C5 的结构解析/FK/拓扑/校验/SQL 渲染与执行工具为确定性，零 token 不统计
- **报告保留原始分片日志**，供归因复核
- **三种 transport 分形态统计**：`by_transport` 必须给出总数/通过/失败；Dubbo 额外记录 `sidecar` 次数；不得把 gRPC/Dubbo 合并进 HTTP 统计
- **`unsupported` 与 `transport_fallback` 必须显式出报**：前者不计入已覆盖，后者使报告标 `degraded`；缺少这两项视为报告不完整
- **`transport_coverage` 必须显式出报**：写出本次 scope、每个 `business_key` 的 primary/alias/independent 数量、alias 冒烟结果、被跳过的 alias 与 `equivalent` 项；`--transport-scope=primary` 时 `degraded=true`
- **业务覆盖与 transport 覆盖分开统计**：`transport_coverage.alias_smoke` 只表示协议适配验证，不得计入业务规则/需求覆盖率
- **`mock_sessions` 必须逐用例出证据**：每条 mock-bound 用例都要带 `session_id`、`bindings[]`（含 `binding_source`、`mock_mode`）、命中次数与关闭状态；没有会话证据的 Mock 用例按 `weak_oracle` 处理，不允许“跑过了就算 Mock 生效”
- **Dubbo agent 状态必须出报**：`agent.mode/version/active/heartbeat_path/pid/filter_heartbeat/filter_instantiated/dubbo_version` 与 `serialization_verified=false` 必须可见；`active=false` 时整批阻断，不允许“没拦截成功还继续跑”
- **Mock 失败子码必须显式归因**：`mock_session_conflict`、`mock_session_missing`、`mock_unexpected_dep`、`mock_branch_not_found`、`mock_response_invalid` 为阻断类（框架侧 `mock_error`，不计 SUT 缺陷）；`env_error`（provider 不可用/注册中心不可达）为环境类，用例 `blocked`；`mock_late_call`、`mock_session_expired` 为告警类并使报告 `degraded`；`mock_not_used` 为失败类（绑定 dep 命中 0 次），不允许按通过处理；任何一类都不允许被静默吞掉或折算成普通断言失败
- **`egress_guard` 必须如实标注**：`network` 表示测试环境已隔离真实下游，是"零透传"的强保证；`none` 表示仅靠 SUT 配置指向 Mock，报告必须 `degraded`，并写出 SUT 配置来源，不能宣称已验证无透传
- **`late_calls` 与 `expired` 是 Mock 语义泄漏信号**：它们通常意味着 drain 窗口不足或 SUT 存在异步/重试调用；报告单列，供 C7.3 归因与 fixture 配置调优
- **mock-bound 用例的串行调度要在报告里可见**：报告说明本次因 Mock 会话串行的用例数（`mock_sessions.opened`）与因此增加的耗时，不把“慢”隐藏成执行器性能问题
- **门禁仍只认真实缺陷数**：`adapter_error`、`unsupported`、回退降级、`mock_error` 都不进入 SUT 缺陷计数，但也都不允许被隐藏

### C7.4.1 Token 消耗统计

> 目的：把"AI 测试全流程"的算力成本透明化，支撑成本治理、模型选型对比、用量预算预警。这是质量负责人视角的延伸——测试不仅要"测得对"，还要"测得起"。

**统计口径**：

| 维度 | 说明 |
|---|---|
| **按节点** | 草案生成（框架外 Skill/模型） / A5 结果诊断分别统计，定位"成本大头" |
| **按模型** | 不同 model 各自累加，支撑多模型成本对比与选型 |
| **按调用类型** | prompt（输入）/ completion（输出）分开，输出 token 通常是成本主因 |
| **按用例** | 可选细粒度：每条用例关联其生成/诊断消耗，定位"贵用例" |

**采集机制**：

1. **LLM 层统一埋点**：token 用量在 LLM 网关层采集（而非各 Agent 自行上报），保证口径一致、不遗漏。返回的 `usage.prompt_tokens / completion_tokens` 落一条 `TokenUsage` 记录
2. **节点归属标记**：每次调用带 `node` 标签（draft/a5），由调用方或编排层注入
3. **重试计入**：A5 诊断的重试调用也计入（它们同样消耗 token），并在 `by_node` 中可区分"首次"与"重试"（可选）

**成本估算**：

- 维护一张**模型单价表**（`{model: {prompt_price, completion_price}}`，可配置），token × 单价 = 估算成本
- 单价表是**估算**，非精确计费——真实账单以云厂商为准，报告标注 `estimated_cost`
- 未配单价的模型 → 只统计 token，`estimated_cost` 置 null

**关键设计决策**：

- **采集放网关层，不放 Agent 内部**——Agent 只负责调用，token 计量是横切关注点，网关统一埋点才能保证"零遗漏、口径一致"
- **token 统计不进门禁**——它是观测指标，不是质量判定依据。成本超预算触发的是"预警"而非"阻断"（除非用户显式配置了成本上限门禁）
- **重试是成本放大器**：一次 A5 诊断失败重试 3 次，token 消耗 ×4。报告里应能看出"重试花了多少 token"，这本身就是"该不该优化归因准确率"的经济依据

**异常与边界**：某次调用未返回 usage（流式中断/上游异常）→ 该次 token 记为 0 并标记 `usage_missing`，不猜；成本单价未配置 → `estimated_cost=null`，仅输出 token 数

**门禁（可选）**：成本超预算预警阈值可配置，触发即告警但默认不阻断

**异常与边界**：重试次数耗尽仍失败 → 终态失败 + 完整 attempt 链；归因未知 → 不进重试、单独成列；调度阶段因外部资源不足无法并行 → 显式降级为串行并在报告标注

**门禁**：归因准确率 ≥85%（抽样人工复核）；假失败率（重试后证明非 SUT 问题）应持续低于阈值并作为回归监控项

---

## 四、数据模型总览

```
需求文档 + 契约源（OpenAPI/Spring MVC 注解/gRPC proto/Dubbo 接口）──C1──▶ Context + RuleSet ──C4a──▶ 业务级草案 ─┐
                                                                  ├─▶ Proposal → 审核 → CaseSet ──C6──▶ TestFile ──▶ C7
ChangedAPI[] ──C2/C3──▶ Profile（含 transport 与下游）──C5──▶ DataPlan ──C4b──▶ 代码级草案 ┘
```

| 实体           | 来源能力   | 消费能力     | 关键字段                                                          |
| ------------ | ------ | -------- | ------------------------------------------------------------- |
| Context      | C1     | C3/C4    | context_id, documents[].sections, endpoints（含 transport）, profiles, limits |
| RuleSet      | C1     | C4/报告   | ruleset_id, version, prompt{name,version,model}, rules[], questions[], gaps[], conflicts[] |
| Rule         | C1     | C4/报告   | rule_key, rule_id, text, type, fields, constraints, source, authority, priority, fingerprint |
| ContractRule | C1     | C4/报告   | rule_key, rule_id, endpoint, field, constraint, authority=A   |
| CaseSet      | C4     | C6/C7/报告 | schema_version, store_version, ruleset{version,hash,id}, cases[], history[], review_log[], requirements[], uncovered_reasons |
| CaseEntry    | C4     | C6/C7    | id, case_key, level, transport, endpoint, coverage_role, request（按 transport）, preconditions, mock_bindings（http/dubbo）, asserts（按 transport）, assert_strength, rule_refs, requirement_refs, change_refs, status, version, fingerprint |
| CaseReview   | C4     | 报告/人     | decision, by, at, note, fingerprint                            |
| ReviewEvent  | C4     | 报告/人     | proposal_id, case_key, action, decision, reviewer, at, fingerprint |
| RequirementDoc | C1/C4 | 报告      | doc, version, sections[]                                       |
| ChangedAPI   | C2     | C3/C4    | transport, endpoint, handler, business_key, change_kinds, contract_change{source,status,details}, confidence, trace, external_dependency_change, unsupported_reason |
| Profile      | C3     | C4/C5    | transport, transport_class, transport_evidence, business_key, coverage_role, alias_of, adapter_fingerprint, contract_source, params.constraints, db_mapping, dependencies, grpc/dubbo 调用字段, downstream_deps（含 HTTP/gRPC/Dubbo 下游）, db_lineage[] |
| CoverageGroup | C3     | C4/C7    | business_key, endpoints[], primary, aliases[], independents[], adapter_fingerprint, evidence |
| TransportAdapter | C3/C6 | C7    | transport, grpc descriptor_source / dubbo sidecar{version,serialization,registry,direct_url}, status(ready/unsupported), evidence |
| DBLineage    | C3     | C5       | edge(READS/WRITES/MAPS_TO/FK), table, column, evidence        |
| DataCatalog  | C5a    | C4/C5b   | catalog_id, tables/columns, foreign_keys, tables_order, states, field_semantics, limits |
| DataPlan     | C5b    | C6/C7    | plan_id, catalog_id, ruleset_hash, case_id, run_scope, tables_order, fixtures, target_state, fk_links(源自血缘), preconditions, 参数化 statements/teardown |
| TestCase     | C4     | C6       | approved CaseEntry 的执行投影：id, transport, endpoint, request, asserts, requirement_refs |
| TestFile     | C6     | C7/人     | 保护段、transport、溯源头                                                  |
| ConflictSet  | C7.1   | C7.2     | resources, group, parallel_group                              |
| MockSemantic | C3.5.1 | C4       | http/dubbo 下游：dep_id（HTTP 为 `http:{downstream_key} {METHOD} {path}`，见 C3.5.5）, semantic_branches[], mock.strategy/property, 用例绑定；grpc 下游 v4.3 为 `mock=unsupported`，v4.4 起为 `mock.strategy="grpc_server"`（见 C3.5.4） |
| MockAgent    | C3.5.3 | C6/C7    | mode(javaagent/classpath), version, active, sut_id, namespace, admin_endpoint, token_fingerprint, heartbeat_path, pid, jvm_start_time, filter_heartbeat, filter_instantiated, dubbo_version, supported_matrix, serialization_verified |
| MockServer   | C3.5.4/C3.5.5 | C6/C7 | transport(http/grpc), mode(embedded/gripmock), version, sut_id, namespace, endpoint(host:port), admin_endpoint, token_fingerprint, route_modes[](path_prefix/host_group/method_path), route_count, routes[], health, history_path, started_at, pid/container_id；gRPC 另有 binary{version,sha256,platform,source}, descriptor_sha256, stub_count, serialization_verified=wire_roundtrip；HTTP 另有 injection 证据与 `tls_not_supported` 清单 |
| MockRoute    | C3.5.5 | C6/C7    | route_key, dep_id, method, path_key, host, target, strategy, route_match, source |
| MockCall     | C3.5.5 | C7/报告   | call_id, session_id, case_id, route_key, dep_id, route_match, method, original_path, query_keys, request_headers(脱敏), body_sha256, body_bytes, branch, response_status, delay_ms, drop_mode, result(ok/unknown/ambiguous/unbound/branch_missing/late), at |
| MockSession  | C6     | C7/报告   | session_id, sut_id, namespace, case_id, bindings[{dep_id,branch,binding_source,mock_mode}], status(open/closed/expired), opened_at, closed_at, ttl_ms, hits[], late_calls[], reason |
| SutInstance  | 5.5    | C3.5/C5/C7/报告 | sut_id, run_id, namespace, ports[{component,endpoint,owner}], heartbeat_path, admin_token_fingerprint, locks{codegraph,case_store}, isolation{port_allocation,namespace,db_scope,degraded} |
| Attempt      | C7.3   | C7.4     | n, status, reason, action, action_target(user/framework), error_envelope, transport_fallback |
| TokenUsage   | LLM 网关 | C7.4     | node, model, prompt_tokens, completion_tokens                  |
| Report       | C7.4   | 人/CI     | 最终通过率、instance、by_transport、transport_coverage、mock_sessions、sut_defects、unsupported、transport_fallback、flaky_candidates、token_usage、门禁结论 |

> 全框架实体字段统一带**证据等级** `evidence ∈ {verified, inferred, unmapped}`，对齐 CodeGraph 诚实标记（`FACT/POSSIBLE/UNKNOWN`），保证"事实源零 LLM、不臆造"可被机器校验。

**基础设施（非业务实体，供多能力复用）**：

| 基础设施            | 提供能力                     | 消费方          |
| --------------- | ------------------------ | ------------ |
| CodeGraph（代码图谱） | 符号+调用关系图、反向/正向追溯、变更 diff | C2、C3、C3.5.1 |
| 术语表             | 业务术语/字段语义                | C3、C4        |
| 规则库（3.1） | 业务规则 BR / 契约规则 CR + questions / gaps / conflicts | C4、报告 |
| 用例库（3.2，一等资产） | 已批准可执行用例（business/code）+ fingerprint / 版本 / 审核日志 | C6、**C7**、报告 |

---

## 五、横切设计

### 5.1 确定性边界（LLM 使用原则）

| 能力          | 是否用 LLM | 理由                          |
| ----------- | ------- | --------------------------- |
| C1 需求分析     | 是（A1）  | 文档切分/哈希/契约解析（HTTP：OpenAPI/Spring MVC 注解；gRPC：proto/descriptor；Dubbo：接口源码）确定性；业务规则由 LLM 按 `a1_requirement_analysis.md` 分析，框架外执行、框架内校验 |
| C2 变更感知     | 否       | git diff + 图谱追溯 + base/target 契约 diff，纯确定性 |
| C3 接口画像     | 否       | 事实源，不容忍幻觉                   |
| C3.5 下游依赖控制 | 部分 | 依赖识别、`success` 分支与 Mock 编排确定性；业务错误分支由规则映射或适配层补充并逐项校验 |
| C5 数据准备     | 是（辅助） | DDL 解析/FK 闭包/拓扑/SQL 渲染/校验确定性；字段业务语义、候选业务态与取值由 LLM 适配层补，未知表/列/值直接拒绝 |
| C4 两级用例设计 | 部分 | 业务级/代码级草案由规则驱动生成（适配层/人工）；导入、审核、版本化零 LLM |
| C6 用例编码     | 部分      | 标准用例模板生成；复杂编排可用 LLM 但双重自检   |
| C7 执行验证     | 否       | 执行与归因，确定性                   |

### 5.2 异常处理总原则

1. **事实源环节（C2/C3 与 C5 的确定性核心）失败即中止**——上游错了下游全是垃圾；C5 的 LLM 语义候选失败不阻塞骨架，缺失字段标 `unmapped`/`inferred`
2. **生成环节（C4/C6）失败可降级**——草案校验失败不写库；C6 模板失败跳过该接口并记录
3. **所有降级必须显式标记**在报告中，不允许静默

### 5.3 可追溯性

每个最终产物都能反向溯源：报告中的失败 → `TC-NNNN`/`case_key` → `rule_refs` 的
`rule_key` → `requirement_refs` 的文档 section 与 quote → `change_refs` 的变更符号 → git
提交。溯源链断任何一环都视为设计缺陷；规则缺失、规则漂移、`change_without_oracle`
必须在报告中显式列出。

**证据等级（evidence）贯穿全程**：每条画像/血缘/造数字段必须带 `verified/inferred/unmapped` 三级证据标记，报告与门禁可据此区分"已验证事实"与"推断值"，保证"事实源零 LLM、不臆造"可被机器审计（对齐 CodeGraph 的 `FACT/POSSIBLE/UNKNOWN` 诚实标记）。

### 5.4 扩展点

| 扩展点             | 机制                                  |
| --------------- | ----------------------------------- |
| 新 Web 框架路由识别    | C2 路由插件接口                           |
| 新文档格式           | C1 解析器插件                            |
| 新数据库方言          | C5 DDL 解析方言包                        |
| 对接测试平台          | C7 报告适配器（测试平台 REST 推送）          |
| 断言库扩展           | C4/C6 断言类型注册表                       |
| 下游依赖注入点         | C3.5 注入点插件（HTTP 代理/SDK 注入/MQ topic） |
| Mock/Sandbox 后端 | C3.5 替身服务接入适配器                      |
| LLM 模型接入        | LLM 网关适配器（不同 provider 的 usage 字段差异归一）   |
| 模型单价表           | C7.4.1 成本估算配置（prompt/completion 单价，可热更） |

### 5.5 多 SUT 并发隔离（v4.5）

> 同一测试环境允许同时运行多个 `astsight run` 进程，每个进程服务一个 SUT 实例。进程之间不共享内存、全局单例或调度器；**测试引擎之间本身不会互相干扰，干扰只来自外部资源**——端口、文件、命名空间、数据库、用例库。因此把一次 run 外部可见的一切都收敛到实例身份与实例作用域，默认不共享。

**隔离单元：`sut_id`**

- 一次 run 只服务一个 SUT；`sut_id` 由 `--sut-id` 显式指定，缺省取 project 名。
- 同一环境下并发运行相同 `sut_id` 视为配置错误；框架不做跨进程合并，也不假设“同名就是同一实例”。重复 `sut_id` 不会静默共享资源：共用 Mock 会在会话创建时报 `mock_session_conflict`，共用心跳路径会在身份校验时报 `mock_identity_mismatch`。
- `base-url` 仍是一次 run 一个 SUT；并发测试多个 SUT = 多个 `astsight run` 进程，不是单进程内多租户。

**规则 1：框架自有的监听端口必须原子分配**

- 范围：HTTP Mock 服务、Dubbo agent admin、gRPC gripmock，以及框架拉起的其他监听端（Dubbo sidecar 及其 gRPC→sidecar 回退走 stdio，不占端口）。
- 分配方式：绑定 `127.0.0.1:0`（需跨容器访问时绑容器网卡 `:0`），由内核分配；读取实际端口后写回 run 配置与 `run_manifest.json`。**禁止固定默认端口、禁止进程内计数器、禁止“先探测再绑定”**。
- 使用方只从 run manifest / 启动参数取实际端口，不从环境变量、全局配置或另一实例的日志推断。
- 每个端口在 manifest 中带 `owner`（`sut_id` + `pid` + 组件名）；报告只出 `host:port` 与 owner，不出 token。
- 分配或绑定失败 → `port_alloc_failed`（环境类，整批阻断），不回退到其他固定端口。

**规则 2：实例身份贯穿所有可寻址资源**

| 资源 | 身份要求 |
|---|---|
| Mock 命名空间 | `namespace = sut_id`；禁止用字面量 `"default"` 作为多 SUT 环境的路由键。未显式指定 `sut_id` 时用 project 名，不用 `"default"` |
| Dubbo 心跳文件 | 路径含 `sut_id`（如 `/tmp/astsight/<sut_id>.heartbeat.json`）；启动前只删除本实例路径；不得共用固定路径 |
| 心跳内容 | 必须含 `sut_id / namespace / admin_endpoint / token_fingerprint`，并保留 `pid`、`jvm_start_time` |
| agent admin | 每实例独立端口 + 每实例随机 token；token 由引擎生成并随启动参数注入，不复用、不落报告 |
| gripmock | 每命名空间一实例；`MockServer.sut_id` 与 `namespace` 必须同时记录 |
| HTTP Mock | 每 `sut_id` 一实例（`transport="http"`、`mode="embedded"`）；默认只绑回环，跨容器时绑容器网卡并写 manifest；admin token 每实例独立；`route_key` 只在所属 namespace 内有效 |
| 会话建立前 | 校验心跳中的 `sut_id / namespace / admin_endpoint / pid / jvm_start_time` 与本 run manifest 一致；任一不符 → `mock_identity_mismatch`，整批阻断 |

- 不允许跨实例复用 admin endpoint：即使地址相同，也必须由本 run 的 manifest 与心跳共同证明归属。
- 会话请求中的 `namespace` 必须等于当前 run 的 `sut_id`；Mock 服务端拒绝不属于本实例命名空间的会话创建。

**规则 3：持久化资源按 run 作用域隔离，写入串行化**

| 资源 | 规则 |
|---|---|
| DB 造数键 | 变量域从 `T{case_id}_` 升级为 `T{run_scope}_{case_id}_`，`run_scope` = `sut_id + run_id` 的稳定短哈希；teardown 按 fixtures 主键逐行参数化删除（逆拓扑），只清本用例造的行，不得按裸 `T`、前缀或整表清理 |
| 固定种子数据 | 必须在 DataPlan 中标记 `shared=true`；共享唯一键进入 C7.1 冲突图并在本 run 内串行；跨 run 的写冲突由环境隔离或数据库唯一约束暴露，框架不假设“无冲突” |
| 用例库写操作 | `cases import/approve` 对 `<case-base>.lock` 加 `fcntl.flock` 排他锁；超时 → `case_store_locked`，不覆盖、不重试写入；只读 `run --mode cases` 不持锁 |
| CodeGraph 索引 | 同一仓库仍只允许一个写者；并发 `sync` 拿不到仓库锁 → `codegraph_busy`，按模式阻断或标 `degraded`（只读查询可继续），不得两写者同时落盘 |
| run 目录 | `run_id` 增加随机后缀（时间戳 + 4 位随机），仍按 `runs/<run_id>/` 隔离；`run_manifest.json` 写在 run 目录根部，记录实例身份与端口 owner；不得共用固定临时目录 |
| 缓存/外部服务 | Redis 等可随测随建的实例按 `sut_id` 分配独立 DB；环境提供的共享实例，框架写入的键必须带 `sut_id`/`run_scope` 前缀，不得使用固定键 |

**失败子码（并发隔离扩展）**

| 子码 | 含义 | 处理 |
|---|---|---|
| `port_alloc_failed` | 原子端口分配/绑定失败 | 环境类，整批阻断 |
| `mock_identity_mismatch` | 心跳或会话命名空间与本 run 的 `sut_id`/endpoint/pid 不一致 | 框架/配置类，整批阻断 |
| `case_store_locked` | 用例库排他锁超时 | 阻断本次写操作，不改库 |
| `codegraph_busy` | 同仓库已有写者 | 按运行模式阻断或 `degraded`，不并发写 |

**报告与证据**：每个 run 的报告增加 `instance` 块（`sut_id / run_id / namespace / ports[] / heartbeat_path / admin_token_fingerprint / locks / isolation`），见 C7.4 示例。端口 owner、锁状态与作用域必须可审计。

**门禁**：同一环境并发 ≥2 个 `sut_id` 时，必须同时满足——所有框架端口来自原子分配并写入 manifest；`namespace` 全部等于 `sut_id`；心跳身份字段匹配；DB fixture 前缀含 `run_scope`；用例库写操作持锁。任一不满足 → 阻断，不允许以“环境共用”为由降级。

**边界**：网络出站隔离、注册中心、provider、数据库实例仍是环境级共享资源，由环境保证；本节只约束框架自己创建、命名、写入的外部资源。

---

## 六、质量门禁汇总

| 能力     | 门禁指标                                  | 验证方式           |
| ------ | ------------------------------------- | -------------- |
| 3.1 规则库 | ruleset 可加载；来源 section 可定位；规则值有 quote 依据；冲突记录并展示（无人工裁决节点） | ruleset 审计 |
| 3.1 规则覆盖 | BR/CR 覆盖率分列；未覆盖规则与 gaps 必须列出；规则漂移用例标 `stale` | 规则覆盖统计 |
| 3.2 用例库 | 无可执行用例（`cases` 模式）阻断；待审 proposal 可见并标 `degraded`；approve 无 pending、乐观锁匹配；两级 `rule_refs`/`change_refs` 完整 | caseset / proposal 审计 |
| 3.2 断言保真 | 无 L3/L4 → L1/L2 降级（逐条列出） | C6 断言扫描 |
| 3.2 需求/变更覆盖 | 未覆盖 section/变更接口必须给出 reason；`change_without_oracle` 单列 | coverage 统计 |
| 3.2 变异测试 | **变异存活率 0%（`delta` 模式）** | 设计目标，尚未实现 |
| C1     | context/ruleset 可确定性重建；过期 context 拒绝导入；`questions/gaps` 不混入 rules；契约源至少一种 | 文档/契约源/prompt hash 对照 |
| C2     | 召回率 ≥90%，误报率 ≤30%；HTTP/gRPC/Dubbo 分形态统计；纯契约变更分支必须产出端点；`unsupported`、`removed`、`external_dependency_change` 显式列出且不计入覆盖；`contract_diff_unavailable` 必须告警 | 20 个真实变更分支人工标注 |
| C3     | 契约源至少一种且可定位；约束提取准确率 100%；HTTP 下游识别召回率单列，`unidentified` 进缺口清单；gRPC/Dubbo 画像字段必须来自 proto/接口源码；`unsupported` 不得产出画像；每个 `business_key` 只有一个 `primary`，alias 必须带 `alias_of` 与 `adapter_fingerprint`（或 `fingerprint_unavailable`） | 自动化比对 + 画像审计 |
| C3.5   | HTTP 与 Dubbo 2.7+ 下游 Mock 覆盖率 100%；存在未 Mock 且未声明下游即阻断；gRPC 下游必须 `mock="unsupported"` 且用例显式排除/`blocked`；HTTP 必须给出 `mock.strategy`、唯一 `dep_id` 与实际配置键（或发现/代理覆盖证据），v1 承诺仅下游专属 `url_override` 与显式明文 `http_proxy`，`discovery_override`/透明代理须标注条件能力，共享 base URL/HTTPS/动态 URL 必须显式 `unsupported` 且依赖用例 `blocked`；Dubbo 必须给出 `mock.strategy="agent_filter"` 与 agent 版本；无 Mock 命中证据标 `weak_oracle` | 画像审计           |
| C3.5.1 | HTTP 与 Dubbo 语义分支覆盖被测接口消费面 100%；用例显式绑定语义分支；gRPC 下游不生成语义模板 | 追溯校验           |
| C3.5.2 | 每个 HTTP 下游至少达档 1（有契约骨架）；语义缺失须显式标记；gRPC 下游标 `mock_unsupported` | 画像审计           |
| C3.5.3 | agent 自检通过（版本匹配 + 心跳文件 `pid/jvm_start_time` 匹配且新鲜 + `filter_instantiated>0`）；`No provider available` 报 `env_error` 不兜底；`serialization_verified=false` 可见；绑定 dep 命中 0 次报 `mock_not_used` | 启动自检 + 执行证据 |
| 5.5 多 SUT 隔离 | 并发 ≥2 个 `sut_id` 时：端口全部原子分配并写入 manifest；`namespace=sut_id`；心跳 `sut_id/namespace/admin_endpoint/pid/jvm_start_time` 匹配；DB fixture 前缀含 `run_scope`；用例库写操作持锁 | 启动自检 + run manifest 审计 |
| C3.5.4 | 随包二进制版本/sha256 校验通过；平台在 `linux/amd64`、`linux/arm64` 矩阵内；target 改写显式；每个绑定桩命中 ≥1 次且有 history 证据；未匹配不得当通过 | wheel 审计 + 启动自检 + history |
| C3.5.5 | 每个 `type=http` 下游有 `mock.strategy`、唯一 `dep_id`、唯一 `route_key` 与注入证据；绑定 route 命中 ≥1 次且有 `mock_calls.jsonl` 证据；未知/歧义路由、Mock 错误响应、晚到调用不得当通过；HTTPS 下游必须 `tls_not_supported` 且用例 `blocked`；`dep_collision`/`mock_route_conflict` 画像期阻断 | 画像审计 + 启动自检 + 调用证据 |
| C5     | 造数成功率 ≥99%，闭包完整率 100%                 | 执行验证           |
| C4     | 仅 approved 用例进入 C6；每 transport 原生状态断言非空率 100%；两级 `rule_refs` 有效；`mock_bindings` 可解析且分支存在；`mock="unsupported"` 下游不被绑定；无 `change_without_oracle` 静默；规则/需求/契约漂移可见；`coverage_role` 与画像一致；alias 至少一条成功冒烟且不计入业务覆盖；`transport_alias_skipped` 显式出报 | case store / proposal 审计 |
| C6     | 可运行率 ≥95%；三 transport fixture smoke 通过；调用 SUT 的生成物不含 `Attach`/`javaagent`；Dubbo 下游 Mock 的启动 fixture 显式引用 agent 并校验心跳 | pytest 收集+执行   |
| C7.1   | 冲突判定准确率 100%（确定性）                     | 对照人工标注         |
| C7.2   | 并行调度可重放、分组确定                          | 重复执行分组一致       |
| C7.3   | 归因准确率 ≥85%；重试 ≤3 且全程留痕；适配器错误不计入 SUT 缺陷；回退单次且留痕 | 抽样人工复核         |
| C7.4   | 门禁结论只反映真实缺陷（不含量化重试水分）；`instance`、`by_transport`、`transport_coverage`、`mock_sessions`、`unsupported`、`transport_fallback` 必报；alias 冒烟不计入业务覆盖 | 报告审计           |
| C7 执行 | 数据清理成功率 ≥99%；Mock 会话冲突/未命中/晚到调用零静默；HTTP 与 Dubbo 下游的透传防护以 `egress_guard` 如实标注（网络隔离才算强保证）；Dubbo agent 未激活整批阻断；gRPC 下游依赖用例已排除/`blocked`，不真实调用 | 执行期监控 + 网络审计          |

---

## 七、风险与对策

| 风险                     | 等级 | 对策                                             |
| ---------------------- | -- | ---------------------------------------------- |
| Java/Spring 路由识别精度不足   | 高  | 先用 5 个真实项目实测；不达标则 Java 栈仅启用"路径启发式+人工确认"模式      |
| 纯契约变更漏检（只改 proto/OpenAPI/接口签名） | 高 | C2 强制 base/target 契约 diff；解析失败标 `contract_diff_unavailable`，不当作无变更；纯契约分支纳入召回率基准 |
| 多协议端点被误当同一接口，alias 漏测差异 | 高 | 端点身份保留 `transport`；`business_key` 只归组；`adapter_fingerprint` 不同必须冒烟，差异大升 `independent`；alias 冒烟不计入业务覆盖 |
| 仓库外变更/依赖升级无法追溯 | 中 | 明确不追溯；标 `external_dependency_change` 并单列；需要覆盖时由用户显式提供外部契约，不猜实现 |
| 多协议覆盖范围被缩水刷高通过率 | 中 | `--transport-scope=primary` 显式降级并标 `degraded`；`transport_alias_skipped` 必须列出；业务覆盖与 transport 覆盖分列 |
| 设计文档格式千差万别             | 高  | 先限定 Markdown 表格型字段定义；其他格式走 LLM 精读+人工复核         |
| 逻辑外键（无 DDL 约束）导致闭包不完整  | 中  | 命名约定补推 + 设计文档表关系章节 + inferred 标记人工确认           |
| 生成用例通过率虚高（断言过宽）        | 中  | 门禁看断言强度指标（字段级断言占比），不只看通过率                      |
| 业务约束推不出（qty≤库存类）       | 中  | 需求原文 + 已审核用例假设显式表达；推不出写入 `uncovered_reasons`，不猜 |
| LLM 规则幻觉（编造错误码/字段）    | 高  | section 落地校验 + 数字/枚举必须有原文出处；A 级不通过降级 B；没写的进 questions/gaps；最终由用例审核确认 |
| 规则层二次翻译导致判定降级        | 中  | 规则只做设计期材料，不执行表达式；oracle 仍是已批准用例断言；规则不参与 C7 |
| BR 编号漂移导致用例挂错规则       | 中  | `rule_key` 稳定身份；`BR-NN` 只用于展示；diff 按 `rule_key` |
| 规则灌水刷覆盖率                  | 中  | 规则粒度最小值；BR/CR 覆盖率分列；覆盖是设计指标不是通过率；审核裁剪 |
| code 用例把 diff 当 oracle        | 高  | 没有 A/B/CR 规则就不生成用例，写 `change_without_oracle`；断言值只能来自规则/契约/已批准 business 用例 |
| 两级用例重复或期望冲突            | 中  | business 为权威层；`(endpoint, case_key)` 唯一；code 只补未覆盖分区 |
| 规则变更后旧用例静默失效          | 中  | `rule_drift` 标记 `stale`；full/delta 默认阻断，需重审或显式降级 |
| 审核流于形式（批量批准）           | 高  | approve 必须带 reviewer；deprecate 逐项批准；review_log 追加留痕；demo 自动批准仅演示 |
| 草案只堆正例，缺负例/边界           | 中  | 审核清单按类别检查；需求覆盖按 section 统计，不允许只数用例条数 |
| 数据库直连权限与安全             | 中  | 默认只读结构（information_schema）；造数仅在独立测试库，连接串按环境隔离  |
| 下游依赖无法识别（动态调用/配置化 URL） | 中  | 标记 unidentified 人工补画像；报告显式告警，不静默透传             |
| Mock 注入失效导致真实透传（资损风险） | 高  | **网络出站隔离**是唯一强保证（`egress_guard=network`）；无隔离时标 `egress_guard=none` + `degraded`，并输出 SUT 配置来源供人工确认；side_effect.has=true 的下游优先要求隔离。不得用应用层会话记录冒充零透传 |
| Mock 响应与 DB 造数变量域不一致   | 中  | Mock 与 DB 造数共享同一变量域（`T{run_scope}_{case_id}_` 前缀），门禁校验两者对齐   |
| 多 SUT 同环境并发时端口/命名空间冲突 | 高 | 5.5：框架端口一律 `bind(:0)` 原子分配并写回 manifest；`namespace=sut_id`，不用字面量 `"default"`；冲突即 `port_alloc_failed` / `mock_session_conflict`，显式阻断 |
| 引擎连到另一个 SUT 的 agent/Mock（串控） | 高 | 5.5：每实例随机 token + 心跳 `sut_id/namespace/admin_endpoint/pid` 校验；不符即 `mock_identity_mismatch`，不得建会话；禁止从全局配置推断 admin endpoint |
| 并发 run 共用 DB 造数键 / 用例库并发批准 | 高 | 5.5：fixture 前缀含 `run_scope`，teardown 只清本前缀；固定种子标 `shared=true` 并串行；`cases import/approve` 持 `flock`，超时报 `case_store_locked` |
| Mock 语义失真导致假绿          | 高  | C3.5.1 语义模型：契约+业务知识+用例意图三层定语义，分支覆盖率门禁 100%     |
| 下游语义信息缺失（无文档无源码）       | 中  | C3.5.2 三档渐进：档1文档先行跑通，档2补源码跑准；缺口显式标记不假装完整       |
| 下游 HTTP 地址在运行时拼接（SpEL、配置中心、服务发现动态注册） | 高 | 画像标 `target=inferred`；明文 HTTP 可走 `http_proxy` 兜底，HTTPS 或代理不可用则标 `unsupported`；不猜地址，不允许静默透传 |
| 不同下游共用同一 `METHOD + path`（如都暴露 `GET /api/v1/status`） | 高 | C3.5.5：`dep_id` 含 `downstream_key`（服务名/配置键/主机），路径占位符归一；同端点多调用点合并；归一后仍撞则 `dep_collision` 阻断，不产生重复 id |
| `route_key` 路由未知或歧义 | 高 | 路径前缀优先、`Host` 次之、唯一 `METHOD+path` 兜底；无命中 `mock_route_unknown`、多命中 `mock_route_ambiguous`，直接阻断，绝不透传或默认 200 |
| HTTPS 下游暂不支持 | 高 | 统一 `mock="unsupported"` / `tls_not_supported`，依赖用例 `blocked`，不降级为 http、不装测试 CA、不做 MITM；报告单列 |
| 内嵌 HTTP Mock 进程异常 | 中 | 启动前健康检查，运行期异常写 `mock_server_unavailable` 并阻断；不把异常响应当作用例通过 |
| 硬编码地址或自定义拦截器绕过配置覆盖 | 中 | 画像阶段检出并标 `mock.strategy="http_proxy"`（仅明文）；无法覆盖或目标为 HTTPS 时显式 `unsupported`（`tls_not_supported`）并降级 |
| 一个 base URL 配置键服务多个下游 | 高 | `url_override` 无法为同一键注入多个 `route_key`：标 `mock_config_ambiguous`，转 `http_proxy`；代理也不可用则 `unsupported`，依赖用例 `blocked`，不猜测路由 |
| 客户端 `fallback`/Sentinel 吞异常造成假绿 | 高 | 用例同时断言“Mock 命中次数 + fallback 未触发”；只断言状态码的标 `weak_oracle` |
| 多个调用点共用同一 `dep_id` 但语义不同 | 中 | 合并为同一 dep，HTTP 边界无法区分调用点；标 `mock_binding_ambiguous` 并阻断该 case，不按调用点拆 `dep_id`；审核文件必须展示 |
| 客户端忽略 `HTTP_PROXY` 或代理链路不可用 | 中 | 画像阶段做兼容性 spike；不可用即 `mock_proxy_unsupported`，下游 `unsupported`，不假装已 Mock |
| 下游 provider 契约与调用方假设不一致 | 中 | 以 OpenAPI/接口文档为契约；发现不一致时标 `contract_conflict` 交人工，不自动择一 |
| Dubbo 接口只在依赖 jar，源码不可见 | 高 | 明确不支持：标 `unsupported/interface_in_jar`，不反编译、不猜签名；报告单列 |
| Dubbo 动态代理 / 反射 / `getBean` 调用 | 高 | 明确不支持：标 `unsupported/dynamic_lookup`；C2/C3 不做运行时注册中心反推 |
| Dubbo 2.6 存量系统 | 中 | 明确不支持；标 `unsupported/dubbo_2_6`，不静默降级到 HTTP |
| Dubbo sidecar 版本/序列化不兼容 | 中 | sidecar 固定 `hessian2` + 精确 `param_types`；启动自检版本与序列化，失败标 `adapter_error`，不回退 HTTP |
| gRPC descriptor 与 proto 不一致 / reflection 关闭 | 中 | 以仓库 descriptor 为准；缺失即 `unsupported/no_descriptor`，不在执行期尝试 reflection |
| gRPC streaming/oneway 端点 | 中 | 仅支持 unary；streaming/oneway 标 `unsupported`，不生成用例 |
| gRPC→sidecar 回退掩盖 gRPC 路径缺陷 | 高 | 仅 Dubbo 家族、显式 `--allow-transport-fallback`、单次且报告 `transport_fallback + degraded`；回退通过不得证明 gRPC 路径正确 |
| gRPC 下游无法 Mock 却执行真实调用 | 高 | 识别即 `mock="unsupported"`；依赖用例显式排除或 `blocked`；Mock 会话无法兜底 gRPC 下游，只能靠用例排除 |
| gripmock 二进制缺失 / 平台架构不匹配 | 中 | 平台专用 wheel 固定版本 + sha256；只承诺 `linux/amd64`、`linux/arm64`；矩阵外显式 `mock_binary_missing`，不回退 PATH 或联网下载；`--gripmock-path` 仅调试并标 `degraded` |
| 随包第三方二进制供应链 / 许可证 | 中 | 从固定 commit 自建、可复现构建、sha256 清单、LICENSE/NOTICE 随包；运行期不下载；发布前核对许可证条款与 wheel 体积 |
| Dubbo agent 与 SUT 版本不兼容 / Filter 未进入调用链 | 高 | 启动自检 + 本地心跳文件（`pid/jvm_start_time` 匹配、心跳新鲜、`filter_instantiated>0`，旧文件启动前清理）；任一不满足即 `agent_not_active` 整批阻断；兼容矩阵未覆盖的组合标 `unsupported`，不在运行期试探 |
| agent 返回对象跳过 Hessian2，掩盖序列化缺陷 | 中 | 报告 `serialization_verified=false`；依赖序列化验证的用例标 `unsupported`，不得以 agent Mock 的通过作为编解码正确性证据 |
| SUT 无法发现 provider（`No provider available`） | 中 | 报 `env_error`，用例 `blocked`；框架不部署/伪造 provider、不降级到真实调用 |
| token 成本失控（全流程 AI 测试烧钱）   | 中  | C7.4.1 统一计量 + 按节点/模型定位成本大头；重试归因优化是降本主抓手；超预算预警 |

---

## 八、实施顺序（设计确认后的工程排期参考）

| 阶段 | 范围 | 出口标准 / 状态 |
| ---- | ------------------------- | -------------- |
| M1 已交付 | C2 变更感知 → C3 画像 → C6 编码 → C7 执行（`delta`） | 真实 Python/Spring 仓库分支 → API 列表；示例服务 delta 通过；注入缺陷可归因 |
| M2 已交付 | C1 需求上下文 → C4 草案/审核/版本化 → `run --mode cases` | prepare → import → approve → cases 模式全链路；未审草案不落库；待审 proposal 可见 |
| M2.1 | **C1 规则层**：分析上下文 + `a1_requirement_analysis` 提示词 + `rules import` + CR 契约规则 + `rule_key` diff | `ruleset.json` 可加载；同输入可 diff；A 级落地校验生效；questions/gaps 单列 |
| M2.2 | **业务级用例**：规则驱动的 C4a 草案 + `rule_refs` + 审核展示规则依据 + 规则覆盖报告 | business 用例全部挂 `rule_refs`；BR/CR 覆盖率分列；B 级规则被批准用例确认 |
| M2.3 | **代码级用例**：C2 变更选择 + 覆盖矩阵 + C4b 差分草案 + `change_refs` + `change_without_oracle` | delta 只为未覆盖变更生成 code 用例；无规则依据时不生成；变更覆盖率可见 |
| M2.4 | **HTTP 下游通用接入**：出站 HTTP 调用点识别（配置/服务发现/代理注入点定位）+ 契约源优先级（OpenAPI > Spring MVC 注解）+ 稳定且唯一的 `dep_id`（C3.5.5） | 无 OpenAPI 的 Spring 项目也能建画像；下游 id 跨构建稳定；同路径跨下游不重名，多调用点不重复；无法识别的调用点进缺口清单 |
| M2.5 | **transport 模型 + gRPC**：`transport/transport_class/transport_evidence`、端点标识、gRPC descriptor 加载、unary 调用、`grpc_status/response_field/metadata` 断言、`unsupported` 清单 | gRPC 项目可建画像并执行 unary 用例；无 descriptor 端点显式 `unsupported`；无 HTTP 伪装 |
| M2.6 | **Dubbo sidecar**：Dubbo 2.7+ 注解/XML 识别、接口源码校验、sidecar 泛化调用（Hessian2、精确 `param_types`）、`rpc_status/result_field/exception_type` 断言、注册中心直连 | Dubbo 2.7+ 标准场景可执行；2.6/jar-only/动态查找显式 `unsupported`；sidecar 启动自检与错误归一 |
| M2.7 | **transport 回退（受限）**：仅 `transport_class=dubbo` 的 gRPC→sidecar，`--allow-transport-fallback` 显式开启、单次、报告 `transport_fallback + degraded` | 默认关闭；回退失败归 `adapter_error`；禁止跨形态回退 |
| M2.8 | **多协议覆盖策略**：C2 provider-only + base/target 契约 diff + `change_kinds`；C3 `business_key`/`coverage_role`/`adapter_fingerprint`；C4 alias 冒烟 + `--transport-scope`；C7.4 `transport_coverage` | 纯契约变更分支可产出端点；每个 `business_key` 仅一个 primary；alias 冒烟可执行且不计入业务覆盖；`transport_alias_skipped` 可见 |
| M2.9 | **Mock 用例级会话（方案 A）**：Mock 服务 admin session API、`mock_bindings` 导入期补全、C7.1 `mock:<namespace>` 独占、C7.2 mock lease 串行、C6 session fixture、C7.4 `mock_sessions` 证据 | 单命名空间单 active session；冲突/未命中/晚到调用全部显式报错；mock-bound 用例串行且证据可复核；无 session 证据的 Mock 用例标 `weak_oracle` |
| M2.10 | **Dubbo 下游 Mock（agent + ByteBuddy + Filter）**：agent 加载（`-javaagent`/classpath）、Filter 拦截、会话分支、同步/异步/重载/泛化、心跳自检、`serialization_verified=false`、兼容矩阵 | agent 未激活整批阻断；绑定 dep 命中 0 次报 `mock_not_used`；`No provider available` 报 `env_error`；2.7/3.x + JDK 8/11/17/21 主组合可运行 |
| M2.11 | **多 SUT 并发隔离（5.5）**：`--sut-id` 与 `namespace=sut_id`、端口 `bind(:0)` 原子分配 + `run_manifest`、心跳身份字段、每实例 token、DB `run_scope` 前缀、用例库 `flock`、报告 `instance` 块 | 同环境并发 ≥2 个 SUT 时无端口/命名空间冲突；心跳与 manifest 身份一致；DB 清理只影响本 run；并发 approve 不会丢更新 |
| M2.12 | **gRPC 下游 Mock（C3.5.4）**：gripmock 随包二进制（平台专用 wheel，`linux/amd64` + `linux/arm64`）、每命名空间进程、stub API、history 取证、target 改写、已知缺陷规避、失败子码 | 安装 `astsight[grpc-mock]` 即用，不要求用户单独下载；版本/sha256 校验通过；矩阵外显式 `mock_binary_missing`；绑定桩命中 ≥1 次 |
| M2.13 | **HTTP 下游 Mock（C3.5.5，仅明文）**：内嵌 FastAPI/uvicorn、每 `sut_id` 实例、`bind(:0)` + manifest、`dep_id` 唯一性算法、`route_key`、v1 承诺的下游专属 `url_override` 与显式 `http_proxy`（`discovery_override`/透明代理条件支持）、会话分支、`mock_calls.jsonl` 证据、HTTPS `tls_not_supported` | 两个服务同路径不撞 `dep_id`；同端点多调用点只生成一个 dep；未知/歧义路由阻断；共享 base URL 报 `mock_config_ambiguous`；绑定 route 命中 ≥1；HTTPS 显式 `unsupported` 且用例 `blocked` |
| M3 | C5 数据准备（外键闭包 + SQL 造数） | 订单场景 3 表闭包造数成功 |
| M4 | C3.5 Mock（HTTP 专属 URL/显式明文代理覆盖 + 条件发现覆盖 + Dubbo agent + 用例级会话）+ C7 并行/冲突 | HTTP 与 Dubbo 2.7+ 下游 Mock 覆盖率 100%；Mock 会话冲突/未命中/晚到调用全部显式出报；`egress_guard` 与 `serialization_verified` 如实标注；HTTP 覆盖策略与实际配置键可审计，v1 承诺项与条件能力分别标注；共享 base URL、覆盖不到或 HTTPS 的标 `unsupported` 且用例 `blocked`；gRPC 下游标 `mock="unsupported"` |
| M5 | `full`/`replay` + C2 召回率基准 | 20 个真实分支人工标注；replay 可重放 |

> 本说明书为设计基线 **v4.7**；v4.7 明确 A4 由规则驱动（不再走“语义分析 → 用例草案”），C5 拆为 C5a 数据目录 / C5b 逐用例计划，且 C5 的 LLM 只提供语义候选、不产出 SQL；**v4.6 取消 Feign 特殊支持**（Feign 按普通 HTTP 客户端处理，覆盖不了即 `unsupported`，见文首 v4.6 决策）；**v4.6.1 收紧 HTTP Mock 范围**（仅明文 HTTP，HTTPS 标 `tls_not_supported`；`dep_id` 唯一性规则见 C3.5.5）；**v4.4 的 gRPC Mock 决策已记录（C3.5.4），但尚未实现**；v4.5 的多 SUT 并发隔离规则见 **5.5**（设计已定，实现待排期）。落地时在 [`决策记录.md`](决策记录.md) §8 的版本变更记录中升版。后续实现阶段任何对本契约的偏离，需回写本文档并升版本号。

> **形态范围（v4.2，下游 Mock 边界已由 v4.3 扩展）**：支持三种 transport——**HTTP、gRPC（仅 unary）、Dubbo（仅 2.7+）**。
> Feign 等 HTTP 客户端一视同仁，按普通 HTTP 出站调用处理，不新增 transport、不做专用支持；gRPC 走仓库 proto/descriptor；Dubbo 走 Java sidecar
> 泛化调用。Dubbo 3 + Triple + protobuf + descriptor + 无 group/version 可路由到 gRPC；其余 Dubbo
> 家族走 sidecar；证据不足标 `transport_evidence=inferred`；无可用路径标 `unsupported`，不静默回退。
> SUT → Dubbo/gRPC 下游的 Mock 在 v4.2 不在范围（`mock="unsupported"`）；v4.3 起 Dubbo 下游改用
> agent + ByteBuddy + Filter 拦截（低侵入），gRPC 下游仍不支持（v4.4 决策：gRPC 下游改用 gripmock 独立服务器，见 C3.5.4）。调用 SUT 仍不使用 Java agent。
> Thrift 不考虑。契约源按 `OpenAPI > Spring MVC 注解 > 文档`（HTTP）、
> 仓库 proto/descriptor（gRPC）、接口源码 + 标准注解/XML（Dubbo）取值，至少一种；
> HTTP 全部缺失时 `contract_incomplete`，不猜字段。
> C2 只感知被测仓库的**提供方**端点；消费方与仓库外变更不追溯（`external_dependency_change`）。
> 同一实现多协议暴露时保留全部端点，默认 `primary` 全量 + `alias` 契约冒烟 + `independent` 全量；
> `--transport-scope=primary` 显式跳过 alias 并标 `degraded`，不静默省略。

> **排期说明**：`full` 模式是入场路径，不是可选项——M3 之前应补一条「`full` 在单仓跑通并产出契约基线」，否则 `delta` 没有基线可维护，后续能力全部悬空。

> **排期说明**：规则层是**设计期资产**——规则用于业务级用例设计与覆盖统计，不执行表达式、不作运行时 oracle；代码级用例只从变更缺口补充，且不得以 diff 为 oracle。相关取舍与约束见 [`决策记录.md`](决策记录.md) §2。

---


## 版本变更记录

> 已迁移至 [`决策记录.md`](决策记录.md) §8；本文不再维护变更历史。
