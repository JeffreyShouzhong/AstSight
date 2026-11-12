# CodeGraph 集成设计

> 依据：`framework-capability-design.md`（能力设计 v4.2）、`engine/codegraph/`（客户端与路由重建）
> 输入：`@colbymchenry/codegraph` **1.6.2** 实包核对（`dist/*.d.ts` 全量类型）+ 本地嵌入样例 + 本轮 11 组可复现实测
> 版本：**v2.0** · 2026-10-25（v1.0 见文末修订记录）

---

## 〇、结论先行

**接入方式已定：代码嵌入，不再走 MCP，也不以 CLI 为主。**

v1.0 的判断是「CLI 为主 + MCP 仅 agent 探索」，本轮推翻其中两条：

| v1.0 | v2.0 | 依据 |
|---|---|---|
| CLI 子进程为主 | **代码嵌入**（常驻 Node worker 直接 import 库） | CLI 的 `--json` 只覆盖 impact/callers 少数子命令；路由、图健康度、按 kind 取节点**只在库 API 里** |
| MCP 不进管道 | 仍然不进管道，但**理由升级** | 不只是"输出面向模型"——嵌入模式下我们自己定义每个方法的出入参，可控性本质不同 |
| `find_routes` 是唯一缺口 | **缺口不是"有没有 API"，而是"API 的语义对不对"** | `getRoutingManifest` 存在，但实测有 4 个静默缺陷，直接用它等于批量产出错误 URL |

**最重要的一条认知修正**：

> 评估第三方能力时，「有 API」只是入场券。必须区分三种状态：
> **缺失**（不报错、返回空，安全）、**权威正确**（可依赖）、**静默错误**（不报错但结果是错的）。
> 第三种最危险——它让下游全链路"运行正常"，只是结论全错。

CodeGraph 的路由能力正好落在第三种。这是本轮全部实测的主线。

---

## 一、实测事实卡

### 1.1 环境

| 项 | 值 |
|---|---|
| 包版本 | `@colbymchenry/codegraph` 1.6.2（原生内核按平台放在子包内；1.6.2 的 `optionalDependencies` 含 `codegraph-{win32,linux,darwin}-{x64,arm64}` 六个平台子包） |
| 模块形态 | **CommonJS**。ESM 具名 import 会抛 `SyntaxError: Named export 'CodeGraph' not found`，必须 default import 或 `createRequire` |
| Node 要求 | 库模式 **>= 22.5**（依赖内置 `node:sqlite`） |
| 实测环境 | Node 22.22.2 / Windows 11 / Python 3.13.14 |
| 平台支持（上游） | 上游 README 的 **Supported Platforms** 声明：每个 release 为 **Windows / macOS / Linux × x64 / arm64** 提供自包含构建（自带 Node runtime，无需编译）；1.6.2 实际发布的平台子包为 `codegraph-{win32,linux,darwin}-{x64,arm64}` |
| 平台验证（本项目） | 本文件全部实测**只覆盖 `win32-x64`**。上游有产物 ≠ 本项目已验证：**部署基线 Linux** 与 macOS 开发机在落地前必须复验原生模块可加载、索引/路由结果一致，不能把 Windows 结论直接外推 |

### 1.2 库 API 全清单（来源：`dist/index.d.ts`，非推测）

v1.0 标注"未见 `getCallees`（待实测）"——**已确认存在**，且远不止这些：

| 类别 | 方法 | 备注 |
|---|---|---|
| 生命周期 | `init` / `initSync` / `open` / `openSync` / `recreate` / `isInitialized` / `close` | **`init` 不幂等**，已初始化目录再 init 抛错 |
| 索引 | `indexAll(opts)` / `indexFiles(paths)` / `sync(opts)` / `getChangedFiles()` | `sync` 跨进程写锁冲突抛 `LockUnavailableError` |
| 监听 | `watch(opts)` / `unwatch()` / `isWatching()` / `getPendingFiles()` | 有降级状态：`isWatcherDegraded()` |
| 检索 | `searchNodes(q, opts)` / `getNodesByName(n)` / `getNodesByNamePrefix` / `getNodesByNameSubstring` / `getNodesByKind(k)` / `getSegmentMatches` | 精确名查找能绕过 FTS 分词的漏召回 |
| 图遍历 | `getCallers(id, depth)` / **`getCallees(id, depth)`** / `getImpactRadius(id, depth)` / `findPath` / `getAncestors` | `Subgraph.nodes` 是 **Map** |
| 路由 | **`getRoutingManifest(limit)`** + `getNodesByKind("route")` | 见 1.3，语义有缺陷 |
| 上下文 | `buildContext(input, opts)` / `getExploreMissDiagnostics` | markdown 包，**只给人/Agent 读** |
| 元信息 | `getStats()` / `getFiles()` / `getDetectedFrameworks()` / `getFileDependencyPairs(minConf)` | |
| 健康度 | `findCircularDependencies()` / `getTopDependedOn(n)` / `getTopCallingFiles(n)` | |

`NODE_KINDS` 共 22 种：`file/module/class/struct/interface/trait/protocol/function/method/property/field/variable/constant/enum/enum_member/type_alias/namespace/parameter/import/export/**route**/component/union`。

`EDGE_KINDS` 共 13 种：`contains/calls/imports/exports/extends/implements/references/type_of/returns/instantiates/overrides/decorates/navigates`。

### 1.3 路由能力实测矩阵

同一份代码分别用两种等价写法，结果完全不同：

| 写法 | 图谱 route 节点 | 说明 |
|---|---|---|
| `@app.route("/x", methods=["POST"])` | **1** ✅ | 唯一被 Flask resolver 识别的形式 |
| `@app.post("/x")` | **0** ❌ | Flask 2.0+ 官方主推写法，**识别不出** |
| `@app.get("/x")` | **0** ❌ | 同上 |
| `@bp.route("/x", methods=["PUT"])` | **1** ✅ | 但 `url_prefix` 丢失 |
| `@bp.get("/x")` | **0** ❌ | 同上 |
| Django `path("orders/", views.f)` | **1** ✅ | 无 method、无前导斜杠 |
| Django `path("api/v1/", include("shop.urls"))` | **1**（假 route） | 前缀不拼，且被计为一条 route |

**为什么 `_probe` 里 `@app.get` 看起来能命中**：那次项目同时声明了 fastapi 依赖，**FastAPI resolver 被激活**，它按 `@app.get/post` 的形状把 Flask 文件也扫了。纯 Flask 项目里没有任何 resolver 认这种写法。

> **结论**：路由识别是**按装饰器字面形状**匹配，不是按框架语义。这决定了「框架插件」不是可选增强，而是必需品。

### 1.4 四个静默缺陷（全部可复现）

| # | 缺陷 | 实测输出 | 真实值 | 危害 |
|---|---|---|---|---|
| 1 | **Flask Blueprint `url_prefix` 不拼接** | `/orders` | `/api/v1/orders` | 请求打错 URL → 404 → 失败归因到无关模块 |
| 2 | **Django `include()` 前缀不拼接** | `orders/` | `/api/v1/orders/` | 同上 |
| 3 | **Django 无 HTTP method** | `orders/` | 需从 view 内 `request.method` 分支推断 | 无法构造请求 |
| 4 | **route 节点数与 manifest 条目数不一致** | 节点 4 / manifest 3 | — | include() 被当作一条 route，污染统计 |

另有两个**数据静默丢失**（不报错，值直接没了）：

| # | 现象 | 原因 |
|---|---|---|
| 5 | `impact.nodes` 序列化成 `{}` | 类型是 `Map<string,Node>`，`JSON.stringify(Map)` → `{}`。样例脚本 `reports/library-analysis.json` 里的 impact 数据**实际全丢了**，脚本却一路正常 |
| 6 | `getRoutingManifest()` 返回 `null` | 有效路由 < 3 条时主动返回 null（防抖），不是错误。微服务/单端点服务因此完全拿不到图证据 |

### 1.5 框架检测依赖清单文件

| 实验 | frameworks | route |
|---|---|---|
| 有 `requirements.txt`（flask） | `["flask"]` | 按 1.3 规则 |
| 无依赖清单 | `[]` | 0 |

框架检测读的是依赖清单（`requirements.txt` / `pyproject.toml` / `package.json`），**不靠 import 语句推断**。副作用：多项目根仓库中子目录的 `requirements.txt` 可能不被看到——WHartTest（889 文件、561 个 Python 文件、明显 Django 项目）实测 `frameworks` 只有 `["vue","vue-router"]`，Django 完全未识别，39 个 route 节点全是 Vue 前端路由。

---

## 二、接入方式选型（定稿）

| 方案 | 形态 | 结论 |
|---|---|---|
| **A. 代码嵌入（选中）** | 常驻 Node worker 内 `import` 库，stdio 逐行 JSON | ✅ **唯一正式路径**。可用全部库 API；出入参自定义；一次启动多次调用，摊掉 SQLite 打开开销 |
| B. CLI 子进程 | `codegraph … --json` | ❌ 退为诊断工具。子命令覆盖面远小于库 API（路由、kind 查询、图健康度都没有） |
| C. MCP | 挂 MCP server | ❌ 不进 C2/C3/C5。输出面向模型阅读，无法作为结构化事实源 |
| D. 直读 SQLite | Python 只读 `.codegraph/codegraph.db` | ⚠️ 仅诊断。耦合内部 schema |

### 为什么"嵌入"和"不用 MCP"不是一回事

```
MCP：      AstSight ──协议──> codegraph MCP server ──> 库
            （工具由 server 定义，返回面向模型）

代码嵌入：  AstSight ──自己的 JSON──> 我们的 worker ──> 库
            （方法与返回结构由 AstSight 定义）
```

否决 MCP 否决的是**协议层与面向模型的输出**，不是"跨进程"。CodeGraph 是 Node 库、AstSight 是 Python，跨 runtime 无法消除——但**跨界面的语义由我们掌握**，这才是关键。

---

## 三、实现架构

```
engine/codegraph/
  client.py         CodeGraphClient 抽象 + RouteRef/SymbolRef/ImpactResult
  embedded.py       EmbeddedCodeGraphClient（进程管理、超时、降级）
  route_repair.py   RoutePrefixRepair（路由前缀重建，AST）
  memory.py         InMemoryCodeGraphClient（测试替身，接口不变）
  bridge/
    codegraph_worker.mjs   常驻 worker，直接 import 库
```

### 3.1 worker 协议（不是 MCP）

一行一 JSON：

```json
请求  {"id": 1, "method": "impact", "params": {"root": "...", "nodeId": "...", "depth": 3}}
响应  {"id": 1, "ok": true,  "result": {...}}
      {"id": 1, "ok": false, "error": {"code": "not_indexed", "message": "..."}}
```

方法集：`ping` / `index.ensure` / `stats` / `search` / `nodes.byname` / `callers` / `callees` / `impact` / `routes` / `context` / `graph.health` / `close`。

启动时先发一条 `{"event":"ready","node":"v22.22.2","nodeOk":true}` 握手，Python 侧据此判断 Node 版本是否满足库模式。

### 3.2 三条工程细节（都踩过）

1. **`init` 不幂等**：必须 `CodeGraph.isInitialized(root)` 先探测。
   **禁止** `try { open } catch { init }` —— 那样会把 open 的真实故障（文件锁、schema 迁移失败）误判成"未初始化"，然后 init 再失败一次，错误信息彻底跑偏。样例脚本 `analyze.mjs` 就是这种写法。
2. **包是 CJS**：用 `createRequire` 加载，并支持 `ASTSIGHT_CODEGRAPH_MODULE_ROOT` 指定安装位置，避免在 bridge 目录再装一份依赖。
3. **`Subgraph.nodes` 是 Map**：worker 里显式 `[...map.values()]` 再序列化，`roots` 是 node id 数组需回查。

### 3.3 路由前缀重建（route_repair.py）

**策略：确定性交叉校验，不做无脑覆盖。**

用 AST 重建完整路径，与图谱给的路径做**后缀校验**：

| 情形 | 结果 | confidence |
|---|---|---|
| 图谱路径 == 重建路径 | 采用 | `verified` |
| 图谱路径是重建路径的完整路径段后缀 | 采用重建路径（前缀被补上） | `verified` |
| 图谱有同名 handler 但路径对不上 | 保留双方，不猜 | `unmapped` |
| 只有图谱证据 | 保留 | `inferred` |
| 只有重建证据 | 补入，`source=framework_plugin` | `verified` |

后缀判定按**完整路径段**：`/orders` 是 `/api/v1/orders` 的后缀，但不是 `/api/v1/backorders` 的后缀。

**尾斜杠必须保留**：Django 的 `path("orders/")` 与 `path("orders")` 是两个 URL（`APPEND_SLASH` 对缺斜杠做 301），Flask 的 `/orders` 与 `/orders/` 也互不兜底。统一 strip 会让请求多一跳甚至直接 404。

覆盖范围（Python 先行）：

| 框架 | 支持 | 说明 |
|---|---|---|
| Flask | `@app.route` / `@app.get/post/...` / `@bp.route` / `@bp.get/...` / `Blueprint(url_prefix=)` / `register_blueprint(url_prefix=)` 覆盖 | 补上图谱的 shortcut 漏识别与前缀丢失 |
| Django | `path` / `re_path` / `include` 链展开 / 根 urlconf 自动定位 | 补上 include 前缀；method 缺失如实保留空串 |
| Spring | 插件未实现；图谱原生支持 | CodeGraph 1.6.2 的 Java resolver 已能拼接类级 `@RequestMapping` 前缀并识别方法级 mapping。AstSight 没有 Spring 源码侧第二来源，因此 Spring 路由一律标 `inferred`，不能升 `verified` |

### 3.4 审计报告

每次 `find_routes()` 产出 `RouteRepairReport`：

```
{'scanned_files': 3, 'declarations': 1, 'matched': 1, 'prefix_repaired': 0,
 'plugin_only': 0, 'conflicts': 0, 'graph_only': 0, 'frameworks': ['flask']}
```

`matched` = 双源交叉命中（可信度最高），`plugin_only` = 图谱漏报，`conflicts` = 两路矛盾。
**这个报告要进画像**：`matched` 占比过低说明该仓库的图谱路由不可信，C3 的门禁应据实放宽或转人工。

---

## 四、与框架能力的接线

### C2 变更感知

实现口径是 **API 级，不是方法级**：方法只是追溯的中间证据，最终输出只保留
`ChangedAPI[]`。

1. `git diff base...target` → 变更文件与增/删行区间；
2. 变更行 → 变更符号：
   - Python：AST 解析，行→符号 `verified`；
   - Java/Kotlin/TS 等：`getNodesInFile(filePath)` 取 CodeGraph 符号表，
     `git diff 行区间 ∩ startLine..endLine`，不在 AstSight 里用正则或缩进猜符号边界；
3. `impact(nodeId, depth=5)` 反向闭包；
4. **闭包 ∩ 路由 handler** → ChangedAPI（接口级）。

> 粒度必须守住：`impact` 是**符号级**，C2 要的是**接口级**。
> `EmbeddedCodeGraphClient.impact()` 内部已完成求交并返回 `affected_routes`，
> 调用方不要拿 `affected`（符号名列表）当接口清单用。

**CodeGraph 不覆盖的两块（C2 自行补齐）**：

- **只收提供方端点**：路由表/图谱里的 HTTP 客户端、`@DubboReference`、gRPC stub 是消费方调用点，
  不是被测端点；它们写入 C3 `downstream_deps`，不进 `ChangedAPI[]`。
- **纯契约 diff**：`.proto`、OpenAPI、Dubbo 接口签名的 base/target 差异不来自符号调用图，
  由 AstSight 契约解析器生成 `change_kinds/contract_change`；图谱无变更符号时也可能有契约变更，
  不得因 `affected_routes` 为空就判定“无影响”。

非 Python 映射的证据等级：

| 变更行位置 | 处理 | 证据 |
|---|---|---|
| 方法/函数体内 | 该符号进入反向追溯 | `verified` |
| 类声明、字段、共享常量 | 该类全部方法进入追溯（API 级宁可多报） | `inferred` |
| import / 包声明 / 类外语句 | 整个文件进入追溯 | `inferred` |
| 纯删除（无新增行） | 当前工作树符号表对不上旧行号 → 整个文件 | `inferred` |
| 删除文件 / 纯重命名 | 按 handler 文件命中路由 | `inferred` |
| 语言不支持或索引未同步 | 进 `unsupported`，不猜 | `unsupported` |

**重载消歧**：Java 同名方法很常见。`symbols.infile` 返回的节点 id 会一路带到
`impact(..., node_id=...)`；按名反查（`getNodesByName`）只作为没有节点 id 时的回退。

**已删除接口的边界**：路由清单取自**当前工作树**，因此被删除的路由不会出现在
`routes.json` 里，也不会成为 `changed_apis`；纯删除只会通过文件级兜底影响同文件
现存路由。若要覆盖「接口被删除」，必须从 base 分支或 OpenAPI 再建一份路由清单
（当前未实现，报告中不得声称已覆盖）。

**深度截断**：`Subgraph` 不返回 `depthUsed`。worker 沿反向边做 BFS 计算实际深度，
只有确实到达 `depth` 上限且该层还有节点时才标 `truncated`；不能把请求深度直接当
实际深度，否则所有图谱影响都会被误降成 `low/unmapped`。

**Spring 路由**：路由来自 CodeGraph 原生 Java resolver，路径可用（类级前缀已实测
拼接），但 AstSight 没有源码侧第二来源，因此 `confidence=inferred`。Spring 路由
覆盖率缺口（官方自选样例 83.3%）仍会表现为「漏掉某些接口」，报告必须保留
`unmatched/unresolved`，不能把“没看到”当“没影响”。

外部 CI 判断"要不要跑"时可用 `sync()` 增量，但注意 `LockUnavailableError`：此时索引本身可用、只是没更新，必须如实上报，不能静默当已同步。

### C3 接口画像 / 下游识别

**端点清单的来源顺序（本轮重排）**：

1. **框架插件为主**（`route_repair`）——唯一能给出完整路径与 method 的来源
2. **图谱为辅**——提供 handler↔端点映射的第二方证据，用于交叉校验
3. OpenAPI / DDL 三方校验（沿用基线）

图谱的 handler 映射在 `@app.route` 形式下是准的，可作为独立第二来源；在 shortcut 形式下直接缺失，此时只有插件单方证据。

下游识别仍走 `callees` 递归，命中 HTTP client / gRPC stub / Dubbo reference / MQ producer /
Redis / 三方 SDK 调用点。**HTTP 下游**统一按出站调用点处理：图谱提供调用点与源码，目标地址、
端口与可覆盖配置由 S3 提取（`type=http`），不区分客户端框架、不新增 transport；同一
`(METHOD, 规范化目标 URL)` 的多个调用点合并为一个 `dep_id`，不同下游同路径不合并（见能力设计
C3.5.5）；识别不出的调用点标 `unidentified` 进缺口清单。**gRPC/Dubbo 的契约文档（proto/descriptor、接口签名、
`@DubboService`/`@DubboReference`、XML）不由 CodeGraph 提供**——图谱只提供符号与调用边，
`transport`、`param_types`、`group/version` 由 AstSight 自有解析器确定性提取；不得把同名符号
当作 Dubbo 接口证据。

### C3.5.1 Mock 语义完整性

从下游调用点所在方法的**源码片段**取分支条件，反推「被测接口消费了下游响应的哪些分支」。
**CodeGraph 给的是调用点位置和源码，语义分支要自己提**——确定性 AST 提取为主，标 `inferred` 的才交 LLM 复核。

### C5 DB 血缘 —— 明确不要指望图谱

CodeGraph 是**符号调用图**，没有表/列级读写血缘。图谱最多追到 ORM / mapper 调用点；列级 `DBLineage` 仍靠 DDL + SQL 解析 + 设计文档。

（`READS_TABLE`/`WRITES_TABLE` 那套来自另一个项目 `raghurammrsd/CODE_GRAPH_MCP`，不要混淆。）

---

## 五、工程红线

1. **遥测必须关**：`CODEGRAPH_TELEMETRY=0` + `DO_NOT_TRACK=1`（客户端已默认注入）。企业代码仓场景，默认开启不可接受。
2. **Node >= 22.5**：不满足时 **`CodeGraphUnavailable` 早失败**，不允许静默降级成空图谱——空图谱会让所有接口判"无影响"，看起来一切正常，实际什么都没测。
3. **禁 `try/catch` 吞 open 错误**：先 `isInitialized()`，再决定 open/init。
4. **Map 必须显式展开**：任何从 Subgraph 出来的数据，序列化前确认不是 Map。
5. **一仓一写者**：并发写同一 `.codegraph/` 会锁冲突，多进程分析需串行化。
6. **`.codegraph/` 进 `.gitignore`**，不进制品。
7. **不支持即空 + `unsupported`**，禁止降级猜测。
8. **版本锁定 1.6.2**，升级前必须重跑 `test_codegraph_embedded.py`——路由相关的四种缺陷都是版本敏感的。
9. **`context()` 只给人/Agent**：`buildContext` 返回的 markdown 包禁止进判定管道。
10. **非 Python 符号边界只能来自 CodeGraph**：`getNodesInFile` / `getNodesInFiles`
    是唯一入口；禁止在 AstSight 里为重载、注解、缩进另写一套解析器。

---

## 六、未知项状态

v1.0 列了 6 项必测，本轮关闭 4 项：

| # | v1.0 未知项 | 状态 |
|---|---|---|
| 1 | 路由 kind 名与字段 | ✅ **关闭**。kind = `route`，字段 `{url, handler, handlerFile, handlerLine, handlerKind, routeId, routeFile, routeLine}`。但语义有 4 个缺陷（1.4） |
| 2 | Spring 实测识别率（对照官方 83.3%） | ⚠️ **合成项目已测**：类级 `@RequestMapping` 前缀、方法级 `@GetMapping`/`@PostMapping`、`helper → service → controller → route` 反向闭包均通过。真实仓库召回率分母仍未做 |
| 3 | Python 栈被测框架 | ✅ **关闭**。Flask / Django / FastAPI resolver 均存在；Flask shortcut 与 Django include 有缺陷 |
| 4 | `getCallees` 是否存在、kind 枚举 | ✅ **关闭**。存在；22 种 kind 已全量记录 |
| 5 | 全量索引耗时与体积 | ⚠️ **部分**。889 文件 → 61MB db、19,574 节点；更大型未测 |
| 6 | JSON 是否携带诚实标记（FACT/POSSIBLE/UNKNOWN） | ✅ **关闭：不携带**。`--json` 与库返回都没有该字段。**基线全文的 `verified/inferred/unmapped` 只能由 AstSight 自己判定**，不能"对齐 CodeGraph 诚实标记" |

新增未知项：

| # | 未知项 | 验证方法 |
|---|---|---|
| 7 | Django `re_path` / DRF `router.register` / 多 settings 的 URL 树 | 真实 Django 项目实测 |
| 8 | Java/Spring 的 `@RequestMapping` 类级路径拼接是否也有类似前缀丢失 | ⚠️ **合成验证通过**：`@RequestMapping("/api/v1/orders")` + `@GetMapping("/{id}")` → `/api/v1/orders/{id}`。5 个真实 Java 仓库的人工分母仍未做 |
| 9 | `route` 节点在增量 `sync` 后是否残留已删除路由 | 删路由 → sync → 查 manifest |

---

## 七、对设计基线的回写

| 位置 | 原文 | 改为 |
|---|---|---|
| 2.3 关键设计决策 | 「CodeGraph 通过 **MCP 挂载接入（仅此一种方式）**」 | 「通过**代码嵌入**接入（常驻 Node worker 直接 import 库）；MCP 不进 C2/C3/C5；CLI 仅诊断」 |
| 2.3 同段 | 「数据模型与证据等级口径**对齐 CodeGraph 的诚实标记**（FACT/POSSIBLE/UNKNOWN）」 | **删除此说法**。实测 CodeGraph 不提供诚实标记，`verified/inferred/unmapped` 由 AstSight 自行判定 |
| 5.4 扩展点 | 「新 Web 框架路由识别 → C2 路由插件接口」 | 明确其**第一用途是补 CodeGraph 的路由缺陷**（前缀丢失 + shortcut 漏识别 + Django 无 method），是必需品 |
| 七 风险表 | 「Java/Spring 路由识别精度不足·高」 | 追加：**Python 栈也有语义缺陷**，风险从"某语言"升级为"全栈"，缓解手段是路由插件交叉校验 |
| 八 迭代 1 | C2 变更感知 | 出口标准增加「路由审计报告中 `matched` 占比达标」 |
| C3 下游识别（v4.6） | HTTP 下游 | `callees` 定位出站 HTTP 调用点；目标地址/端口与可覆盖配置由 S3 提取，`type=http`，不区分客户端框架、不新增 transport；识别不出标 `unidentified` |
| C3 下游/契约（v4.2） | gRPC stub、Dubbo reference | `callees` 只提供调用点与符号；gRPC 契约取 repo proto/descriptor，Dubbo 契约取接口源码 + 全限定注解/XML。图谱不提供 `transport`、`param_types`、`group/version` 证据；无源码接口标 `unsupported` |

---

## 附一、证据等级

| 来源 | 等级 |
|---|---|
| `dist/index.d.ts` 全量类型定义 | **A（一手）** |
| 1.3 / 1.4 / 1.5 的实测矩阵 | **A（一手，可复现）**：`engine/tests/test_codegraph_embedded.py` |
| 官方 README 的框架识别率（Spring 83.3% / Django 74.1%） | A，但**仅为官方自选样例**，与本轮实测的"语义缺陷"不矛盾——识别率统计的是"能不能看到端点"，不统计"路径对不对" |
| 本文件第三、四节实现方案 | **A/B（已落地并有测试）** |

> 补充：官方声称 Flask 100%、Django 74.1%。本轮实测 Flask 在 `@app.route` 下确实 100%（端点级），但**路径前缀错误率 100%**（只要用了 Blueprint）。
> **「识别率」这个指标本身有误导性**：它度量"是否看到端点"，不度量"看到的是否正确"。
> 这正是 〇 节说的第三种状态——静默错误不会被识别率指标捕获。

## 附二、v1.0 → v2.0 修订记录

| 项 | v1.0（2026-10-22） | v2.0（2026-10-25） |
|---|---|---|
| 接入方式 | CLI 为主，库 API 为演进 | 代码嵌入（库 API）为唯一正式路径 |
| 路由结论 | "唯一缺口：无 find_routes 库方法" | **错误已纠正**：有 `getRoutingManifest`，但有 4 个静默缺陷 |
| 路由可用性 | 预期"有 API 即可用" | 实测"有 API ≠ 语义可用"，插件为主 |
| 证据等级来源 | 对齐 CodeGraph 诚实标记 | **CodeGraph 无诚实标记**，自行判定 |
| 落地物 | 仅设计 | 设计 + worker + 客户端 + 前缀重建 + 19 项断言 |
