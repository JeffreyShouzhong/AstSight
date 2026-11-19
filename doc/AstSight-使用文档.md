# AstSight 使用文档

| 项 | 内容 |
|---|---|
| 适用版本 | M1 确定性闭环 + 规则驱动用例 + SQLite/PostgreSQL/MySQL 造数 |
| 命令入口 | `python -m astsight`（在 `engine/` 下运行） |
| 主要输入 | 代码库、需求/设计文档、HTTP/gRPC/Dubbo 契约源（可选）、DDL/seed（可选） |
| 主要输出 | 规则库、用例库、造数计划、pytest 用例、执行报告与门禁结论 |
| 运行环境 | Linux 为目标环境；Windows 可用于开发调试 |

本文覆盖整个 AstSight 项目的安装、配置、标准流程、各能力命令、运行模式、产物、
门禁和排错。C1 只是其中一个步骤，单独展开见
[C1 规则生成使用文档](C1-规则生成使用文档.md)；本文第 7 节给出可直接执行的版本。

---

## 1. 项目定位

AstSight 是一套**接口自动化测试引擎**，不是测试管理平台。它不做用户管理、用例管理 UI
或任务调度，重点解决三件事：

1. 从代码变更中找出**受影响接口**；
2. 用需求/设计文档、接口契约和数据库结构生成**可追溯的规则与用例**；
3. 执行后给出**带证据等级的判定结论**，并把“没看到、没判到、没执行”的范围单独列出。

核心原则：

- 规则和用例必须有原文或契约依据，不能由模型自由发挥；
- 审核必须由人完成，框架不提供“自动全量批准”；
- 覆盖缺口、待审草案、未执行用例和契约漂移不会被静默忽略；
- 模型是可选入口，确定性校验、用例库、造数 SQL 和门禁不依赖模型。

---

## 2. 能力全景

| 能力 | 作用 | 主要命令 |
|---|---|---|
| C1 / A1 | 需求 + 设计（可选）+ ContractModel（OpenAPI / Spring MVC / gRPC descriptor / Dubbo 2.7+）→ `BR` + 能力子集内 `CR` | `rules c1` / `rules prepare` + `rules import` |
| C2 | Git 分支差异 → 变更文件、符号、受影响 API | `run --mode delta` |
| C3 | 路由清单、接口画像、下游调用点 | `run`、`cases prepare`、`mock plan` |
| C3.5.1 | 契约 `success` + 规则分支 → Mock 语义 | `mock semantics`；`agent run` 内部也会生成 |
| C3.5.5 | 明文 HTTP 下游 Mock 服务与调用证据 | `mock plan`、`run --mock-plan` |
| Dubbo 入口 | `@DubboService`/XML 提供方画像 → sidecar/Triple 调用 → 原生断言 | `run --dubbo-targets`、`cases prepare` |
| C5a | DDL/FK/seed → 数据目录与业务态候选 | `data catalog`、`data semantics` |
| A4 / C4 | 规则驱动用例草案 → 校验 → 待审 proposal → 版本化用例库 | `cases prepare`、`cases import`、`cases approve` |
| C5b | 已批准用例 + 数据目录 → 逐用例造数计划 | `data plan`、`data apply` |
| C6 | 用例 → pytest 代码 | `run` |
| C7 | 执行、归因、报告、门禁 | `run`、`cases status`、`rules status` |
| CodeGraph | 非 Python 符号、调用链、Spring 路由 | `codegraph install/status`、`run --codegraph on` |
| Agent | 把上述链路串成一条可选命令 | `agent run` |

---

## 3. 安装与环境

### 3.1 Python 依赖

```bash
cd engine
pip install -r requirements.txt
```

依赖包含 `langgraph`、`pydantic`、`httpx`、`PyYAML`、`pytest`、`fastapi`、`uvicorn` 等。
源码目录运行时需要把 `engine/` 放进 `PYTHONPATH`：

```bash
cd engine
PYTHONPATH=. python -m astsight --help
```

如果包已经安装到当前 Python 环境，可以省略 `PYTHONPATH=.`。后文命令默认都在
`engine/` 目录下执行。

### 3.2 CodeGraph（非 Python 项目必需）

CodeGraph 用于 Java/Kotlin/TypeScript 的符号边界、调用链和 Spring 路由识别。
Python 项目的基础 AST 能力不依赖它，但完整调用链和跨语言符号映射需要。

前置条件：Node >= 22.5。安装到用户级缓存后，所有项目复用：

```bash
PYTHONPATH=. python -m astsight codegraph install
PYTHONPATH=. python -m astsight codegraph status
```

默认安装位置：

- Windows：`%LOCALAPPDATA%\AstSight\codegraph`
- Linux/macOS：`~/.local/share/astsight/codegraph`

`--root` 可覆盖安装目录；`--force` 可强制重装。

### 3.3 运行环境

- 目标运行环境是 Linux（服务器 / CI 容器）；Windows 只用于本地开发调试。
- 路径分隔符、子进程、端口绑定和文件锁按 POSIX 语义设计。
- 被执行的 SUT 需要可访问的 HTTP 地址（`--base-url`）或 Dubbo 直连地址
  （`--dubbo-url`、`--dubbo-target`，或服务级映射 `--dubbo-targets`）；
  两者都没有时只生成、不执行。

---

## 4. 配置

### 4.1 模型配置

`rules c1`、`agent run` 和显式调用模型适配层时需要模型。推荐用配置文件：

```bash
cd engine
cp llm.example.json llm.json     # llm.json 已 gitignore
```

查找顺序为 `--llm-config PATH` / `ASTSIGHT_LLM_CONFIG` → `./llm.json` →
`~/.astsight/llm.json`。`llm.json` 支持 `provider`、`model`、`base_url`、
`auth_style`（Anthropic 的 `x-api-key` / `bearer`）、`api_key`、`api_key_env`、
`timeout`、`max_tokens`、`temperature`、`max_retries`；未知字段会直接报错，便于发现拼写错误。

密钥不要写进命令、脚本、报告或仓库：建议保持 `api_key` 为空，用 `api_key_env`
指向环境变量（如 `ASTSIGHT_LLM_API_KEY`）。环境变量始终覆盖文件值。

DeepSeek 的 Anthropic 兼容端点示例：

```json
{
  "provider": "anthropic",
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com/anthropic",
  "auth_style": "bearer",
  "api_key_env": "ANTHROPIC_AUTH_TOKEN",
  "max_tokens": 32768
}
```

`deepseek-flash` 也能用，但真实验证里它在实体场景生成过非法断言；生成质量要求高时优先
`deepseek-chat`。

| 用途 | 优先变量 | 兼容变量 |
|---|---|---|
| API Key | `ASTSIGHT_LLM_API_KEY` | `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` |
| Base URL | `ASTSIGHT_LLM_BASE_URL` | `ANTHROPIC_BASE_URL` |
| 模型 | `ASTSIGHT_LLM_MODEL` | `ANTHROPIC_MODEL` |
| Provider | `ASTSIGHT_LLM_PROVIDER` | - |
| 鉴权头 | `ASTSIGHT_LLM_AUTH_STYLE` | - |
| 超时 | `ASTSIGHT_LLM_TIMEOUT` | - |
| 最大输出 | `ASTSIGHT_LLM_MAX_TOKENS` | - |
| 最大重试 | `ASTSIGHT_LLM_MAX_RETRIES` | - |
| 配置文件 | `ASTSIGHT_LLM_CONFIG` | - |

示例只写变量名，实际值由运行环境注入：

```bash
export ASTSIGHT_LLM_API_KEY=...
export ASTSIGHT_LLM_BASE_URL=...
export ASTSIGHT_LLM_MODEL=...
export ASTSIGHT_LLM_PROVIDER=openai   # openai | anthropic；缺省按 key 前缀推断
```

也可以使用 Anthropic 兼容变量：

```bash
export ANTHROPIC_AUTH_TOKEN=...
export ANTHROPIC_BASE_URL=...
export ANTHROPIC_MODEL=...
```

环境变量 `ASTSIGHT_*` 优先于配置文件，配置文件优先于 `ANTHROPIC_*`。
没有模型配置时，使用 C1 的两段式流程，或只运行确定性命令
（`run`、`cases import/approve`、`data catalog/plan/apply`）。

### 4.2 数据库

C5 造数支持 SQLite、PostgreSQL、MySQL。方言由 `--db-url` 决定，SQL 占位符与标识符
引用在生成计划时按方言固化；运行或手工 apply 时通过 URL 指定：

```bash
# SQLite（stdlib，无需额外驱动）
--db-url sqlite:////absolute/path/to/service.db

# PostgreSQL（需要 psycopg）
--db-url postgresql://user:pass@127.0.0.1:5432/service

# MySQL（需要 PyMySQL）
--db-url mysql://user:pass@127.0.0.1:3306/service?charset=utf8mb4
```

SQLite 相对路径也可用，但建议使用绝对路径，避免工作目录变化后指错库。
`data catalog` 用 DDL/DBML 时默认 `sqlite`，可用 `--dialect postgres|mysql` 指定；
`--db-url` 反射时方言从 URL 推断，显式冲突会报错。`agent run` 会从 `--db-url` 推断，
也可以用 `--db-dialect` 覆盖。计划里的方言与
`--db-url` 不一致时直接标 `data_dialect_mismatch`，不会把 SQLite 的 `?` 占位符
发到 PostgreSQL/MySQL。

事务语义一致：整批 statement 在一个事务内执行，失败回滚；teardown 按主键逆序删除。
PostgreSQL/MySQL 的建表、迁移与连接池仍由被测项目负责，AstSight 只做用例级造数与回收。

### 4.3 Mock

外部已启动的 Mock 服务通过以下参数接入：

```bash
--mock-admin-url http://127.0.0.1:PORT \
--mock-token ... \
--mock-namespace ...
```

也可以用同名环境变量：`ASTSIGHT_MOCK_ADMIN_URL`、`ASTSIGHT_MOCK_TOKEN`、
`ASTSIGHT_MOCK_NAMESPACE`、`ASTSIGHT_MOCK_RUN_SCOPE`、`ASTSIGHT_MOCK_DRAIN_MS`、
`ASTSIGHT_MOCK_TTL_MS`。

`run --mock-plan plan.json` 会启动内嵌 Mock，不需要手工提供 admin 地址；
它与 `--mock-admin-url` 等外部接入参数不能同时使用。

### 4.4 路径约定

| 路径 | 含义 |
|---|---|
| `<project>/ruleset.json` | 规则库，唯一真相源 |
| `<project>/ruleset.md` | 规则库只读评审视图 |
| `<project>/caseset.json` | 项目级已批准用例库 |
| `<project>/.astsight/rules/` | C1 上下文与草案 |
| `<project>/.astsight/casegen/` | A4 上下文与草案 |
| `<project>/.astsight/cases/proposals/` | 待审 proposal |
| `<project>/.astsight/data/` | C5a/C5b 产物 |
| `<project>/.astsight/runs/<run_id>/` | 单次运行的全部产物 |
| `<project>/.astsight/mock-plan.json` | MockPlan 草案默认位置 |

---

## 5. 工作模型

### 5.1 两条用例来源

- **business 用例**：由需求/设计规则驱动，必须引用 `rule_refs` 和 `requirement_refs`。
- **code 用例**：由代码变更补充，必须引用 `change_refs`，用于 delta 回归。

用例身份是 `(endpoint, case_key)`。`TC-NNNN` 只在批准写库时分配，编号永不复用；
更新或恢复沿用原编号并升版本，旧版本进入 `history`。

### 5.2 两个库

- `ruleset.json`：规则库。只由 `rules c1` / `rules import` 写入，不支持手工编辑。
- `caseset.json`：已批准用例库。只由 `cases approve` 写入，待审内容在 proposal 目录。

`ruleset.md` 是只读视图；规则有误时应修改需求/设计原文后重新生成，而不是改
`ruleset.json`。

### 5.3 人工门禁

- `cases import` 只生成待审 proposal，不改用例库。
- `cases approve` 必须显式提供 `--reviewer`，并重新校验、重新 diff。
- `agent run --approve-as` 只自动批准 `new/update/revive`；`deprecate` 永远拒绝。
- 待审 proposal 默认只产生 warning 和 `degraded`，可用
  `--fail-on-pending-proposals` 升级为 blocker。

---

## 6. 快速开始

### 6.1 先跑内置 demo

```bash
cd engine

# delta 闭环：自动建临时 git 仓库、起示例服务、跑完整链路
python examples/demo_delta.py

# 注入 500 缺陷，验证门禁失败与归因
python examples/demo_delta.py --defect

# 需求 → 用例 → 审核 → 版本化 → 执行（自动批准仅用于演示）
python examples/demo_case_review.py
python examples/demo_case_review.py --no-auto-approve   # 停在待审 proposal

# 规则 → Mock 语义 → 造数 → 用例 → 计划 → 执行（需要模型）
python examples/demo_agent_flow.py --keep
python examples/demo_agent_flow.py --keep --no-auto-approve
```

### 6.2 对已有仓库跑 delta

```bash
cd engine
PYTHONPATH=. python -m astsight run \
  --project /path/to/service-repo \
  --mode delta --base main --target HEAD \
  --base-url http://127.0.0.1:8010 \
  --reset-url http://127.0.0.1:8010/_reset \
  --openapi /path/to/service-repo/openapi.json
```

`--project` 必须是 git 仓库根；delta 需要能执行 `git diff`。

### 6.3 只生成、不执行

去掉 `--base-url`，或显式加 `--skip-execute`：

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/service-repo \
  --mode delta --base main --target HEAD \
  --skip-execute
```

产物在 `.astsight/runs/<run_id>/`，重点看 `changed_apis.json`、`profiles.json`、
`cases.json` 和 `report.md`。

---

## 7. C1：需求/设计 → 规则库

这是整个流程的第一步，也是后续用例生成的依据。C1 只产出规则，不生成用例。

### 7.1 一条命令

```bash
cd engine
PYTHONPATH=. python -m astsight rules c1 \
  --project examples/partner_onboarding \
  --requirements examples/partner_onboarding/docs/requirements.md \
  --design examples/partner_onboarding/docs/design.md \
  --openapi examples/partner_onboarding/openapi.json
```

- `--requirements`：必填，可重复；可以是文件或目录（目录递归读取 `.md/.markdown/.txt`）。
- `--design`：可选，可重复；与需求一起作为 A1 输入。
- `--openapi`：可选；不传时自动发现项目内常见 OpenAPI 路径。C1 同时扫描
  Spring MVC、gRPC descriptor 与 Dubbo 2.7+ 接口，只从各源可证明的能力子集生成
  `CR`；不可证明字段写 `contract_gaps`。
- `rules c1` 是 `rules extract` 的别名。

### 7.2 只有需求文档

```bash
PYTHONPATH=. python -m astsight rules c1 \
  --project examples/partner_onboarding \
  --requirements examples/partner_onboarding/docs/requirements.md
```

### 7.3 多份需求和多份设计

```bash
PYTHONPATH=. python -m astsight rules c1 \
  --project $PROJECT \
  --requirements $PROJECT/docs/requirements.md \
  --requirements $PROJECT/docs/requirements-extra.md \
  --design $PROJECT/docs/design.md \
  --design $PROJECT/docs/api-design.md \
  --openapi $PROJECT/openapi.json
```

### 7.4 只校验，不写库

```bash
PYTHONPATH=. python -m astsight rules c1 \
  --project $PROJECT \
  --requirements $PROJECT/docs/requirements.md \
  --design $PROJECT/docs/design.md \
  --openapi $PROJECT/openapi.json \
  --dry-run
```

`--dry-run` 仍然调用模型，仍会消耗 token；只是不写 `ruleset.json` 和 `ruleset.md`。

### 7.5 参数

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--project` | 是 | - | 项目根目录，规则库写到该目录下 |
| `--requirements` | 是，至少一个 | - | 需求文档，可重复 |
| `--design` | 否，可重复 | 空 | 设计文档，与需求一起作为 A1 输入 |
| `--openapi` | 否 | 自动发现 | OpenAPI 契约文件；其他契约源仍会自动扫描 |
| `--out` | 否 | `.astsight/rules` | 上下文目录，相对 `--project` |
| `--rule-base` | 否 | `ruleset.json` | 规则库路径，相对 `--project` |
| `--max-rules` | 否 | `500` | 单次抽取上限，限制在 1–500 |
| `--prompt` | 否 | `prompts/a1_requirement_analysis.md` | 覆盖 A1 提示词 |
| `--dry-run` | 否 | `false` | 只校验，不写规则库 |
| `--json` | 否 | `false` | 输出机器可读 JSON |

### 7.6 产物

```text
<project>/
├── ruleset.json
├── ruleset.md
└── .astsight/rules/<context_id>/
    ├── context.json
    ├── prompt.md
    ├── rules.schema.json
    ├── rules.draft.json
    └── README.md
```

`ruleset.json` 主要字段：

- `ruleset_id`、`version`：规则库标识与版本；
- `rules[]`：业务规则 `BR`；
- `contract_rules[]`：由 ContractModel 生成的 `CR`，带 `source_kind` 与跨源 `support[]`；
- `conflicts[]`、`gaps[]`、`questions[]`：冲突、缺口、待确认问题；
- `documents[]`：输入文档及哈希；
- `prompt`：提示词版本与模型信息。

`context.json` 额外包含 `contract_model`：四类契约源的 operation/field/source_ref、
能力状态与 `gaps`。CR 只从其中可证明字段生成；跨源相同约束合并到 `support[]`，
不同约束生成 framework conflict。

每条规则带 `rule_key`（稳定键）、`rule_id`（展示编号）、`text`、`constraints`、
`source`（文档/章节/原文引用）、`authority`（`A`/`B`）和 `fingerprint`。

`ruleset.md` 是人工评审视图，包含统计、规则表、冲突、缺口、假设、待确认问题和
枚举未闭合项。**不要手工编辑 `ruleset.json` 或 `ruleset.md`**。

### 7.7 校验门禁

`rules c1` 和 `rules import` 在写库前执行确定性校验：

| 校验 | 说明 |
|---|---|
| 上下文一致性 | `context_id`、文档哈希、提示词哈希必须匹配 |
| 来源可解析 | `source.doc/section/number` 必须能定位 |
| 原文落地 | `source.quote` 必须是章节原文的精确子串 |
| 关键 token | 规则文本里的数字、错误码、枚举值必须出现在 `quote` 中 |
| 约束取值 | `constraints.values`、`error_code` 必须有原文依据 |
| 权威等级 | 只允许 `A`/`B`；C/D 不可作为独立 oracle |
| 枚举闭合 | `in/not_in` 必须显式声明 `exhaustive` |
| 冲突口径 | 冲突必须是“A 说 X，B 说非 X”或明确的分支不可达 |
| schema | 草案必须符合 `rules.schema.json` |

校验失败不会写规则库，模型原始草案保留在上下文目录，命令返回非 0。

### 7.8 两段式流程

模型在框架外运行、需要人工审阅草案，或不希望框架直接调用模型时：

```bash
# 1. 只生成上下文
PYTHONPATH=. python -m astsight rules prepare \
  --project examples/partner_onboarding \
  --requirements examples/partner_onboarding/docs/requirements.md \
  --design examples/partner_onboarding/docs/design.md \
  --openapi examples/partner_onboarding/openapi.json

# 2. 外部模型按 context.json / prompt.md / rules.schema.json 写 rules.draft.json
# 3. 先校验
PYTHONPATH=. python -m astsight rules import \
  --project examples/partner_onboarding \
  --context examples/partner_onboarding/.astsight/rules/<context_id> \
  --draft examples/partner_onboarding/.astsight/rules/<context_id>/rules.draft.json \
  --dry-run

# 4. 校验通过后写入
PYTHONPATH=. python -m astsight rules import \
  --project examples/partner_onboarding \
  --context examples/partner_onboarding/.astsight/rules/<context_id> \
  --draft examples/partner_onboarding/.astsight/rules/<context_id>/rules.draft.json
```

### 7.9 重跑与版本

每次 `rules c1` 都是完整快照：

- 版本号递增；
- 本次未出现的旧规则进入 diff 的 `deprecated`；
- 已修复的 `questions/gaps/conflicts` 不会残留；
- 文档、OpenAPI 或提示词变化会生成新的 `context_id`。

重跑后检查：

```bash
PYTHONPATH=. python -m astsight rules status --project $PROJECT
```

重点看 `business_rules`、`contract_rules`、`status.active`、`questions/gaps/conflicts`
和 `rule_drift_stale_cases`。完整 C1 参数与排错见
[C1 规则生成使用文档](C1-规则生成使用文档.md)。

---

## 8. A4：规则 → 用例草案

`cases prepare` 只生成端点/需求上下文包，不做自由语义分析。A4 的输入是
`ruleset + profiles + mock_semantics + data_catalog`。

### 8.1 生成上下文

```bash
PYTHONPATH=. python -m astsight cases prepare \
  --project examples/partner_onboarding \
  --requirements examples/partner_onboarding/docs/requirements.md \
  --requirements examples/partner_onboarding/docs/design.md \
  --openapi examples/partner_onboarding/openapi.json \
  --data-catalog examples/partner_onboarding/.astsight/data/data_catalog.json
```

参数：

| 参数 | 说明 |
|---|---|
| `--requirements` | 必填，可重复；目录递归读取 `.md/.markdown/.txt` |
| `--openapi` | 可选；缺省自动发现，或从 `--base-url` 获取 |
| `--base-url` | 可选；服务已启动时用于补充契约 |
| `--codegraph auto\|on\|off` | 扫描代码路由的模式；有 OpenAPI 时也会用真实 handler 补下游 |
| `--no-scan-routes` | 不扫描代码路由，只使用 OpenAPI/合成路由 |
| `--data-catalog` | 可选；数据目录随上下文打包，供 A4 判断可造状态 |
| `--mock-semantics` | 可选；Mock 语义随上下文打包，供 A4 绑定下游分支 |
| `--out` | 相对 `--project`，默认 `.astsight/casegen` |

产物：

```text
<project>/.astsight/casegen/<context_id>/
├── context.json
├── prompt.md
├── cases.schema.json
└── README.md
```

`cases prepare` 只生成接口和需求事实，不把规则库写进上下文。A4 仍需读取项目根目录的
`ruleset.json`，所以应先完成 C1；`cases import --ruleset` 用于确定性校验
`rule_refs`。

有 OpenAPI 时也会扫描代码路由，但只保留 OpenAPI 中的端点：命中的路由用真实
`handler/file` 替换合成值，从而带上 `downstream_deps`；代码里独有的内部路由
（如 `/_reset`）不进入 A4 上下文。纯 OpenAPI 项目扫不到代码路由时回退到合成路由，
`context_id` 不因这次扫描而变化。

没有 OpenAPI 时不会中断：画像标 `contract_incomplete=true`、原因
`openapi_missing`，可以扫描代码路由；字段不确定时留空，不猜测。

### 8.2 写 `cases.draft.json`

由模型按 `prompts/a4_case_design.md` 生成，或由人按同一份 schema 填写。最小结构：

```json
{
  "schema_version": 1,
  "context_id": "<照抄 context.json>",
  "ruleset_id": "<照抄 ruleset.json>",
  "ruleset_hash": "<照抄 ruleset.json>",
  "scope_docs": ["requirements.md"],
  "cases": [
    {
      "case_key": "order_stock_insufficient",
      "title": "库存不足时创建订单返回 40001",
      "level": "business",
      "transport": "http",
      "coverage_role": "primary",
      "requirement_refs": [
        {
          "doc": "requirements.md",
          "section": "3.2 库存校验",
          "quote": "库存不足时返回 400 与错误码 40001"
        }
      ],
      "rule_refs": [
        {"rule_key": "RK-0123456789ab", "rule_id": "BR-01"}
      ],
      "preconditions": [],
      "endpoint": "POST /api/orders",
      "category": "negative",
      "priority": "P0",
      "path_params": {},
      "query": {},
      "headers": {},
      "body": {"sku_id": "S001", "qty": 1},
      "asserts": [
        {"kind": "status_code", "op": "eq", "expect": 400},
        {"kind": "body_field", "target": "error_code", "op": "eq", "expect": "40001"}
      ],
      "assert_strength": "L2",
      "rationale": "规则 BR-01 明确给出错误码",
      "mock_bindings": {}
    }
  ],
  "uncovered_requirements": []
}
```

关键约束：

- `endpoint` 必须原样取自画像；
- business 用例至少 1 条 `requirement_refs`，且 `quote` 是 section 原文连续子串；
- business 用例至少 1 条 `rule_refs`，`rule_key` 必须存在且不是过期/无依据规则；
- code 用例至少 1 条 `change_refs`；
- `preconditions[].state_key` 必须存在于 `data_catalog.states`；
- `mock_bindings` 是 `{dep_id: branch}`，dep 和 branch 都必须存在；
- `relation_refs` 选择要物化的规则关系；含 `$step` 的关系还必须用
  `relation_bindings` 把语义别名绑定到更早的步骤名，否则导入阻断；
- 断言值必须来自规则原文或契约；`response_fields` 已包含 4xx/5xx 错误体字段，
  断言未声明字段会产生 `assertion.response_field_unknown` warning；
- `uncovered_requirements[].rule_keys` 填写该 section 下明确未覆盖的 A/B 级规则；
  未引用且未声明的规则会产生 `rule.uncovered` warning。

#### 多请求用例（`steps`）

频控、幂等重试、状态流转、先创建后查询等规则需要连续请求时，用 `steps` 在一条
用例内顺序执行。使用 `steps` 时，顶层 `body/path_params/query/headers/asserts`
必须为空，顶层 `endpoint` 必须出现在某个步骤中。

```json
{
  "case_key": "sms_captcha_from_third_request",
  "endpoint": "POST /api/partner/sms/send",
  "steps": [
    {
      "name": "first",
      "endpoint": "POST /api/partner/sms/send",
      "body": {"mobile": "13800138000"},
      "asserts": [{"kind": "status_code", "op": "eq", "expect": 200}]
    },
    {
      "name": "second",
      "endpoint": "POST /api/partner/sms/send",
      "body": {"mobile": "13800138000"},
      "asserts": [{"kind": "status_code", "op": "eq", "expect": 429}]
    },
    {
      "name": "third",
      "endpoint": "POST /api/partner/sms/send",
      "body": {"mobile": "13800138000"},
      "asserts": [
        {"kind": "status_code", "op": "eq", "expect": 400},
        {"kind": "body_field", "target": "error_code", "op": "eq", "expect": "CAPTCHA_REQUIRED"}
      ]
    }
  ]
}
```

- `steps[].name` 匹配 `[a-z][a-z0-9_]{1,63}`，用例内唯一；`endpoint` 必须取自画像。
- 每步至少 1 条断言，且至少 1 条 `status_code`；断言按该步的画像字段校验。
- `{"$ref": "<step>.<field>"}` 只能引用**更早**步骤的响应字段，可用于
  `body`、`query`、`path_params` 和断言 `expect`；支持嵌套字段 `a.b`，不支持 header。
- 步骤共享同一个 `client` 与 Mock 会话；`preconditions` 在第一步前 apply，
  `mock_bindings` 对整个用例生效；每条用例前仍会调用 `--reset-url`。
- 最多 50 步/用例、单步 50 条断言；不要为凑覆盖把无关接口串成一条用例。
- `db_row` 的 `where` 也支持 `$ref`；`body_schema` 的 `expect` 不做引用解析。

#### 断言语言

| kind | 允许的 op | target / expect |
|---|---|---|
| `status_code` | `eq` / `in` / `between` | target 留空，expect 为状态码、状态码列表或 `[low, high]` |
| `body_field` | `exists` / `eq` / `ne` / `in` / `gte` / `lte` / `contains` | target 为响应顶层字段；`exists` 不需要 expect |
| `header` | `exists` / `eq` / `contains` | target 为 header 名（如 `content-type`） |
| `body_schema` | `matches` | target 为响应状态码（如 `"201"`）；expect 可省略，由 OpenAPI 响应 schema 自动回填 |
| `db_row` | `exists` / `absent` / `count_eq` / `eq` / `ne` / `gte` / `lte` / `unchanged` | target 为表名；`eq/ne/gte/lte` 用 `table.column`；`where` 为等值条件；`unchanged` 比较请求前后整行，可配 `ignore_columns` |

约束与执行语义：

- `body_field.expect` 支持受限关系表达式：`{"$field": "items"}` 引用当前响应字段，
  `{"$ref": "first.id"}` 引用更早步骤字段，表达式运算只允许
  `len` / `sum` / `avg` / `min` / `max` / `count_distinct` / `pluck` /
  `add` / `sub` / `mul` / `div`。`pluck` 用于从列表元素中取单个字段，
  例如 `{"$op": "sum", "args": [{"$op": "pluck", "args": [{"$field": "items"}, "price"]}]}`
  表示明细价格求和。列表为空、`pluck` 缺字段或类型不匹配都会让用例显式失败，
  不会被吞成“跳过断言”。例如：

  ```json
  {
    "kind": "body_field",
    "target": "total",
    "op": "eq",
    "expect": {"$op": "len", "args": [{"$field": "items"}]}
  }
  ```

  表达式由生成器翻译为固定的取值与运算代码，不执行任意 Python。`$field` / `$op`
  仅允许出现在响应断言中，不得用于请求体、query、path 参数或 header。
- 规则库的 `rules[].relations` 可声明带原文证据的关系配方，表达式使用
  `$response` / `$request` / `$step` / `$op`。A4 在用例 `relation_refs` 中选择关系，
  框架会自动生成带 `relation_ref` 的 `body_field` 或 `db_row` 断言，模型不需要重复手写表达式。
  关系无法映射时，必须在 `uncovered_requirements[].relation_keys` 中列明；
  A/B 规则关系既未物化也未声明会阻断提案导入。
- 跨步骤关系用 `$step` 写语义别名，例如规则里声明
  `{"$step": "create.order_id"}`，A4 在用例中绑定具体步骤名：
  `"relation_bindings": {"RR-...": {"create": "create_order"}}`。框架会把别名替换成
  `{"$ref": "create_order.order_id"}` 并挂在用例最后一步；绑定缺失、绑定到最后一步
  或绑定不存在的步骤都会阻断导入。
- `body_schema` 使用 OpenAPI 中该状态码的 JSON Schema（本地 `$ref` 已内联）；
  该状态码没有 JSON Schema 时校验报错。生成的 pytest 需要 `jsonschema`。
- `db_row` 只执行只读 `SELECT`；表名/列名先做标识符白名单校验，值全部参数化。
  声明了 `db_row` 的项目应通过 `--data-catalog` 提供目录，导入时会校验
  表、列和 `where` 列是否存在；缺目录只产生 `assertion.db_catalog_missing` warning。
- `db_row.wait_ms` 可设为 0–60000，0 表示只查询一次；异步落库场景可设置等待时间，
  框架会轮询到断言通过或超时。只有需求/设计明确最终一致时才应使用非零值。
- **异步优先断言消息**：如果异步边界是 MQ/消息，应断言消息是否发出及
  topic/key/payload/headers 与条数（不校验先后顺序），不要改成轮询数据库；只有
  SUT 内部异步（线程池、after-commit hook、定时任务、CDC）没有消息可观测时，
  才用 `wait_ms` 等数据库最终状态。MQ 生产者拦截已支持 Kafka/RabbitMQ/RocketMQ
  与 Python 的 kafka-python/pika；引擎不测试 SUT 的多线程/并发行为。
- 生成用例运行时若未设置 `ASTSIGHT_DB_URL`，`db_row` 断言会 skip 为
  `data_unavailable`，不会伪装成通过。
- 失败场景可加 `db_row` 断言验证“没有副作用”，例如创建失败后
  `{"kind": "db_row", "target": "partners", "op": "absent", "where": {"mobile": "..."}}`。
- 已有行不应被改动时用 `unchanged`：

  ```json
  {"kind": "db_row", "target": "orders", "op": "unchanged",
   "where": {"id": "O-1001"}, "ignore_columns": ["updated_at"]}
  ```

  生成物会在请求前给 `where` 命中的行拍快照，请求后逐列比较；`updated_at`、
  版本号这类允许变化的列放进 `ignore_columns`。`unchanged` 必须给 `where`，
  不接受 `expect` 和 `wait_ms`，只对点名的行生效，不做全表断言。

`assert_strength` 由断言内容自动推导，声明值仅作为设计意图：

- L1：仅状态码、字段存在、`body_schema` 结构校验；
- L2：`body_field` / `header` / `db_row` 的确定值比较；
- L3：同响应字段关系（`$field` / 受限运算），或断言引用了更早步骤的响应（`$ref`）；
- L4：同时引用两个及以上不同步骤（跨响应不变量）。

声明值与推导值不一致会产生 `assert_strength.mismatch` warning，导入以推导值为准。

完整字段规则见 `prompts/a4_case_design.md` 和上下文中的 `cases.schema.json`。

手工执行 A4 时，仍应读取项目 `ruleset.json`（规则不进 context）。数据目录和
Mock 语义可用 `cases prepare --data-catalog/--mock-semantics` 打包进上下文；
未打包时，`cases import` 的 `--data-catalog`、`--mock-semantics` 会在导入时
重新校验。规则库始终由 `--ruleset` 显式提供。

### 8.3 校验并生成待审 proposal

```bash
PYTHONPATH=. python -m astsight cases import \
  --project examples/partner_onboarding \
  --context examples/partner_onboarding/.astsight/casegen/<context_id> \
  --draft examples/partner_onboarding/.astsight/a4/example-run/cases.draft.json \
  --ruleset examples/partner_onboarding/ruleset.json \
  --require-rules
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--ruleset` | 规则库，用于校验 `rule_refs` |
| `--data-catalog` | 数据目录，用于校验 `preconditions` |
| `--mock-semantics` | Mock 语义，用于校验 `mock_bindings` |
| `--require-rules` | 强制每条用例引用规则 |
| `--dry-run` | 只校验和 diff，不写 proposal |

`cases import` 会与已批准用例库做 diff：

| 情况 | 处理 |
|---|---|
| 锚点存在且 fingerprint 相同 | `unchanged`，自动沿用 |
| 锚点存在但 fingerprint 不同 | `update`，待审；旧版本继续有效 |
| 锚点只在 deprecated 用例中 | `revive`，待审恢复 |
| 新锚点 | `new`，待审新增 |
| 已批准用例在 scope 内但草案中消失 | `deprecate`，必须显式批准 |
| 用例文档不在本次 scope | `kept_out_of_scope`，原样保留 |
| 草案为空但库非空 | 拒绝导入，防止清空用例库 |
| 全部 unchanged | 不生成 proposal，报告“无变更” |

待审内容写到：

```text
<project>/.astsight/cases/proposals/<proposal_id>/
├── proposal.json
├── cases.review.json
└── ...（校验结果、原始草案、上下文快照）
```

`caseset.json` 不会被修改。

---

## 9. 人工审核与用例库

### 9.1 编辑审核文件

先读同目录的 `cases.review.md`。它的开头是“审核导航”：按规则冲突、多步骤、
前置状态/其他告警、常规四类分组，并给出建议顺序；冲突 topic 下列出受影响用例。
同一 topic 的用例通常可以采用同一结论。

然后可以用交互命令逐条填写，不必手工编辑 JSON：

```bash
PYTHONPATH=. python -m astsight cases review \
  --proposal examples/partner_onboarding/.astsight/cases/proposals/<proposal_id> \
  --pending
```

每条会显示标题、规则、需求 section、请求/步骤、断言和告警。输入：

- `a` 批准，`r` 拒绝，`s` 跳过，`q` 保存并退出；
- 可带备注：`a 按设计 3.4 裁决`；
- 空输入等同跳过；决定只写回 `cases.review.json`，不会自动批准。

也可以手工打开 `cases.review.json`，把 `decision` 改为 `approve` 或 `reject`。
审核人由 `cases approve --reviewer` 指定，item 内的 `reviewer` 字段不参与批准。
所有条目都不能留 `pending`。

### 9.2 应用审核结果

```bash
PYTHONPATH=. python -m astsight cases approve \
  --project examples/partner_onboarding \
  --proposal examples/partner_onboarding/.astsight/cases/proposals/<proposal_id> \
  --reviewer <your-name>
```

批准时框架会：

- 重新校验草案；
- 重新计算 diff 和 fingerprint；
- 只把 `approve` 的条目写入 `caseset.json`；
- 给新增用例分配 `TC-NNNN`；
- 更新版本、`history` 和 `review_log`。

如果用例库版本/hash 已变化，会拒绝批准，要求重新 import。审核/批准应串行执行，
CI 中不要并发 `cases approve`。

### 9.3 查看状态

```bash
PYTHONPATH=. python -m astsight cases status --project $PROJECT
PYTHONPATH=. python -m astsight rules status --project $PROJECT
```

重点看已批准/废弃数量、待审 proposal、需求覆盖率、规则漂移和过期用例。

---

## 10. C5：数据目录与造数

### 10.1 C5a 数据目录

结构来源三选一：DDL、DBML，或直接从真实数据库反射。

```bash
# 1) DDL
PYTHONPATH=. python -m astsight data catalog \
  --project examples/partner_onboarding \
  --ddl examples/partner_onboarding/schema.sql \
  --out .astsight/data

# 2) DBML
PYTHONPATH=. python -m astsight data catalog \
  --project $PROJECT --dbml schema.dbml --dialect postgres

# 3) 真实库反射（只读，不读取业务数据）
PYTHONPATH=. python -m astsight data catalog \
  --project $PROJECT \
  --db-url postgresql://user:pass@127.0.0.1:5432/service \
  --schema public --include 'order*'

# 以上任意一种都可以再叠加代码枚举/常量组与规则常量
PYTHONPATH=. python -m astsight data catalog \
  --project $PROJECT --ddl schema.sql \
  --code src --code web/src \
  --ruleset ruleset.json
```

可选参数：

- `--seed`：seed JSON，用于补充初始数据；
- `--dialect`：DDL/DBML 的目标方言（默认 `sqlite`）；`--db-url` 时从 URL 推断，显式冲突会报错；
- `--schema`：反射的 schema（PostgreSQL）或 database（MySQL），多个用逗号分隔；
- `--include` / `--exclude`：按表名 glob 过滤反射结果，可重复；
- `--context`：C1 规则上下文，用于字段语义对齐；
- `--ruleset`：规则库 JSON。规则里 `eq/in` 的常量会按字段名唯一匹配折叠成该列的
  取值候选（`rule_keys` 保留依据）；同名列表跨表出现时放弃折叠并告警，不猜表；
- `--code`：扫描 Java / Python / Go / TypeScript 枚举与常量组的目录或文件（可重复）。
  枚举之外还读 Java `static final`、Go `const` 块和 TS `as const` 对象；默认跳过
  `node_modules`、`target`、`build`、`dist`、`.venv`、`__pycache__` 等目录；
- `--rule-states`：需要同时给 `--ruleset`。把同一条件下、覆盖两张及以上表的
  `eq/in` 约束组合成候选 `DataState` 骨架，写入目录的 `states`。主键、外键和
  其它 NOT NULL 列会用确定性占位值补足，并在 `gaps` 中逐条标记，明确哪些值不是
  业务事实。

即使不调用模型，目录也会先做一轮确定性语义候选：

- 表/列注释（DDL `COMMENT`、DBML `Note`、真实库注释）里的关键词用于补
  `semantic_type`（金额/数量/状态/是否/时间/类型等）；
- 注释里的「值=含义」（如 `0=待支付；1=已支付`）与 DBML `Enum` 值上的
  `[note: '...']` 进入 `value_semantics`；
- `CHECK ... IN (...)`、`ENUM(...)`、规则常量与代码枚举/常量组提供取值集合和含义；
- 与数据库枚举冲突的注释/规则/代码取值会被丢弃或告警，不会静默采纳。

代码枚举/常量组只在以下情况映射到列，且必须唯一命中（常量组还要求组名以
`status`/`state`/`type`/`kind`/`code`/`level`/`flag`/`enum`/`category` 或其复数结尾，
避免把超时、日志之类的配置常量当成业务取值）：

1. 数据库已有枚举值（`CHECK IN`/`ENUM`）能在代码枚举里全部找到（最强依据）；
2. 列注释里明确写了枚举类型名，如“订单状态（OrderStatus）”；
3. 列名与枚举名符合「表名 + status/type/code」命名约定，例如 `orders.status`
   对应 `OrderStatus` / `OrderStatusType`。

同一列命中多个枚举时，先比较取值类型是否与列类型一致；仍并列就放弃并写
`code_enum_ambiguous`，不猜。每条候选的 `source` 记录来源，如 `comment`、`dbml`、
`rule`、`enum`、`llm` 或 `code:src/order_status.ts`。这些候选的证据一律是
`inferred`；业务含义仍以需求原文与人工审核为准。

跨表状态候选（`--rule-states`）的边界：

- 跨表组合只处理 `eq/in` 约束；`in` 会按取值组合展开，每条组合一个候选态，单组
  最多展开 20 个组合，超出部分写 `state_value_combinations_truncated` gap；
- `operator="transition"` 的约束按 `[迁移前, 迁移后]` 生成状态转移候选：迁移前值进
  `table_values`，迁移后值进 `target_state`；允许单表，也可以和另一张表的 `eq/in`
  约束组合；
- 同一张表上的成对字段 `from_*/to_*`、`old_*/new_*`、`before_*/after_*` 也会生成
  转移候选：行里同时保存两列（表示一次迁移事件），`target_state` 声明期望的 `to`
  值；`NULL` 会转成 null；
- 约束字段必须映射到某张表的某一列；同名跨表时先用同组其他能唯一映射的字段
  收窄候选表，收窄后仍不唯一才写 `rule_state_field_ambiguous` 并放弃；
- 只支持单列主键和单列外键；复合主键/外键、循环依赖会写 `rule_state_invalid`；
- 主键、外键和被外键牵连的父表由框架补确定性占位值；其它 NOT NULL 列用类型占位，
  每条都写 `state_placeholder` gap；
- 生成后逐条跑 `validate_plan_fixtures`（类型、枚举、FK 闭包），不通过就丢弃并告警；
- 占位值只是“能落库的骨架”，不是业务上正确的数据，必须人工确认或替换；
- 生成的状态写入目录 `states`，可以直接在用例 `preconditions[].state_key` 中引用；
  `data plan` 会展开成 fixtures，`apply/teardown` 按主键参数化执行。规则状态 →
  计划 → SQLite 建表/apply/teardown 已有 e2e 覆盖。

反射只读 `pg_catalog` / `information_schema` / `sqlite_master`，不导出数据。
PostgreSQL 需要 `psycopg`，MySQL 需要 `PyMySQL`；成功反射的表会带上主键、唯一约束、
外键、注释和枚举值。选择一个以上 schema 时，表名统一写成 `schema.table`，造数 SQL
会按方言分别引用；被过滤掉表的外键会同时移除，并在 `warnings` 里记录。

无模型时仍产出骨架：注释/枚举/规则常量能推出的语义先填上，仍缺失的列标
`field_semantics_unmapped` 并进入 `gaps`。

需要模型补字段语义和业务态候选时：

```bash
# 框架外模型：按 prompts/c5_data_catalog.md 写出 draft 再合并
PYTHONPATH=. python -m astsight data semantics \
  --project $PROJECT \
  --catalog .astsight/data/data_catalog.json \
  --draft .astsight/data/semantics.draft.json \
  --ruleset ruleset.json

# 或直接调用模型（需要 LLM 环境变量）
PYTHONPATH=. python -m astsight data semantics \
  --project $PROJECT \
  --catalog .astsight/data/data_catalog.json \
  --llm \
  --context .astsight/casegen/<context_id> \
  --ruleset ruleset.json
```

`--draft` 与 `--llm` 必须二选一。适配层只提供候选，逐项校验表/列/枚举/规则引用，
非法候选整体拒绝；模型不产出 SQL。通过后写回 `data_catalog.json`，其中 `states`
可直接被 A4 的 `preconditions.state_key` 引用。无法用 `table_values` 构造的状态
（例如进程内存里的频控计数）写入 `gaps`，由 A4 改用 `steps` 连续请求覆盖；只有
连续请求也无法构造时才写入 `uncovered_requirements`，不要硬造状态。

顺序上要注意：如果已批准用例声明了 `preconditions.state_key`，必须先用
`data semantics` 把 draft/LLM 候选写回目录，再执行 `data plan`；否则会报
`state_unknown`。draft 里的 `rule_keys` 也必须来自**当前** `ruleset.json`，
规则重抽后旧 key 会失效，需要同步更新示例或重新生成 draft。

查看目录里的业务态候选：

```bash
PYTHONPATH=. python -m astsight data states \
  --project $PROJECT \
  --catalog .astsight/data/data_catalog.json \
  --state rule_BR_01_1a2b3c   # 可选：只看一个状态
```

输出 `state_key`、规则 key、各表行数、`target_state`、占位 gap 和 fixture
校验错误。加 `--json` 输出结构化结果，适合接平台或脚本。规则状态、迁移状态和
seed 状态都会列出来，占位值会逐条说明，不会被当成已确认数据。

如果规则能连成多步迁移（如 `pending → paid → shipped`），目录还会生成
`state_paths`，`data states` 会按 `path_key` 列出每一步的 `from → to`、规则 key
和 condition。路径只是描述规则推导出的状态流转，**不是可直接执行的 fixtures**，
也不会自动展开成多行造数。

### 10.2 C5b 逐用例计划

在用例批准后生成：

```bash
PYTHONPATH=. python -m astsight data plan \
  --project $PROJECT \
  --catalog .astsight/data/data_catalog.json \
  --cases caseset.json \
  --out .astsight/data
```

计划只使用目录中存在的表/列/状态，并校验 FK 闭包、类型、枚举、`preconditions`
覆盖和 SQL 标识符白名单。

迁移后断言（受限）：

- 只对**正例**、且 `preconditions.state_key` 指向 `transition_*` / `state_path_*`
  状态的情况生成；
- 支持多表、多列、多行和复合主键：按主键逐行生成 `where`；
- 行选择规则：已有行等于目标值时只断言这些行（`state_path_*` 的终态行）；
  没有行等于目标值且表里只有一行时断言这一行（`transition_*` 单行迁移）；
  多行且无法确定目标行时跳过并告警；
- 目标列不能是 `to_*/new_*/after_*` 这类成对字段；
- 生成的是只读 `db_row` 断言（`table.column == 目标值`，按主键定位），写入
  `data_plan.json` 的 `post_assertions`；生成物在用例正常结束后、teardown 之前执行；
- 负例、成对字段、无法定位的多行目标不自动断言，只保留 `target_state` 作为线索；
- 断言失败会先让用例失败，然后照常 teardown，不吞掉失败。

### 10.3 手工 apply / teardown

```bash
PYTHONPATH=. python -m astsight data apply \
  --db-url sqlite:////absolute/path/to/service.db \
  --plan .astsight/data/data_plan.json
```

默认执行后立即回收；`--keep-data` 保留数据。事务内 apply，失败回滚。

在 `run` 中自动逐用例造数：

```bash
PYTHONPATH=. python -m astsight run \
  --project $PROJECT --mode cases \
  --base-url http://127.0.0.1:8010 \
  --data-plan .astsight/data/data_plan.json \
  --db-url sqlite:////absolute/path/to/service.db
```

没有 plan/DB 时，带 `preconditions` 的用例标 `blocked`，不会伪造状态或静默跳过。
PostgreSQL/MySQL 分别用 `postgresql://user:pass@host:5432/db` 与
`mysql://user:pass@host:3306/db`；计划方言与 URL 不一致时标
`data_dialect_mismatch`。

---

## 11. Mock

### 11.1 Mock 语义（C3.5.1）

Mock 语义描述每个下游依赖在成功和业务分支下应返回什么。契约 `success` 是确定性生成，
业务分支必须有规则或契约依据；无依据的分支会被拒绝。

手工流程用独立命令生成：

```bash
# 确定性层：每个 dep 固定 success；active 的 error_handling/state_transition/
# data_consistency/validation 规则在文本含失败提示与 dep token 时生成 rule_<BR> 分支
PYTHONPATH=. python -m astsight mock semantics \
  --project $PROJECT \
  --context .astsight/casegen/<context_id> \
  --ruleset ruleset.json \
  --out .astsight/mock_semantics.json
```

需要模型补响应结构/分支时，二选一：

```bash
# 框架外模型：按 prompts/c35_mock_semantics.md 写出 draft
PYTHONPATH=. python -m astsight mock semantics ... \
  --draft mock_semantics.draft.json

# 直接调用模型
PYTHONPATH=. python -m astsight mock semantics ... --llm
```

候选分支必须带 `source.rule_keys` 且规则存在；未知 dep/规则、重复分支、无依据分支
整体报错；模型标 `verified` 的分支降级为 `inferred`。顶层与 per-dep `gaps` 都会合并。
无法从需求/规则得到依据的失败分支只会写入 `gaps`，应回到 A1 补规则，不应在 Mock
层猜响应。

生成后，`cases prepare --mock-semantics .astsight/mock_semantics.json` 把它打包进
A4 上下文，`cases import` 才能校验用例的 `mock_bindings`。`agent run` 也会在内部
执行同一流程；没有语义文件时，不要编造 `dep_id` 或分支。

### 11.2 生成 MockPlan 草案（C3.5.5）

```bash
PYTHONPATH=. python -m astsight mock plan \
  --project examples/order_service \
  --codegraph off \
  --out .astsight/mock-plan.json
```

输出是**草案**：

- `routes[].branches` 默认为空，需要按业务语义手工补充；
- 有配置键但不知道原值的依赖会标 `unsupported` 和 `reason=injection_original_unknown`；
- 有确定目标的依赖默认使用 `http_proxy`；
- 未识别到下游、或文件已存在且未加 `--force` 时，命令失败且不写文件。

最小计划结构：

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

`dep_id` 必须与用例 `mock_bindings` 的键一致。HTTPS 下游标 `tls_not_supported`，
不做 MITM 或降级。

### 11.3 执行时使用 Mock

```bash
# 内嵌 Mock：启动服务、生成注入事实、执行后停止
PYTHONPATH=. python -m astsight run \
  --project $PROJECT --mode cases \
  --base-url http://127.0.0.1:8010 \
  --mock-plan .astsight/mock-plan.json

# 或接入已启动的 Mock
PYTHONPATH=. python -m astsight run \
  --project $PROJECT --mode cases \
  --base-url http://127.0.0.1:8010 \
  --mock-admin-url http://127.0.0.1:PORT \
  --mock-token ... \
  --mock-namespace ...
```

`run --mock-plan` 不启动 SUT，也不改写 SUT 配置；它只负责起 Mock 和产出注入事实。
需要一条命令完成注入与启动时用 `agent run`。

运行结束会核对每个会话的绑定 `dep_id` 是否至少命中一次。零调用的绑定报
`mock_not_used` 并阻塞，避免“用例通过但没走到 Mock”的假绿。晚到、过期或损坏的
证据进入 warning，报告标 `degraded`。

---

## 11A. Dubbo 入口用例（sidecar）

前面几节讲的是“SUT 调下游，把下游挡成桩”。这一节是反方向：**被测服务本身是
Dubbo 提供方**，由 sidecar 泛化调用进去。两者互不替代：

- 下游 Mock：Java agent + 消费端 Filter，注入被测 JVM；
- 被测 Dubbo 入口：独立 Java 进程 `astsight-dubbo-sidecar.jar`，不改、不 Attach 被测 JVM。

### 11A.1 前置

```bash
cd java
mvn -pl astsight-sidecar-dubbo package
# target/astsight-dubbo-sidecar.jar

cd ..
python tools/build_sidecar_wheel.py
# dist/astsight_dubbo_sidecar-0.1.0.dev0-py3-none-any.whl
pip install dist/astsight_dubbo_sidecar-*.whl

cd engine
PYTHONPATH=. python -m pytest tests/test_dubbo_provider_profile.py \
  tests/test_dubbo_case_validate.py tests/test_codegen_dubbo.py -q
```

引擎按 `ASTSIGHT_DUBBO_SIDECAR_JAR` → 已安装 wheel → 仓库构建产物解析 jar；
wheel 会校验 manifest、版本、协议号、sha256 与大小。找不到时用例 blocked
（`dubbo_sidecar_unavailable`），不会回退成 HTTP。

### 11A.2 提供方画像

`build_profiles` 会扫描：

- `@DubboService`、2.7 的 `org.apache.dubbo...@Service`；
- XML `<dubbo:service interface=... ref=... group=... version=...>`。

每个接口方法生成一个 `transport=dubbo` 画像，例如：

```
dubbo:com.acme.OrderService#create(com.acme.CreateOrderRequest)?group=order&version=1.0.0
```

接口源码必须在仓库、参数/返回类型可解析；2.6 注解、接口在 jar、多接口无
`interfaceName`/`interfaceClass`、Kotlin 提供方都会显式 `unsupported`。
Spring 的 `@Service` 不会被误判。

DTO 字段会进入 `parameter_schemas` / `result_schema`：普通字段、嵌套 DTO、
record 组件、枚举常量、必填标记（`@NotNull` / `@NotBlank` / `@NotEmpty`），
以及 `@Size` / `@Min` / `@Max` / `@Pattern` / `@Email` / `@Positive` 等约束。
项目自定义校验注解（如 `@Mobile`、`@Range`、`@EnumValue`、`@Column`）会进入
`constraints.custom`，能识别的参数会推断成 `format` / `minimum` / `maximum` /
`allowed_values` 等。集合/Map/数组带元素 schema，模板最多生成 10 个元素；
`java.time.*`、`java.util.Date`、`UUID`、`URI`、`URL` 单独识别。模板和 A4
都按这些字段构造 `arguments`；`@Pattern` 会尝试生成匹配值，未知第三方类型
按空对象采样并在用例 `note` 里标明。

### 11A.3 目标地址

测试必须钉到具体的 SUT provider，**不使用注册中心**。注册中心只能说明
“存在可用实例”，不能证明返回地址属于本次被测部署；解析不到就阻断，
不拿全局 URL 猜一个。

按优先级解析：

1. **来源校验**：`--dubbo-source-commit` / `--dubbo-source-branch`
   或 `--dubbo-source-sha256` + `--dubbo-artifact`；不一致阻断为
   `dubbo_source_mismatch`，未提供时报告 `dubbo_source_unverified` 告警；
2. **代码/XML 显式地址**：`@DubboService(url=...)`、XML 等；
3. **端点去重**：代码地址收敛到同一个 `protocol + IP + port` 时，
   未解析服务可以共享该 endpoint；
4. **服务级映射**：`--dubbo-targets FILE`，键为
   `interface?group=&version=`；同一 endpoint 下多服务各自指定地址时用它；
   代码地址与映射不一致时默认阻断，加 `--dubbo-target-override` 才允许覆盖；
5. **运行直连地址**：`--dubbo-target IP:port`（可带 `dubbo://` /
   `tri://` / `grpc://`），或 `--dubbo-host IP` + 画像
   `dubbo.protocol.port`；只补映射和代码都没给出的服务；
6. **全局 URL**：`--dubbo-url` 只在所有服务收敛到同一个 endpoint 时可用；
   出现多个 endpoint 时一律阻断；
7. 仍无唯一 endpoint，或出现多个不可消歧的 endpoint →
   `dubbo_target_unresolved` / `dubbo_target_ambiguous`，用例 blocked。

只接受 **IPv4 / IPv6 字面量 + 显式端口**；域名、K8s Service、VIP 别名
一律拒绝，避免“以为钉住了、其实还是负载均衡”。

多服务映射文件示例：

```json
{
  "source": {
    "commit": "b87134655e8cf9c589147312ac9dde59a8978bb2",
    "branch": "master"
  },
  "targets": {
    "com.acme.OrderService?group=order&version=1.0.0": "dubbo://10.0.0.12:20880",
    "com.acme.PayService": "dubbo://10.0.0.13:20881"
  }
}
```

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/sut \
  --mode full --codegraph off \
  --dubbo-targets .astsight/dubbo-targets.json
```

报告会写到 `dubbo_targets.json`，逐服务记录实际 endpoint、来源
（code / code_shared / mapping / mapping_override / direct / direct_host /
global）和代码文件/行号，便于审计。

### 11A.4 运行

纯 Dubbo 项目没有 HTTP 路由，`full` 模式仍可执行：

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/dubbo-service \
  --mode full \
  --codegraph off \
  --dubbo-url dubbo://127.0.0.1:20880
```

有 HTTP 入口的项目同时带 `--base-url` 和 `--dubbo-url` 即可跑两种入口。
多服务/多端口项目改用 `--dubbo-targets`，不要用一个全局 URL 套所有服务。

手工用例走 `cases prepare` → 导入草案 → 审核 → `run --mode cases`。
下面只保留 Dubbo 相关字段；business 用例仍需 `requirement_refs`：

```json
{
  "case_key": "order_by_id_ok",
  "level": "business",
  "transport": "dubbo",
  "endpoint": "dubbo:com.acme.OrderService#byId(java.lang.String)?group=order&version=1.0.0",
  "category": "business",
  "asserts": [
    {"kind": "rpc_status", "op": "eq", "expect": "OK"},
    {"kind": "result_field", "target": "orderId", "op": "eq", "expect": "O-1"}
  ],
  "dubbo": {
    "interface": "com.acme.OrderService",
    "arguments": ["O-1"]
  }
}
```

`dubbo.interface/method/parameter_types/group/version` 以画像为准，草案省略时
由框架回填；`arguments` 数量必须与参数类型数量一致。`transport=dubbo` 不接受
`path_params/query/headers/body`。

Dubbo 多步骤用例用 `steps`，每步填 `name` / `endpoint` / `dubbo` / `asserts`，
后续步骤可以用 `{"$ref": "<step>.<field>"}` 引用更早步骤的返回值：

```json
{
  "case_key": "order_query_then_echo",
  "level": "business",
  "transport": "dubbo",
  "endpoint": "dubbo:com.acme.OrderService#echo(java.lang.String)?group=order&version=1.0.0",
  "category": "business",
  "steps": [
    {
      "name": "first",
      "endpoint": "dubbo:com.acme.OrderService#byId(java.lang.String)?group=order&version=1.0.0",
      "dubbo": {"arguments": ["O-1"]},
      "asserts": [
        {"kind": "rpc_status", "op": "eq", "expect": "OK"},
        {"kind": "result_field", "target": "orderId", "op": "exists"}
      ]
    },
    {
      "name": "second",
      "endpoint": "dubbo:com.acme.OrderService#echo(java.lang.String)?group=order&version=1.0.0",
      "dubbo": {"arguments": [{"$ref": "first.orderId"}]},
      "asserts": [
        {"kind": "rpc_status", "op": "eq", "expect": "OK"},
        {"kind": "result_present", "op": "exists"}
      ]
    }
  ]
}
```

顶层 `asserts`、`body`、`path_params`、`query`、`headers` 必须为空；
顶层 `endpoint` 必须出现在 steps 中。每个 Dubbo 步骤至少一条 `rpc_status`。

### 11A.5 断言

| kind | 支持 op | 说明 |
|---|---|---|
| `rpc_status` | `eq` / `ne` / `in` | `OK` / `TIMEOUT` / `SERIALIZATION` / `NETWORK` / `FORBIDDEN` / `NO_PROVIDER` / `RPC_EXCEPTION` / `BAD_REQUEST` |
| `result_present` | `exists` / `absent` | 返回值是否为 null |
| `result_field` | `exists` / `eq` / `ne` / `in` / `gte` / `lte` / `contains` | target 用点号路径取返回值字段 |
| `error_code` | `eq` / `ne` / `in` / `contains` | 如 `rpc_timeout`、`no_provider` |
| `exception_type` | `eq` / `ne` / `contains` | 异常类名 |
| `error_message` | `eq` / `contains` | 异常信息 |

每个 Dubbo 用例至少要有 1 条 `rpc_status`。HTTP 断言类型（`status_code` /
`body_field` / `header` / `body_schema`）与 Dubbo 断言类型不能混用。

### 11A.6 边界

- 只支持直连地址；注册中心（Nacos/ZooKeeper）不作为目标发现或兜底。
- Hessian2 与 Triple unary 已支持：`dubbo://` 走 Hessian2 泛化调用；
  `tri://` 有 Protobuf descriptor 时走 gRPC-compatible adapter，无 proto
  的 Java 接口走 sidecar Triple-native。Kryo/FST 显式失败。
- 多服务/多端口项目用 `--dubbo-targets` 逐服务指定地址；全局 URL 只在
  所有服务共用一个 endpoint 时可用。
- sidecar 固定同步调用；异步/oneway 不生成用例。
- Dubbo 多步骤已支持；A5 可通过 `steps[].dubbo.arguments` / `steps[].asserts`
  修多步骤用例，单步用例用顶层 `dubbo.arguments`。
- 模板只生成正向冒烟：DTO 参数按字段采样（必填、嵌套 DTO、枚举、约束、
  集合元素、时间格式）；`@Pattern` 会尝试生成匹配值，lookaround/反向引用等
  复杂结构失败时提示人工确认；自定义校验注解按名称与参数推断常用约束。
  未知第三方类型按空对象发送并在 `note` 标明；没有字段级断言时只校验
  `rpc_status` 与返回值存在。
- A4 已收 Dubbo 画像与 `parameter_schemas`，可直接生成 `transport=dubbo`
  草案；草案只填 `dubbo.arguments`，接口/方法/参数类型由框架回填并校验。

---

## 11B. 用例业务实体与下游 Mock 联动

需要“一个手机商品”时，不要从环境里找真实商品 ID。正确做法是：用例自己声明
实体，请求和下游 Mock 都引用同一份数据。

```json
{
  "data": {
    "product": {"id": "P-1001", "category": "phone", "name": "Mock 手机"}
  },
  "query": {"product_id": {"$data": "product.id"}},
  "mock_bindings": {"http:svc:catalog GET /products/{}": "success"},
  "mock_replies": {
    "http:svc:catalog GET /products/{}": {
      "id": {"$data": "product.id"},
      "category": {"$data": "product.category"}
    }
  }
}
```

执行时：

1. 请求带 `P-1001`；
2. SUT 查询商品的下游调用命中 Mock；
3. Mock 在 `success` 分支原响应体上合并 `id/category`，让 `P-1001` 在本次会话里
   是一台手机；
4. 断言 SUT 的业务结果（比如推荐结果只含手机），而不是“查得到这条数据”。

规则：

- `$data` 只支持 `{"$data": "实体.字段"}` 对象形式；可用在 `path_params`、
  `query`、`body`、Dubbo `arguments/attachments`、断言 `expect/where` 和
  `mock_replies`；`headers` 首版仍是普通字符串。
- `mock_replies` 必须与该 dep 的 default `mock_bindings` 同时出现；只支持
  HTTP / Dubbo 下游，gRPC 下游会报 `mock_reply_transport_unsupported`。
- 引用不存在的字段报 `data.ref_unknown`；实体里再写 `$data` 报
  `data.nested_ref`；声明了实体但没人引用只给 `data.unused` 告警。
- 生成的 pytest 里 `$data` 已换成字面量，便于阅读和排错。
- `data` 和 `mock_replies` 进入用例指纹，改实体等于改用例，需要重新审核。
- A4 会按业务实体生成 `data` 和 `mock_replies`：提示词拿到下游分支的实际响应体，
  模型漏写 `mock_bindings` 时框架自动补 `success` 并告警；补充了分支响应体里
  不存在的字段会给 `mock.reply_field_unknown` 告警，方便审核时重点看。

### 11B.1 前置数据：只插表，不调接口

用例需要的数据库数据由 C5b 造数计划插入，不在用例里“先调一个创建接口造数”：

- 主表、关联表（父行/子行）一起插；有外键时用 `{"$ref": "orders[0].id"}`
  引用同批父行，单父键可自动补外键，多父未显式引用会阻断；
- 插入的主键/业务键就是用例请求里用的值，不另插一组 ID 再让用例碰运气；
- 不依赖环境里已有的真实记录，测试开始前插入、结束后按主键清理；
- `precondition.kind=api_state`（调业务接口造前置）会直接报
  `precondition.api_state_unsupported`，不执行。
- `preconditions[].where/set` 可以用 `{"$data": "实体.字段"}`，造数前会解析成
  实体值。precondition 同时写了 `table` 和 `where` 时，造数计划会检查插入行里
  是否有完全匹配的一行；没有就拒绝计划，报 `precondition_row_missing`。
  这样请求、前置条件和插入行用的是同一个 ID，对不上会在造数阶段就暴露。
- **不要求请求里的每个字段都出现在 fixtures**。业务输入、负例非法值、SUT 自己
  生成的字段、下游 Mock 的实体 ID、认证/租户头，都可以不出现在数据库里；
  只有你显式写进 `preconditions` 的 `table + where` 会被硬校验。

下游服务的数据不插库，也不调下游接口：由 `mock_replies` 让下游 Mock 返回
用例声明的实体。这样一次用例的数据只有两处来源——本服务数据库的插入行，
以及下游 Mock 的响应，都是用例自己定义的。

---

## 11C. MQ 消息断言（Kafka / RabbitMQ / RocketMQ）

异步消息不连 broker，也不启动消费者：Java agent 在生产者侧截获消息，命中
计划内 topic 时不真正发送，只把内容交给 AstSight 做断言。

```json
"mq_assertions": [
  {
    "topic": "orders-topic",
    "client": "kafka",
    "key": "O-1001",
    "headers": {"trace": "t1"},
    "body": {"order_id": "O-1001", "status": "paid"},
    "count": 1
  }
]
```

- `topic` 必填；`client` 为 `kafka`（默认）、`rabbitmq` 或 `rocketmq`；
  `key`、`headers`、`body` 都是子集匹配，`count` 为空表示至少一条。
- 需要 SUT 通过 Java agent 启动；计划内 topic 必须能从
  `/_admin/agent/routes` 的 `mq_deps` 读到。
- 消息不会真正发到 broker；报告里 `transport=mq`、`mode=producer_intercept`。
- 只验证“SUT 准备发什么”，不验证 broker 接收、分区路由和序列化字节；
  Agent 返回的 `RecordMetadata` 为 null，依赖元数据的回调需要自行容错。
- 不校验多条消息的先后顺序，也不覆盖 SUT 的多线程并发发送行为：消息在调用
  线程里被同步截断，只存在调用先后，没有投递顺序。
- 计划含 MQ topic 但 Agent 心跳没有 `mq.installed`，或缺少对应 client
  （`kafka` / `rabbitmq`）的拦截模块时，门禁阻断为
  `mq_producer_not_installed`，不会静默当作已拦截。
- Kafka：拦截 `KafkaProducer.send`；`topic` 就是 Kafka topic。
- RabbitMQ：拦截 `ChannelN.basicPublish`；`topic` 用 `exchange/routingKey`
  表示，例如 `orders-exchange/order.created`，`key` 为 routingKey。
- 版本矩阵：Kafka 2.8.2/3.7.0、RabbitMQ 4.9.0/5.21.0、RocketMQ 4.9.7/5.3.1
  已用真实客户端跑过拦截 e2e；Python 当前验证 kafka-python 3.0 / pika 1.3。
- RocketMQ：拦截 `DefaultMQProducer` 的同步/异步 send；`topic` 就是 RocketMQ
  topic，`key` 取 `Message.getKeys()`，headers 取 `getProperties()`。同步发送
  会返回合成的 `SendResult`（构造失败时为 null）；异步回调会尽力调用
  `onSuccess`。
- RabbitMQ 和 RocketMQ 的 publish 可以拦掉，但 SUT 启动时仍可能先建立连接、
  声明 exchange/queue 或连接 NameServer；没有中间件时可能在连接/启动阶段就失败。
  要完全脱离中间件，还需要连接级 Mock，当前未做。

### 11C.1 Python SUT

`agent run` 的 SUT 命令是 Python（`python` / `uvicorn` / `gunicorn` 等）且 Mock
计划含 MQ topic 时，会自动把 `engine/astsight/mq/inject` 放进 `PYTHONPATH`，并设置：

```text
ASTSIGHT_MQ_CAPTURE=1
ASTSIGHT_MQ_ADMIN_URL=<Mock admin 地址>
ASTSIGHT_MQ_TOKEN=<token>
ASTSIGHT_MQ_SUT_ID=<sut_id>
```

A4 的输入会带画像扫描出的 `mq_producers[]`：Java 的 ProducerRecord/KafkaTemplate、
RabbitMQ basicPublish/RabbitTemplate、RocketMQ Message/syncSend，以及 Python 的
kafka-python send、pika basic_publish。topic 会依次解析字面量、同文件
`static final`、Spring `@Value`/`@ConfigurationProperties`，以及
`application*.properties|yml` 与项目根 `.env`；`${key:default}` 的默认值可用。
解析不到或同键多值冲突时写进 `mq_warnings`，不猜动态值。
A4 只从这些候选生成 `mq_assertions`，找不到候选就写未覆盖，不编 topic。

`sitecustomize.py` 在解释器启动时挂 import hook，只 patch 两个纯 Python 客户端：

- `kafka-python`：`KafkaProducer.send`，命中后返回已完成的 Future；
- `pika`：`Channel.basic_publish` 与 `BlockingChannel.basic_publish`，同步/阻塞
  连接都覆盖；命中后返回 `True`（阻塞）或 `None`（Channel）。

手工启动 SUT 时，照上面的环境变量自己设置即可。Python 注入启动时会向 Mock
服务上报一次心跳（`mq.installed=true` 与已安装 client），门禁据此确认拦截已生效；
没有心跳时按 `mq_producer_not_installed` 阻断。当前不支持 confluent-kafka、
aio-pika、kombu/Celery；Go/Node/.NET 也还没有生产者拦截。

---

## 12. 运行模式

同一个引擎，差异只在范围。`run` 默认 `--mode delta`。

### 12.1 full：建基线

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/service-repo \
  --mode full \
  --base-url http://127.0.0.1:8010 \
  --openapi /path/to/service-repo/openapi.json
```

扫描全量路由，构建画像、模板用例和契约断言。`full` 必须建契约基线；
只生成用例不建基线会留下负债。需要规则驱动、可审核的用例库时，走 C1 → A4 → 审核 →
`run --mode cases` 这条链路。

### 12.2 delta：只重判受影响范围

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/service-repo \
  --mode delta \
  --base main --target HEAD \
  --base-url http://127.0.0.1:8010 \
  --reset-url http://127.0.0.1:8010/_reset \
  --codegraph on \
  --openapi /path/to/service-repo/openapi.json
```

- `--project` 必须是 git 仓库根；
- `--target` 必须是当前检出的工作树，不能是任意 git 引用；
- 只生成、不执行时加 `--skip-execute`；
- 重点看 `changed_apis.json` 的 `evidence`、`unresolved_symbols` 和 `unsupported_files`；
- delta 会用临时 `git worktree` 检出 merge-base，重扫 base 路由并与当前对比；
  被删除的接口写入 `deleted_apis.json`，报告单列“被删除的接口”并给
  `deleted_api` 告警。用例库中指向该接口的已批准 HTTP 用例会自动标为
  `deprecated`（逻辑删除），不再进入运行；报告列出被废弃的用例。
- 契约字段变化写入 `contract_changes.json`，报告单列“契约字段变化”：
  - HTTP OpenAPI：参数新增/删除/必填/类型、请求体字段与必填、响应状态码与字段，
    变化端点会并入 `changed_apis`；
  - gRPC：method 新增/删除、request/response 类型、streaming、message 字段
    编号/名称/类型/必填/枚举值；
  - Dubbo：method 签名、group/version、参数与返回 DTO 字段。
- gRPC/Dubbo 变化目前只报告和告警，不自动生成对应入口用例；接口在依赖 jar、
  动态代理仍按既有边界标 unsupported。

### 12.3 cases：只执行已批准用例

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/service-repo \
  --mode cases \
  --base-url http://127.0.0.1:8010 \
  --reset-url http://127.0.0.1:8010/_reset \
  --ruleset ruleset.json \
  --data-plan .astsight/data/data_plan.json
```

- 只读 `caseset.json` 中 `status=approved` 的用例；
- 没有已批准用例是 blocker，退出码非 0；
- 待审 proposal 默认只 warning + `degraded`，`--fail-on-pending-proposals` 可升级；
- `--allow-stale-cases` 允许 approved endpoint 在 OpenAPI 中缺失时降级为 warning。

### 12.4 常用参数

| 参数 | 说明 |
|---|---|
| `--base-url` | 被测 HTTP 服务地址；不提供则只生成不执行 |
| `--dubbo-url` | 全局 Dubbo 直连地址；只在所有服务共用一个 endpoint 时可用 |
| `--dubbo-target` | 运行直连地址 `IP:port`（可带 `dubbo://`/`tri://`/`grpc://`）；只补未解析服务 |
| `--dubbo-targets` | 服务级目标映射 JSON，键为 `interface?group=&version=` |
| `--dubbo-host` | 画像只解析出端口时拼 `dubbo://host:port`，默认 `127.0.0.1` |
| `--dubbo-source-commit` / `--dubbo-source-branch` | 校验 SUT git 来源与画像一致 |
| `--dubbo-source-sha256` + `--dubbo-artifact` | 校验 SUT 构建产物 sha256 |
| `--dubbo-target-override` | 允许 `--dubbo-targets` 覆盖代码里的显式地址 |
| `--reset-url` | 每条用例前的重置接口；Git Bash 下建议传完整 URL |
| `--openapi` | OpenAPI 文件或 URL；缺省自动发现 |
| `--out` | 产物根目录，相对 `project`，默认 `.astsight/runs` |
| `--skip-execute` | 不请求被测服务 |
| `--allow-unmapped` | 不把覆盖缺口当阻塞；缺口仍逐条列出 |
| `--retries` | 环境抖动重试次数，默认 1，上限 3 |
| `--timeout` | 单请求超时 |
| `--min-pass-rate` | 通过率门禁 |
| `--case-base` | 用例库路径，默认 `caseset.json` |
| `--ruleset` | 规则库路径，默认项目 `ruleset.json` |
| `--allow-stale-rules` | 规则 hash 与用例快照不一致时降级为 warning |
| `--data-plan` / `--db-url` | C5b 计划和造数数据库 |
| `--mock-plan` | 启动内嵌 Mock |
| `--mock-admin-url` / `--mock-token` | 接入外部 Mock |
| `--fail-on-pending-proposals` | 待审 proposal 升级为 blocker |
| `--min-requirement-coverage` | 需求覆盖率门禁，默认不设阈值 |
| `--json` | 输出机器可读报告摘要 |

### 12.5 退出码

| 退出码 | 含义 |
|---|---|
| `0` | pass |
| `1` | fail（门禁阻塞或真实缺陷） |
| `2` | not_run（没有执行，例如只生成或环境未就绪） |

---

## 13. 门禁与报告

### 13.1 阻塞项

- 真实缺陷（`code_defect`）；
- 生成代码语法错误；
- 未闭合的 `unmapped`（非 cases 模式）；
- cases 模式没有 approved 用例；
- `--fail-on-pending-proposals` 且存在待审 proposal；
- `--min-requirement-coverage` 未达标；
- approved endpoint 在 OpenAPI 中缺失，且未加 `--allow-stale-cases`；
- Mock 绑定零调用（`mock_not_used`）。

### 13.2 警告项

- 存在待审 proposal；
- OpenAPI / 需求文档漂移；
- 需求未覆盖；
- 存在 deprecated 用例；
- 用例未执行；
- 通过率低；
- contract drift 未校验。

### 13.3 单列、不算质量结论

- `recovered_by_retry`：重试恢复；
- 未执行用例；
- 契约缺失接口；
- 文档未覆盖需求。

**静默通过是最大风险。** 任何“没看到 / 没判到”的范围都必须出现在报告里。

---

## 14. CodeGraph 与变更感知

### 14.1 何时需要

- Java/Spring：需要 CodeGraph 识别路由、方法行区间和调用链；
- Kotlin/TypeScript：需要 CodeGraph 提供符号边界和文件内节点；
- Python：基础 AST 可用，但完整调用链和跨语言映射仍建议开启。

### 14.2 使用

```bash
PYTHONPATH=. python -m astsight codegraph install
PYTHONPATH=. python -m astsight codegraph status

PYTHONPATH=. python -m astsight run \
  --project /path/to/java-repo \
  --mode delta --base main --target HEAD \
  --codegraph on \
  --full-index \
  --openapi /path/to/openapi.json
```

- `--codegraph auto`：默认，可用则用；
- `--codegraph on`：强制使用；
- `--codegraph off`：关闭，非 Python 文件会进入 `unsupported_files`；
- `--full-index`：重建索引；
- `--codegraph-module-root`：覆盖 CodeGraph 安装目录。

### 14.3 证据等级

| 证据 | 含义 |
|---|---|
| `verified` | 方法级行映射直接命中 |
| `inferred` | 类级/文件级兜底或调用链推断 |
| `unmapped` | 没追到，必须人工确认 |

`changed_apis.json` 的 `confidence`、`evidence`、`attribution` 和 `trace` 要一起看。
`medium/inferred` 是推断，不是方法级直接命中。

### 14.4 已知边界

- `.sql` 等文件不产出符号，会进 `unsupported_files`；默认 fail-closed，
  `--allow-unmapped` 可降级，但不能假装已覆盖。
- 纯删除接口不会出现在当前工作树的路由清单里；目前只能做文件级兜底。
- Spring `@ModelAttribute` 等反射调用不在静态调用图里，会进 `unresolved(no_route_reached)`。
- 同名重载可能串边，trace 里会体现。

---

## 15. Agent 端到端（可选）

`agent run` 把规则、Mock 语义、造数、用例、审核、计划、执行和诊断串起来：

```text
需求文档 + 代码变更（OpenAPI 可选）
  → 路由/影响接口
  → A1 规则（BR + CR）
  → C3.5.1 Mock 语义
  → C5a 数据目录（提供 --ddl 时）
  → A4 用例设计
  → 待审 proposal（--approve-as 可批准 new/update/revive）
  → C5b 造数计划 → MockPlan → 启动 Mock
  → 注入环境变量启动 SUT → pytest 执行
  → 确定性归因 → A5/A6 诊断+修复（同一节点）→ 有界重跑（默认最多 3 轮）
```

基本命令：

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

边界：

- `--approve-as` 只自动批准初始 proposal 的 `new/update/revive`；`deprecate` 永远拒绝；
- 不提供 `--approve-as` 时停在待审 proposal；
- A5/A6 默认最多 3 轮（`--max-repair-rounds`，上限 5，0 关闭）。运行期修复可改用例
  请求/断言/`mock_bindings` 和实际绑定的 Mock 分支；用例补丁以
  `astsight-agent:repair` 身份自动批准，Mock 补丁热更新。修复不得删除断言或降强，
  不改用例身份、规则引用或前置状态；
- 没有 OpenAPI 时可以用代码路由，但字段格式/必填类用例无法生成；
- 分析阶段 SUT 尚未启动，因此不能从 `--base-url` 拉取契约；
- 启动期注入支持环境变量形式的 `url_override`、Spring Cloud 静态实例的
  `discovery_override` 和显式 `http_proxy`；Java SUT 的 `agent_rewrite` 会通过
  `JAVA_TOOL_OPTIONS` 自动加载 agent，支持 OkHttp、Feign（阻塞）、RestTemplate
  和直连 Apache HttpClient 4.3+/5.x classic，HTTPS 目标改写为明文 Mock 地址；
  内置使用未 shaded OkHttp / Apache HC 的 SDK 会顺带被改写，但计划期能否识别
  该依赖仍取决于画像能否解析出 URL；
- WebClient、自有传输或 shaded HTTP 客户端的第三方 SDK、JDK HttpClient /
  `HttpURLConnection` / async-http-client / HC 4.0–4.2 / HC5 async 不建设
  adapter，计划期标 `unsupported`，不静默透传；
- 下游自动识别覆盖 Python 的 `requests`/`httpx`/`urllib`，以及 Java（含 Feign
  接口）、Go/Node 的常见 HTTP 客户端；生成式 SDK、动态代理和注册中心发现的
  服务地址仍不识别，会明确落在 `downstream_unsupported`；
- Java `@Value("${pay.base-url}")` 等 Spring 配置键会规范化为环境变量形式
  （`PAY_BASE_URL`），以便注入；无法映射的配置键只告警，不猜原值。

---

## 16. 产物与报告

### 16.1 项目级文件

```text
<project>/
├── ruleset.json                 # 规则库
├── ruleset.md                   # 规则库只读视图
├── caseset.json                 # 已批准用例库
└── .astsight/
    ├── rules/<context_id>/      # C1 上下文与草案
    ├── casegen/<context_id>/    # A4 上下文与草案
    ├── cases/proposals/         # 待审 proposal
    ├── data/                    # C5a/C5b
    ├── mock-plan.json           # MockPlan 草案
    └── runs/<run_id>/           # 单次运行产物
```

`caseset.json` 建议纳入项目版本管理，用例变更可以通过 diff 审阅；`ruleset.json`
是生成物，不手工编辑；`ruleset.md` 用于评审。

### 16.2 运行目录

```text
.astsight/runs/<run_id>/
├── run_meta.json
├── routes.json
├── changes.json
├── changed_apis.json
├── profiles.json
├── ruleset.json
├── mock_semantics.json
├── data_catalog.json
├── data_plan.json
├── caseset.json
├── requirement_coverage.json
├── pending_proposals.json
├── cases.json
├── codegen.json
├── generated/
├── results.xml
├── execution.json
├── report.json
└── report.md
```

使用 Mock 时还会生成：

- `run_manifest.json`：实际 endpoint/port、route、injection、token 指纹（不含 token）；
- `mock_injection.json`：供 SUT 启动器消费的注入清单（不含 token）；
- `mock_calls.jsonl`：调用证据，参与 `mock_not_used` 校验。

`report.md` 是给人看的结论；`report.json` 用于机器消费；`execution.json` 保留执行明细。

报告里的 `audit` 块显式列出审计完整性：`weak_oracle` 标出只有状态码断言且
Mock 未命中的用例，`mock_sessions` 记录逐用例会话证据（详见
`report.mock.session_evidence`），agent run 还会给出 `token_usage`。
未实现或不适用的审计项（`egress_guard`、`instance`、`transport_coverage`、
`transport_fallback`）直接标 `not_implemented` / `not_applicable`，不会静默省略。

---

## 16.1 本地诊断包与反馈

`run` 和 `agent run` 在门禁失败时会自动生成诊断包（默认 `--feedback auto`），
与 `report.json` / `report.md` 一起写进 run 目录；成功运行不生成。
`--feedback always` 总是生成，`--feedback off` 关闭。
也可以随时手动从任意 run 目录生成脱敏诊断包：

```bash
# 预览将包含哪些文件，不写盘
PYTHONPATH=. python -m astsight feedback collect --run .astsight/runs/<run_id> --dry-run

# 生成 feedback.zip（默认输出到 run 目录）
PYTHONPATH=. python -m astsight feedback collect --run .astsight/runs/<run_id>

# 生成 issue 草稿与 GitHub 提交链接
PYTHONPATH=. python -m astsight feedback open \
  --run .astsight/runs/<run_id> --repo JeffreyShouzhong/AstSight
```

- 诊断包包含脱敏后的 `report.json`、`execution.json`、`mock_evidence.json`、
  失败用例快照与代码（可用 `--no-case-code` 关闭），以及人读的 `summary.md`。
- 默认脱敏：敏感键名、认证头、query 值、邮箱/手机号/身份证、绝对路径不会原样包含；
  提交前仍建议自己检查一遍。
- **不自动上传**，不依赖服务端。生成的 `feedback.zip` 由你自行附加到 GitHub Issue。
- `feedback open` 会写 `issue.md` 并打印提交链接；可用 `ASTSIGHT_REPO` 或 `--repo`
  指定仓库。
- 只收集运行产物，不收集被测系统源码。

## 17. 示例项目

| 目录 | 用途 |
|---|---|
| `engine/examples/partner_onboarding/` | 合伙人注册，需求/设计/OpenAPI/DDL 齐全，适合走 C1 → A4 → 执行 |
| `engine/examples/order_service/` | 18 接口 FastAPI 示例，含 `case_samples/` 和需求文档 |
| `engine/examples/downstream_service/` | 报价服务 → 价格服务，演示 HTTP Mock |
| `engine/examples/demo_delta.py` | 一条命令演示 delta 闭环 |
| `engine/examples/demo_case_review.py` | 演示需求 → 审核用例 → 执行 |
| `engine/examples/demo_agent_flow.py` | 演示规则 → Mock → 造数 → 用例 → 执行 |
| `java/samples/dubbo-provider/` | 常驻 Spring Boot Dubbo provider，含 DTO/约束/枚举，演示 sidecar 与 Dubbo 入口用例 |

Dubbo provider 样例：

```bash
cd java/samples/dubbo-provider
mvn -q -DskipTests package
java -jar target/dubbo-provider-sample-0.1.0-SNAPSHOT.jar
# READY 20880

cd ../../../engine
PYTHONPATH=. python -m astsight run \
  --project ../java/samples/dubbo-provider \
  --mode full --codegraph off \
  --dubbo-url dubbo://127.0.0.1:20880
```

合伙人示例的完整步骤见
[engine/examples/partner_onboarding/README.md](../engine/examples/partner_onboarding/README.md)。

启动示例服务：

```bash
cd engine/examples/partner_onboarding
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8030
```

- OpenAPI：`http://127.0.0.1:8030/openapi.json`
- 健康检查：`GET /health`
- 重置数据：`POST /_reset`
- 调试状态：`GET /_debug/state`

---

## 18. 常见问题

### 18.1 模型配置缺失或 401

检查 `ASTSIGHT_LLM_API_KEY` / `ANTHROPIC_AUTH_TOKEN`、`ASTSIGHT_LLM_BASE_URL`、
`ASTSIGHT_LLM_MODEL` 是否在当前 shell 生效。不要把密钥写进命令或提交到仓库。
如果报 `llm_response_truncated`，提高 `ASTSIGHT_LLM_MAX_TOKENS`。

### 18.2 出现 `openapi_missing`

没有传入 `--openapi`，项目内也没有自动发现到 OpenAPI。Spring MVC、gRPC descriptor
与 Dubbo 接口仍会被扫描。若最终 CR 仍为空，查看 `context.json.contract_model.sources`
和 `contract_model.gaps`；只有所有契约源都缺失或均无法静态证明字段时才会出现该情况。

### 18.3 规则校验报 `quote` 不匹配

通常是草案的 `quote` 不是原文精确子串，或规则文本中的数字/错误码没有出现在引用里：

1. 检查 `context.json` 中对应 section 的原文；
2. 修正草案的 `quote` 或规则文本；
3. 重新 `rules import`。

不要为了过校验去改 `ruleset.json`。

### 18.4 `context_id` 不一致

`prepare` 和 `import` 之间文档、OpenAPI 或提示词发生了变化。重新执行 `rules prepare`，
使用新的上下文目录。

### 18.5 未发现任何路由

- 确认 `--project` 指向项目根；
- Python 项目确认路由写法在扫描器支持范围内；
- Java/Spring 确认 CodeGraph 已安装并 `--codegraph on`；
- 提供 `--openapi`；
- 不要用 `--allow-unmapped` 掩盖“完全没找到路由”。

### 18.6 CodeGraph 不可用

先运行 `codegraph install` 和 `codegraph status`。仍不可用时，非 Python 项目会进入
`unsupported_files`，不会用正则猜符号边界。

### 18.7 待审 proposal 导致 `degraded`

这是预期行为：默认只用上一版已批准用例。完成人工审核并 `cases approve` 后重跑；
或在 CI 中加 `--fail-on-pending-proposals` 强制阻塞。

### 18.8 用例被标 `blocked`

通常是：

- 用例有 `preconditions`，但没有 `data_plan` 或 `--db-url`；
- 数据库方言不受支持，或 plan 方言与 `--db-url` 不一致；
- Mock 依赖不可用或下游标 `unsupported`。

查看 `report.json` 的阻塞原因，不要通过删断言或改用例身份绕过。

### 18.9 `mock_not_used`

Mock 绑定声明了 `dep_id`，但整个会话没有调用。检查 SUT 是否真的走了该下游、
注入是否生效、`dep_id` 是否与用例绑定一致。零调用是阻塞项，不是 warning。

### 18.10 HTTPS 下游 Mock 被阻断

v1 只支持明文 HTTP；HTTPS 下游标 `tls_not_supported`，用例 `blocked`。
不做 MITM、不静默降级。

### 18.11 Windows Git Bash 改写 `--reset-url`

Git Bash 会把以 `/` 开头的参数改写成 Windows 路径。建议传完整 URL，例如
`--reset-url http://127.0.0.1:8010/_reset`，或设置 `MSYS_NO_PATHCONV=1`。

### 18.12 delta 报“project 不是仓库根”

`--project` 必须是 git 仓库根。子目录会让 `git diff` 的相对路径错位，框架直接拒绝，
不静默处理。

### 18.13 生成代码 collect 失败

先看 `codegen.json` 和 `generated/`。框架会在执行前跑 `pytest --collect-only`；
语法错误是 blocker。不要手工改生成文件后继续用同一 run，修输入后重跑。

### 18.14 `ruleset.json` / `caseset.json` 能手工改吗

不能。`ruleset.json` 只能由 `rules c1` / `rules import` 写入；`caseset.json` 只能由
`cases approve` 写入。要改规则，改需求/设计原文后重抽；要改用例，重新生成草案并审核。

---

## 19. 边界与未实现

- `db_row` 单条断言支持 `wait_ms` 轮询；多行/多表共享截止时间的异步一致性明确不做。
- 不生成跨用例捕获或鉴权链路；多请求仅限单条用例内的 `steps`。
- C5 造数支持 SQLite/PostgreSQL/MySQL，但只覆盖用例级 fixture apply/teardown；
  不负责建库、迁移、连接池与跨库事务。
- `replay` 模式明确不实现。
- 历史缺陷案例库的自动入库与服务端汇聚未实现；当前使用 `feedback collect` 生成本地
  诊断包，再由使用者自行提交 GitHub Issue。
- HTTP / Dubbo / gRPC 下游 Mock 已统一到同一控制面；streaming、reflection、
  Triple streaming 明确不支持。
- 多 SUT 并发隔离（设计保留）明确不排期。
- LangGraph 编排未接线：`orchestration/` 只有 StateGraph 骨架；当前流程是线性阶段 +
  仅 `case_error` / `mock_error` 最多 3 轮的修复回环，其他根因直接结束并产出报告。
- 用例入库/更新必须人工审核。唯一的自动废弃是接口删除时把对应用例标为
  `deprecated`，以 `astsight:deleted-api` 身份写入 `review_log`；`deprecate`
  提案仍不能自动批准。
- 非 Python 符号映射依赖 CodeGraph；关闭后不会用正则兜底。
- 被删除的接口不会列为 `changed_apis`；delta 会从 base 分支重扫路由，
  写入 `deleted_apis.json` 并自动逻辑删除对应用例。
- 分析阶段 SUT 尚未启动，不能从 `--base-url` 拉取契约。

---

## 20. 命令速查

```bash
cd engine

# 环境
pip install -r requirements.txt
PYTHONPATH=. python -m astsight --help
PYTHONPATH=. python -m astsight codegraph install
PYTHONPATH=. python -m astsight codegraph status

# C1：需求 + 设计 + OpenAPI
PYTHONPATH=. python -m astsight rules c1 \
  --project $PROJECT \
  --requirements $PROJECT/docs/requirements.md \
  --design $PROJECT/docs/design.md \
  --openapi $PROJECT/openapi.json

# C1：只有需求
PYTHONPATH=. python -m astsight rules c1 \
  --project $PROJECT \
  --requirements $PROJECT/docs/requirements.md

# C1：两段式
PYTHONPATH=. python -m astsight rules prepare \
  --project $PROJECT \
  --requirements $PROJECT/docs/requirements.md \
  --design $PROJECT/docs/design.md \
  --openapi $PROJECT/openapi.json
PYTHONPATH=. python -m astsight rules import \
  --project $PROJECT \
  --context $PROJECT/.astsight/rules/<context_id> \
  --draft $PROJECT/.astsight/rules/<context_id>/rules.draft.json

# 规则状态与渲染
PYTHONPATH=. python -m astsight rules status --project $PROJECT
PYTHONPATH=. python -m astsight rules render --project $PROJECT --stdout

# A4：生成上下文
PYTHONPATH=. python -m astsight cases prepare \
  --project $PROJECT \
  --requirements $PROJECT/docs/requirements.md \
  --openapi $PROJECT/openapi.json

# 导入草案并生成待审 proposal
PYTHONPATH=. python -m astsight cases import \
  --project $PROJECT \
  --context $PROJECT/.astsight/casegen/<context_id> \
  --draft $PROJECT/cases.draft.json \
  --ruleset ruleset.json \
  --require-rules

# 人工审核后批准
PYTHONPATH=. python -m astsight cases approve \
  --project $PROJECT \
  --proposal $PROJECT/.astsight/cases/proposals/<proposal_id> \
  --reviewer <your-name>

# 用例状态
PYTHONPATH=. python -m astsight cases status --project $PROJECT

# C5a / C5b
PYTHONPATH=. python -m astsight data catalog \
  --project $PROJECT --ddl $PROJECT/schema.sql
PYTHONPATH=. python -m astsight data plan \
  --project $PROJECT \
  --catalog .astsight/data/data_catalog.json \
  --cases caseset.json
PYTHONPATH=. python -m astsight data apply \
  --db-url sqlite:////absolute/path/to/service.db \
  --plan .astsight/data/data_plan.json

# MockPlan
PYTHONPATH=. python -m astsight mock plan \
  --project $PROJECT --out .astsight/mock-plan.json

# 三种运行模式
PYTHONPATH=. python -m astsight run --project $PROJECT --mode full \
  --base-url http://127.0.0.1:8010 --openapi $PROJECT/openapi.json
PYTHONPATH=. python -m astsight run --project $PROJECT --mode delta \
  --base main --target HEAD --base-url http://127.0.0.1:8010 --codegraph on
PYTHONPATH=. python -m astsight run --project $PROJECT --mode cases \
  --base-url http://127.0.0.1:8010 --data-plan .astsight/data/data_plan.json

# Agent 端到端
PYTHONPATH=. python -m astsight agent run \
  --project $PROJECT \
  --requirements $PROJECT/docs/requirements.md \
  --openapi $PROJECT/openapi.json \
  --mode full --approve-as <reviewer>

# 反馈诊断包
PYTHONPATH=. python -m astsight feedback collect --run .astsight/runs/<run_id> --dry-run
PYTHONPATH=. python -m astsight feedback collect --run .astsight/runs/<run_id>
PYTHONPATH=. python -m astsight feedback open --run .astsight/runs/<run_id> \
  --repo JeffreyShouzhong/AstSight

# 示例
python examples/demo_delta.py
python examples/demo_case_review.py
python examples/demo_agent_flow.py --keep

# 测试
python -m pytest -q
```
