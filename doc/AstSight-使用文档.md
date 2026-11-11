# AstSight 使用文档

| 项 | 内容 |
|---|---|
| 适用版本 | M1 确定性闭环 + 规则驱动用例 + SQLite 造数 |
| 命令入口 | `python -m astsight`（在 `engine/` 下运行） |
| 主要输入 | 代码库、需求/设计文档、OpenAPI（可选）、DDL/seed（可选） |
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
| C1 / A1 | 需求 + 设计（可选）+ OpenAPI（可选）→ 业务规则 `BR` + 契约规则 `CR` | `rules c1` / `rules prepare` + `rules import` |
| C2 | Git 分支差异 → 变更文件、符号、受影响 API | `run --mode delta` |
| C3 | 路由清单、接口画像、下游调用点 | `run`、`cases prepare`、`mock plan` |
| C3.5.1 | 契约 `success` + 规则分支 → Mock 语义 | `mock semantics`；`agent run` 内部也会生成 |
| C3.5.5 | 明文 HTTP 下游 Mock 服务与调用证据 | `mock plan`、`run --mock-plan` |
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
- 被执行的 SUT（被测服务）需要可访问的 HTTP 地址；没有 `--base-url` 时只生成、不执行。

---

## 4. 配置

### 4.1 模型配置

`rules c1`、`agent run` 和显式调用模型适配层时需要模型。密钥只从环境变量读取，
不要写进命令、脚本、报告或仓库。

| 用途 | 优先变量 | 兼容变量 |
|---|---|---|
| API Key | `ASTSIGHT_LLM_API_KEY` | `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` |
| Base URL | `ASTSIGHT_LLM_BASE_URL` | `ANTHROPIC_BASE_URL` |
| 模型 | `ASTSIGHT_LLM_MODEL` | `ANTHROPIC_MODEL` |
| Provider | `ASTSIGHT_LLM_PROVIDER` | - |
| 超时 | `ASTSIGHT_LLM_TIMEOUT` | - |
| 最大输出 | `ASTSIGHT_LLM_MAX_TOKENS` | - |
| 最大重试 | `ASTSIGHT_LLM_MAX_RETRIES` | - |

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

`ASTSIGHT_*` 优先于 `ANTHROPIC_*`。没有模型环境时，使用 C1 的两段式流程，
或只运行确定性命令（`run`、`cases import/approve`、`data catalog/plan/apply`）。

### 4.2 数据库

C5 造数 v1 只支持 SQLite。运行或手工 apply 时通过 URL 指定：

```bash
--db-url sqlite:////absolute/path/to/service.db
```

相对路径也可用，但建议使用绝对路径，避免工作目录变化后指错库。MySQL/PostgreSQL
会标 `unsupported_dialect`，不会静默降级。

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
- `--openapi`：可选；不传时自动发现项目内常见 OpenAPI 路径。仍找不到时只生成 `BR`，
  不生成 `CR`，并产生 `openapi_missing` warning。
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
| `--openapi` | 否 | 自动发现 | 契约文件；用于生成 `CR` |
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
- `contract_rules[]`：OpenAPI 契约规则 `CR`；
- `conflicts[]`、`gaps[]`、`questions[]`：冲突、缺口、待确认问题；
- `documents[]`：输入文档及哈希；
- `prompt`：提示词版本与模型信息。

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
      "assert_strength": "L1",
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

有 DDL 时，先从 DDL/FK/seed 生成数据目录：

```bash
PYTHONPATH=. python -m astsight data catalog \
  --project examples/partner_onboarding \
  --ddl examples/partner_onboarding/schema.sql \
  --out .astsight/data
```

可选参数：

- `--seed`：seed JSON，用于补充初始数据；
- `--dialect`：DDL 方言；
- `--context`：C1 规则上下文，用于字段语义对齐。

无模型时仍产出骨架，缺失语义标 `unmapped`。

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
MySQL/PostgreSQL 标 `unsupported_dialect`。

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
- 重点看 `changed_apis.json` 的 `evidence`、`unresolved_symbols` 和 `unsupported_files`。

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
| `--base-url` | 被测服务地址；不提供则只生成不执行 |
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
  → 确定性归因 → A5 诊断 → 可选 A6 修复建议 → 有界重跑
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
  --approve-as demo \
  --max-repair-rounds 1
```

边界：

- `--approve-as` 只自动批准 `new/update/revive`；`deprecate` 永远拒绝；
- 不提供 `--approve-as` 时停在待审 proposal；
- A6 默认关闭（`--max-repair-rounds 0`），只修请求参数和断言，不改用例身份、
  规则引用、前置状态或 Mock 绑定；
- 没有 OpenAPI 时可以用代码路由，但字段格式/必填类用例无法生成；
- 分析阶段 SUT 尚未启动，因此不能从 `--base-url` 拉取契约；
- 启动期注入只支持环境变量形式的 `url_override` 和显式 `http_proxy`；
- 下游自动识别目前只覆盖 Python 的 `requests`/`httpx`/`urllib` 调用点。

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

---

## 17. 示例项目

| 目录 | 用途 |
|---|---|
| `engine/examples/partner_onboarding/` | 合伙人注册，需求/设计/OpenAPI/DDL 齐全，适合走 C1 → A4 → 执行 |
| `engine/examples/order_service/` | 18 接口 FastAPI 示例，含 `case_samples/` 和需求文档 |
| `engine/examples/downstream_service/` | 报价服务 → 价格服务，演示 HTTP Mock |
| `engine/examples/demo_delta.py` | 一条命令演示 delta 闭环 |
| `engine/examples/demo_case_review.py` | 演示需求 → 审核用例 → 执行 |
| `engine/examples/demo_agent_flow.py` | 演示规则 → Mock → 造数 → 用例 → 执行 |

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

没有传入 `--openapi`，项目内也没有自动发现到 OpenAPI。业务规则和业务用例仍可生成，
但不会生成 CR 契约规则，字段格式/必填类用例也会缺失。需要契约时补 `--openapi`，
或把契约文件放到自动发现路径。

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
- 数据库方言不是 SQLite；
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

- 断言强度仍以 L1 为主：状态码、字段存在、字段值；L2–L4 尚未实现。
- 只支持单请求用例，不生成跨用例捕获或鉴权链路。
- C5 造数 v1 只支持 SQLite；MySQL/PostgreSQL 标 `unsupported_dialect`。
- `replay` 模式未实现。
- gRPC Mock 未落地；HTTP Mock 只支持明文 HTTP。
- 多 SUT 隔离、Dubbo 下游 Mock 的 Java agent 尚未实现。
- 审核必须人工完成；`deprecate` 永远不能自动批准。
- 非 Python 符号映射依赖 CodeGraph；关闭后不会用正则兜底。
- 被删除的接口不会自动出现在 `changed_apis`，需要额外比对 base 分支或 OpenAPI。
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

# 示例
python examples/demo_delta.py
python examples/demo_case_review.py
python examples/demo_agent_flow.py --keep

# 测试
python -m pytest -q
```
