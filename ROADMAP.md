# 路线图

> 本文件说明 AstSight 的**已知边界**与**规划方向**。诚实是我们的原则：
> 每一项“尚未实现”，在代码和报告里都会被显式标注，而不是静默降级。
> 欢迎在 issue 里认领下面任何一项，或在 discussion 里提出新的方向。

## 为什么是 AstSight

一句话：**把需求文档和代码变更加工成可执行、可审核、可追溯的 API 用例。**

与“pytest + 一个 LLM”的区别，在于两个设计原则：

1. **模型只出候选，不直接改结论**——规则和用例都经框架校验后入库，杜绝模型幻觉落库；
2. **初始用例审核是硬门禁**——用例入库必须由真实审核人执行，框架不会自动批准；
   运行期失败修复是 Agent 的有界动作，以 `astsight-agent:repair` 身份留痕，不冒充人工审核。

规则是唯一事实源，用例从规则派生，冲突在规则层就暴露，而不是散落在一堆用例里。这是为了回答一个更根本的问题：**AI 生成的东西，出错了能被抓住吗。**

## 当前能力（已交付）

- 需求/设计文档 + OpenAPI → 业务规则（BR）+ 契约规则（CR）
- 规则 + 接口画像 + Mock 语义 + 数据目录 → 可校验的用例草案
- 人工审核 + 版本化用例库（`TC-NNNN` 永不复用）
- 多请求用例（`steps[]` 顺序请求与跨步骤引用）
- 断言：状态码、响应字段、header、OpenAPI 响应 schema、只读数据库行；强度 L1–L4 按内容推导
- 下游明文 HTTP Mock + C5a 数据目录 + C5b 逐用例 SQLite/PostgreSQL/MySQL 造数
- 用例级业务实体：请求与下游 Mock 引用同一份 `data`，用 `mock_replies` 把实体
  写进响应，按业务语义造数而不是从环境里捞真实 ID
- A5/A6 合并诊断修复：默认最多 3 轮，可改用例请求/断言/绑定与实际使用的 Mock 分支，
  断言不得删除或降强，Mock 修复热更新到运行中的服务
- 三种运行范围：`full` / `delta` / `cases`
- 证据化报告（未映射范围、未执行用例、待审提案、规则漂移单列）

## 短期（近期迭代）

### 1. 断言强度：从“显式关系”到“规则自动推导”

**现状**：已支持 L1（状态码/存在/结构）、L2（确定值）、L3（`$ref` 跨步骤关系、
同响应 `$field` 关系和受限算术）与 L4（引用两个及以上步骤），并按断言内容自动推导强度。
`db_row.wait_ms` 支持显式异步落库轮询。规则库可声明原文明确的关系配方
（`$response` / `$request` / `$step` / `$op`），A4 只选择 `relation_key` 并为 `$step`
绑定具体步骤名，由框架确定性生成断言；未物化且未声明未覆盖的关系会阻断导入。
受控运算已扩展到 `avg/min/max/count_distinct/pluck`，并配有 20 条标注评估集与
`tools/eval_relation_extraction.py`，覆盖条件公式、百分比、混合运算与跨步骤/请求
混合；v10 基线 14/14 严格命中、P/R/expression_accuracy 均为 1.00。
**下一步**：评估操作数顺序与更长的多条件公式，决定哪些公式值得进入规则层，
哪些只能落成 gap；同时评估是否需要新增“上一个动作的请求字段”节点。
这是“AI 测试能顶事”最关键的一步——只有 L1 时，框架只能判断“接口有没有按契约返回”，判断不了“业务对不对”。

### 2. 造数深度：方言已通，下一步反射与跨库 schema

**现状**：C5a/C5b 已支持 SQLite / PostgreSQL / MySQL 的 apply/teardown 与方言校验；
`data catalog` 现在可以从 DDL、DBML 或真实数据库反射生成结构目录，多 schema 时使用
`schema.table` 限定名。确定性语义候选已落地：注释关键词补结构语义，注释「值=含义」、
DBML `Enum` note、`CHECK IN`/`ENUM`、规则 `eq/in` 常量、Java/Python/Go/TS 枚举
与 Java `static final`、Go `const`、TS `as const` 常量组进入 `value_semantics`，
每条候选带 `source`，冲突会丢弃并告警。跨表状态骨架已支持：`--rule-states`
把同一条件下覆盖多表的 `eq/in` 约束组合成 `DataState`，`in` 按取值组合展开
（单组上限 20），主键/外键/NOT NULL 用确定性占位值补齐并逐条写 gap；
`operator="transition"` 与成对字段（`from_*/to_*`、`old_*/new_*`、`before_*/after_*`）
会生成“迁移前 → 迁移后”的 `target_state` 候选。规则折叠与状态候选都支持同组字段
消歧，同名列跨表时先用唯一字段收窄，不再直接放弃。规则状态已接入
`data plan` → SQLite `apply/teardown` 闭环，并有 e2e 覆盖。多步迁移路径已能生成
`state_paths`（最多 5 步），并展开为 `state_path_*` 多行 DataState：逐迁移状态
生成行，自动分配主键并补齐父子表，可直接进入 SQLite apply/teardown。
迁移后断言已支持受限版本：正例、实体表单列迁移、单列主键时生成只读 `db_row`
断言并在用例结束后执行。
**目标**：补负例“状态保持不变”、异步落库和多行组合的断言，并设计路径终态
同时覆盖多行时的断言定位。当前 C5b 已支持多表多行、父行 `$ref` 引用、单父键
自动关联、歧义阻断和状态路径执行展开。
真实后端几乎不用 SQLite，下一步重点是“结构自动、语义半自动、业务判断仍回需求原文”。

### 3. 文档差异化定位落到门面（已完成）

README 首页现在直接对比“pytest + 一个 LLM”和 AstSight 在门禁、规则事实源、
测试数据、失败定位四件事上的差异，并明确测试数据只有“插表造数 + 下游 Mock”
两个来源。后续只需随能力变化维护这张表，不用再另写一份销售文档。

## 中期

- **gRPC 完整支持**：当前仅 unary；补齐流式与 server reflection。
- **Dubbo 被测入口（sidecar / Triple）**：下游 Mock 已由 agent + Filter 覆盖；Hessian2 sidecar 与 Triple unary（protobuf descriptor 或 Java interface）均已打通，包含 wheel 分发、常驻 provider 样例、提供方画像、DTO/proto 采样、A4 草案、多步骤、诊断/修复、codegen 与执行。目标定位采用直连解析（来源校验 + 代码地址 + 服务级映射 + IP 字面量），已在 Apache Online Boutique 多服务试点验证；不做注册中心选 provider。仍不支持：streaming、reflection、异步/oneway。
- **时间窗口测试**：通过时钟注入或专用测试接口，覆盖频控窗口、跨月零点、超时等真实时间场景（当前以“连续请求立即命中”近似，报告显式标注）。
- **replay 模式**：流量回放生成用例。
- **被删除接口的变更感知**：从 base 分支/OpenAPI 建第二份路由清单，覆盖“接口被删”。

## 长期 / 待讨论

- 断言类型注册表，开放自定义断言插件
- 更多 Web 框架路由识别插件（C2）
- 更多文档格式解析器（C1）
- 报告适配器插件（对接各类测试资产管理平台）

## 非目标（明确不做）

- 用户管理、用例管理 UI、任务调度——AstSight 是引擎，这些交给上层平台
- 自动批准初始用例——入库仍由人审核，不会因为“AI 觉得对”就绕过人；
  仅运行期失败的用例/Mock 修复由 Agent 在硬性护栏内自动执行并留痕

## 参与方式

认领任一项前，先开 issue 说明思路；改动范围大的，先讨论再动工。详见 [CONTRIBUTING.md](CONTRIBUTING.md)。
