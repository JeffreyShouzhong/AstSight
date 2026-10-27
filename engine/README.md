# AstSight Engine · 正式实现

> 项目名：**AstSight**（AST + Sight）｜ CLI：`astsight` ｜ Maven：`io.astsight:astsight-core`
> 正式实现根目录，与 `testhub-proto/`（历史原型）隔离。
> 设计依据：`framework-capability-design.md`（能力设计）+ `agent-architecture.md`（Agent 架构）。

## 当前进度：M1 确定性闭环 + 需求 → 审核用例 → 版本化

M1 的目标不是“功能齐全”，而是**一条可演示、可验证、结论诚实的链路**：

```
A. 代码变更驱动
git diff → 变更符号 → 影响接口 → OpenAPI 画像 → 模板用例
         → pytest 编码 → collect 自检 → 执行 → 确定性归因 → 报告 + 门禁

B. 需求驱动（无规则抽取中间层）
需求/设计文档 + OpenAPI → cases prepare（context/prompt/schema）
                        → Skill/人工产出 cases.draft.json
                        → cases import（校验 + 与已批准库 diff）
                        → 人工审核 cases.review.json
                        → cases approve（写版本化 caseset.json）
                        → run --mode cases（只执行已批准用例）→ pytest → 报告
```

两条入口共用同一套编码、执行与报告；文档驱动不会伪造 git 变更。
**审核前草案不进入用例库，也不会被编码执行。**

| 模块 | 状态 | 说明 |
|---|---|---|
| `contracts/` 契约层 | ✅ | 核心实体 + 用例库契约 `CaseSet/CaseEntry/CaseReview` |
| `orchestration/` 编排骨架 | ✅ | LangGraph 骨架：入口分流 + A4.5 审核门（M1 CLI 走确定性直连） |
| `codegraph/` 接入层 | ✅ | 代码嵌入客户端 + 路由前缀重建 + InMemory 替身 |
| `astsight/changes/` C2 | ✅ | git 两点/三点 diff、AST 变更符号、直接命中 + 图谱反向追溯 |
| `astsight/profile/` C3 | ✅ | OpenAPI 参数/请求体/响应字段/约束 → 画像；无契约标 `contract_incomplete` |
| `astsight/cases/context.py` | ✅ | 需求 section 解析 + OpenAPI 端点目录 → 上下文包（prompt/schema） |
| `astsight/cases/validate.py` | ✅ | 草案结构、端点/字段/断言、需求 quote 反幻觉、路径与大小限制 |
| `astsight/cases/proposal.py` | ✅ | new/update/revive/deprecate/unchanged/kept_out_of_scope 六路 diff |
| `astsight/cases/store.py` | ✅ | 项目级 `caseset.json`：版本、history、review_log、原子写与备份 |
| `astsight/cases/templates.py` | ✅ | delta/full 的确定性模板用例（不参与审核用例编号） |
| `astsight/codegen/` C6 | ✅ | 每接口一个 pytest 文件 + conftest，语法自检 + collect 自检 |
| `astsight/execute/` C7 | ✅ | JUnit XML、确定性归因、只对环境抖动做有界重试 |
| `astsight/report/` C7.4 | ✅ | JSON + Markdown；用例库版本、待审、需求覆盖、drift 单列 |
| `examples/order_service` | ✅ | 18 接口 FastAPI 示例 + 可重置种子数据 + 示例草案 |
| `examples/demo_delta.py` | ✅ | 一条命令跑真实 git / 真实服务 / 真实 pytest |
| `examples/demo_case_review.py` | ✅ | 一条命令跑需求 → 草案 → 审核 → 版本化 → pytest |

### 当前边界（不假装）

- **不接 LLM SDK**：`cases prepare` 只产出上下文包、prompt 与 JSON Schema；
  草案由 Claude Code Skill、其他模型或人工写出，再走同一套校验/审核链路。
- **审核不能自动化**：`cases approve` 必须显式给 `--reviewer`；没有任何“自动全量批准”参数。
  示例 demo 的自动批准仅用于演示，并明确拒绝自动批准 `deprecate`。
- **只支持单请求用例**：v1 不生成 setup/teardown、跨用例捕获或鉴权链路；需要前置状态的
  流程需求写入 `uncovered_requirements`（如 `needs_c5_state_setup`），不伪造可执行用例。
- **断言强度仍为 L1**：主要断言状态码/字段存在/字段值；尚未实现基于需求表达式的 L2–L4 判定。
- **自动“矫正”仅两件事**：生成后 `compile` 自检 + 执行前 `pytest --collect-only`；执行后只对
  连接/超时类环境抖动重试（默认 1 次，上限 3），**不修改业务代码，不猜失败原因**。
- **不做有状态业务编排**：示例服务用 `/_reset` 保证可重复；真实项目的造数与依赖编排属于 C5。
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

解释器（本机已装齐依赖）：

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

Java/Spring 的 API 级变更感知走同一条命令，但必须让 CodeGraph 可用：

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/java-repo --mode delta --base main --target feature \
  --codegraph on \
  --codegraph-module-root /path/to/node_modules \
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



## 需求 → 用例 → 审核 → 版本化

```bash
cd engine
PROJECT=examples/order_service

# 1. 生成上下文包（需求文档 + OpenAPI）
PYTHONPATH=. python -m astsight cases prepare \
  --project $PROJECT \
  --requirements $PROJECT/docs/api-design.md \
  --requirements $PROJECT/docs/requirements.md \
  --base-url http://127.0.0.1:8010

# 产物：$PROJECT/.astsight/casegen/<context_id>/
#   context.json  prompt.md  cases.schema.json  README.md

# 2. Skill/人工读取 context/prompt/schema，写出 cases.draft.json

# 3. 校验并与已批准用例库 diff（只写 proposal，不改 caseset.json）
PYTHONPATH=. python -m astsight cases import \
  --project $PROJECT \
  --context $PROJECT/.astsight/casegen/<context_id> \
  --draft $PROJECT/case_samples/cases.draft.json

# 4. 人工编辑 proposal 下的 cases.review.json（approve / reject + reviewer + note）

# 5. 应用审核结果
PYTHONPATH=. python -m astsight cases approve \
  --project $PROJECT \
  --proposal $PROJECT/.astsight/cases/proposals/<proposal_id> \
  --reviewer alice

# 6. 查看用例库 / 待审 proposal / 需求覆盖
PYTHONPATH=. python -m astsight cases status --project $PROJECT --json

# 7. 只执行已批准用例
PYTHONPATH=. python -m astsight run --project $PROJECT --mode cases \
  --base-url http://127.0.0.1:8010 \
  --reset-url http://127.0.0.1:8010/_reset
```

`cases` 命令参数：

- `prepare --requirements PATH`：可重复；目录会递归读取 `.md/.markdown/.txt`。
  OpenAPI 必须能从 `--openapi`、项目内自动发现或 `--base-url` 拉取；都找不到则报错，
  不猜测字段。
- `import --context PATH --draft PATH`：校验失败退出非 0，且**不修改 store**；
  `--dry-run` 只输出校验与 diff。
- `approve --proposal PATH --reviewer NAME`：必须显式 reviewer；重新校验草案、重新 diff、
  重算 fingerprint，不信任审核文件里的旧字段。store 版本/hash 已变化时拒绝批准并要求重新 import。
- `status --json`：输出 store 版本、approved/deprecated 数量、待审 proposal、需求覆盖。

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
  priority、method/path、参数、header、body、asserts、requirement_refs 的
  `(doc, section, quote)`。`id/source/title/note/rationale/文档 hash/审批信息/时间戳` 不参与。
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

## 产物与门禁

每次运行落盘到 `<project>/.astsight/runs/<run_id>/`：

```
run_meta.json  routes.json  changes.json  changed_apis.json
profiles.json  caseset.json  requirement_coverage.json  pending_proposals.json
cases.json     codegen.json  generated/
results.xml    execution.json  report.json  report.md
```

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
│   ├── cli.py            `python -m astsight run|cases ...`
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
│   ├── demo_delta.py     一条命令演示 delta 闭环
│   └── demo_case_review.py  一条命令演示需求→审核用例→执行
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
- `test_case_store.py`：fingerprint 口径、版本/编号不变量、六路 diff、批准与拒绝、
  history/superseded、幂等 import、乐观锁、空草案保护。
- `test_case_review_e2e.py`：真实服务上的 prepare → import → approve → run 全链路，
  待审 proposal 的 warning/degraded/blocker 行为。
- `test_case_orchestration.py`：A4.5 审核门前置阻断与 code-only 旁路。
- `test_codegraph_embedded.py`：CodeGraph 嵌入、已知路由缺陷的修复、证据等级，
  以及 Java/Spring 的真实分支 diff → 受影响 API 闭环（合成 Maven 项目）。
  需要 Node >= 22.5 与 `@colbymchenry/codegraph`；可通过
  `ASTSIGHT_CODEGRAPH_MODULE_ROOT` 指定 node_modules，环境不具备时显式 SKIP。

## 下一步

- 接 A1/A4/A5 Agent：`prompts/requirements_to_cases.md` 已定义生成契约，
  `prompts/a5_result_diagnosis.md` 已改为基于 `caseset.json` 与需求 quote 的归因。
- C5 数据准备：真实项目的造数、依赖编排与状态清理（把 `needs_c5_state_setup` 变成可执行）。
- 断言强度升级：在已审核用例上增加 L2/L3 判定（字段约束、状态迁移）。
- 文档接入扩展：PDF/Word、跨文档冲突裁决。
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
