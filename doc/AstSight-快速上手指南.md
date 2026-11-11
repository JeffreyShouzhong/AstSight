# AstSight 快速上手指南

> 适用对象：第一次使用 AstSight 的开发/测试工程师。
> 目标：30 分钟内跑通 demo、配好大模型，并在自己的项目上生成第一版可执行用例。
> 完整参数与原理见 [AstSight 使用文档](AstSight-使用文档.md)。

---

## 1. 它是做什么的

AstSight 把「需求文档 + 设计文档 + OpenAPI + 代码仓库」变成可执行、可审核、可版本化的 API 测试用例：

```text
需求/设计 → 规则库 ruleset.json → 用例草案 → 人工审核 → caseset.json
         → 造数计划 → pytest 脚本 → 执行报告
```

两个关键约定：

- **模型只出候选**：规则、用例都必须通过框架校验；模型输出不直接入库。
- **人工审核是硬门禁**：`cases approve` 必须由真实审核人执行，框架不会自动批准。

---

## 2. 安装

```bash
cd engine
pip install -r requirements.txt

# 源码目录运行时需要把 engine/ 加入 PYTHONPATH
PYTHONPATH=. python -m astsight --help
```

- Python 3.11 及以上（仓库最近在 3.13 上验证）。
- Linux 是目标运行环境；Windows 可用于本地调试（见第 7 节注意事项）。
- 非 Python 项目需要 CodeGraph（Node >= 22.5）：`python -m astsight codegraph install`。

---

## 3. 先跑 demo（不需要模型）

```bash
cd engine

# 代码变更 → 接口画像 → 用例 → pytest → 执行报告
python examples/demo_delta.py

# 需求 → 用例 → 人工审核 → 版本化 → 执行
python examples/demo_case_review.py

# 保留临时工作区，方便查看 .astsight 产物
python examples/demo_case_review.py --keep
```

两个 demo 都会自动启动示例服务；跑完后看临时目录里的 `.astsight/runs/<run_id>/report.md`。

第三个 demo 需要模型配置，配好第 4 节后再跑：

```bash
python examples/demo_agent_flow.py --keep
```

---

## 4. 配置大模型

### 4.1 复制模板

```bash
cd engine
cp llm.example.json llm.json
```

`llm.json` 已加入 `.gitignore`，不会被提交。**推荐把 key 放在环境变量里**，配置文件只写变量名：

```bash
export ASTSIGHT_LLM_API_KEY=你的key
```

### 4.2 三种常见配置

**Anthropic 官方**

```json
{
  "schema_version": 1,
  "provider": "anthropic",
  "model": "claude-sonnet-5-5",
  "base_url": "https://api.anthropic.com",
  "auth_style": "x-api-key",
  "api_key_env": "ANTHROPIC_API_KEY",
  "max_tokens": 16384
}
```

**DeepSeek（Anthropic 兼容端点）**

```json
{
  "schema_version": 1,
  "provider": "anthropic",
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com/anthropic",
  "auth_style": "bearer",
  "api_key_env": "DEEPSEEK_API_KEY",
  "max_tokens": 16384
}
```

**其他 OpenAI 兼容网关**

```json
{
  "schema_version": 1,
  "provider": "openai",
  "model": "your-model-id",
  "base_url": "https://your-gateway.example/v1",
  "api_key_env": "OPENAI_API_KEY"
}
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | 否 | 当前固定为 `1` |
| `provider` | 是 | `anthropic` 或 `openai` |
| `model` | 是 | 服务端 model id |
| `base_url` | 否 | 缺省用官方地址；自建网关必须填写 |
| `auth_style` | 否 | 仅 Anthropic 协议：`x-api-key`（SDK）或 `bearer`（兼容网关） |
| `api_key` / `api_key_env` | 二选一 | 直接写 key，或写保存 key 的环境变量名（推荐后者） |
| `timeout` / `max_tokens` / `temperature` / `max_retries` | 否 | 调用参数；推理型模型建议 `max_tokens ≥ 16384` |

### 4.3 不写配置文件，只用量环境变量

```bash
export ASTSIGHT_LLM_PROVIDER=openai
export ASTSIGHT_LLM_MODEL=your-model-id
export ASTSIGHT_LLM_BASE_URL=https://your-gateway.example/v1
export ASTSIGHT_LLM_API_KEY=你的key
```

也兼容 Claude Code / Anthropic 兼容端点的通用变量：`ANTHROPIC_AUTH_TOKEN`、
`ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`、`ANTHROPIC_MODEL`。

### 4.4 优先级与查找位置

```text
ASTSIGHT_LLM_* 环境变量  >  llm.json  >  ANTHROPIC_* 环境变量
```

`llm.json` 查找顺序：

1. `--llm-config <路径>`（所有调用模型的命令都支持）
2. `ASTSIGHT_LLM_CONFIG` 环境变量
3. 当前目录 `./llm.json`（在 `engine/` 下运行命令时即 `engine/llm.json`）
4. `~/.astsight/llm.json`（想全局复用就放这里）

显式指定的路径不存在会直接报错，不会静默回退。

### 4.5 验证配置

```bash
cd engine
PYTHONPATH=. python -m astsight rules extract \
  --project examples/partner_onboarding \
  --requirements examples/partner_onboarding/docs/requirements.md \
  --design examples/partner_onboarding/docs/design.md \
  --openapi examples/partner_onboarding/openapi.json
```

能读文档、调用模型并写出 `ruleset.json` 就说明配置可用。

---

## 5. 在自己的项目上使用

### 5.1 准备材料

```text
your-project/
├── docs/requirements.md      # 需求文档（必填）
├── docs/design.md            # 设计文档（建议）
├── openapi.json              # 接口契约（建议；缺省会自动发现）
└── schema.sql                # 建表语句（需要造数时）
```

### 5.2 路线 A：一条命令跑完整链路（需要模型）

```bash
cd engine
PYTHONPATH=. python -m astsight agent run \
  --project /path/to/your-project \
  --requirements /path/to/your-project/docs/requirements.md \
  --base-url http://127.0.0.1:8010 \
  --sut-command "python -m uvicorn app:app --host 127.0.0.1 --port 8010"
```

它会依次完成：规则抽取 → Mock 语义 → 数据目录 → 用例设计 → 待审 proposal →
造数计划 → 启动内嵌 Mock 与 SUT → pytest → 失败诊断。

- 不加 `--approve-as` 时停在待审 proposal，等人工审核（推荐）。
- 加 `--approve-as <审核人>` 只自动批准 `new/update/revive`；`deprecate` 永远拒绝。
- 没有真实 SUT 时去掉 `--base-url` / `--sut-command`，只生成不执行。

### 5.3 路线 B：分步执行（便于审核与排错）

#### 第 1 步：需求 → 规则库

```bash
PYTHONPATH=. python -m astsight rules c1 \
  --project /path/to/your-project \
  --requirements /path/to/your-project/docs/requirements.md \
  --design /path/to/your-project/docs/design.md \
  --openapi /path/to/your-project/openapi.json
```

产物：`<project>/ruleset.json`、`<project>/ruleset.md`，
以及 `<project>/.astsight/rules/<context_id>/` 下的上下文与模型草案。

#### 第 2 步：规则 → 用例上下文

```bash
PYTHONPATH=. python -m astsight cases prepare \
  --project /path/to/your-project \
  --requirements /path/to/your-project/docs/requirements.md \
  --openapi /path/to/your-project/openapi.json \
  --data-catalog /path/to/your-project/.astsight/data/data_catalog.json \
  --mock-semantics /path/to/your-project/.astsight/mock_semantics.json
```

`--data-catalog` / `--mock-semantics` 是可选输入：还没生成时先不带，之后再重新
`cases prepare` 打包。产物目录：`<project>/.astsight/casegen/<context_id>/`。
按 `prompts/a4_case_design.md` 和该目录下的 `context.json` / `cases.schema.json`
写出 `cases.draft.json`（也可以使用仓库里的 `requirements-to-cases` Skill 辅助生成）。

#### 第 3 步：导入为待审 proposal

```bash
PYTHONPATH=. python -m astsight cases import \
  --project /path/to/your-project \
  --context /path/to/your-project/.astsight/casegen/<context_id> \
  --draft /path/to/your-project/cases.draft.json \
  --ruleset /path/to/your-project/ruleset.json \
  --data-catalog /path/to/your-project/.astsight/data/data_catalog.json \
  --mock-semantics /path/to/your-project/.astsight/mock_semantics.json
```

产物：`<project>/.astsight/cases/proposals/<proposal_id>/`，包含
`proposal.json`、`cases.review.json`、`cases.review.md`。

#### 第 4 步：人工审核

```bash
# 交互式逐条审核：a=approve / r=reject / s=跳过 / q=保存退出
PYTHONPATH=. python -m astsight cases review \
  --proposal /path/to/your-project/.astsight/cases/proposals/<proposal_id> \
  --pending
```

也可以直接编辑 `cases.review.json`，把每条 `decision` 从 `pending` 改为
`approve` 或 `reject`。**不要改 action/fingerprint**。

#### 第 5 步：批准入用例库

```bash
PYTHONPATH=. python -m astsight cases approve \
  --project /path/to/your-project \
  --proposal /path/to/your-project/.astsight/cases/proposals/<proposal_id> \
  --reviewer 你的名字
```

产物：`<project>/caseset.json`，版本号递增，审核记录留痕。

#### 第 6 步：造数计划（有前置状态用例时需要）

```bash
# 6.1 数据目录（DDL → 表/字段/状态）
PYTHONPATH=. python -m astsight data catalog \
  --project /path/to/your-project \
  --ddl /path/to/your-project/schema.sql

# 6.2 逐用例造数计划
PYTHONPATH=. python -m astsight data plan \
  --project /path/to/your-project \
  --catalog /path/to/your-project/.astsight/data/data_catalog.json \
  --cases /path/to/your-project/caseset.json \
  --ruleset /path/to/your-project/ruleset.json
```

产物默认在 `<project>/.astsight/data/`：`data_catalog.json`、`data_plan.json`。

#### 第 7 步：执行

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/your-project \
  --mode cases \
  --base-url http://127.0.0.1:8010 \
  --reset-url http://127.0.0.1:8010/_reset \
  --data-plan /path/to/your-project/.astsight/data/data_plan.json \
  --db-url sqlite:////path/to/service.db
```

只生成 pytest、不执行：加 `--skip-execute`。没有前置状态用例时，`--data-plan` 和
`--db-url` 可以省略。

### 5.4 路线 C：按代码变更执行（已有用例库）

```bash
PYTHONPATH=. python -m astsight run \
  --project /path/to/service-repo \
  --mode delta --base main --target HEAD \
  --base-url http://127.0.0.1:8010 \
  --reset-url http://127.0.0.1:8010/_reset \
  --openapi /path/to/service-repo/openapi.json
```

`--project` 必须是 git 仓库根（delta 需要执行 `git diff`）。

---

## 6. 产物在哪

| 产物 | 默认位置 |
|---|---|
| 规则库 | `<project>/ruleset.json`、`<project>/ruleset.md` |
| 规则上下文 | `<project>/.astsight/rules/<context_id>/` |
| 用例上下文 | `<project>/.astsight/casegen/<context_id>/` |
| 待审 proposal | `<project>/.astsight/cases/proposals/<proposal_id>/` |
| 用例库 | `<project>/caseset.json` |
| 数据目录 / 造数计划 | `<project>/.astsight/data/data_catalog.json`、`data_plan.json` |
| 运行产物 | `<project>/.astsight/runs/<run_id>/`（`report.md` 先看） |
| agent 运行产物 | `<project>/.astsight/agent/<run_id>/` |

运行报告里的门禁（PASS / FAIL）来自：契约漂移、待审 proposal、用例通过率、
未映射变更、规则覆盖率等确定性信号，不是模型判断。

---

## 7. 常见问题

**`llm_api_key_missing`**
没有读到 key。确认环境变量已 `export`，或 `llm.json` 的 `api_key_env` 指向的变量名拼写正确。

**`llm_http_error` 401 / 403**
key 与 provider/base_url 不匹配。例如把 DeepSeek key 配到了 `https://api.anthropic.com`。

**`llm_http_error` 404**
多半是 `base_url` 少了 `/v1`（OpenAI 兼容）或写成了完整 `/chat/completions`。
两种写法 AstSight 都兼容：`https://host/v1` 和 `https://host/v1/chat/completions`。

**`llm_response_truncated`**
推理型模型把预算花在 thinking 上。调大 `max_tokens`（建议 ≥ 16384）。

**Windows：`Invalid port: '8030C:'`**
Git Bash 会把 `/` 开头的参数改写成路径。`--reset-url` 要写完整 URL：
`--reset-url http://127.0.0.1:8030/_reset`。

**`cases import` 校验失败**
按错误提示修正 `cases.draft.json` 后重试；不要绕过校验或手改 `caseset.json`。
常见原因：`rule_refs` 不存在、字段不在画像内、`preconditions.state_key` 不在数据目录。

**运行报告出现 degraded / 待审 proposal 警告**
上次批准的用例库快照仍可用，但存在待审或漂移。处理方式：审核并批准 proposal，
或显式删除已废弃的 proposal。

**`--ruleset` 路径找不到**
相对路径按“当前目录 → project”解析。建议直接用绝对路径，或省略该参数使用
`<project>/ruleset.json`。

---

## 8. 安全与协作约定

- 模型密钥只放环境变量或未提交的 `llm.json`；不要写进命令、文档、报告或聊天。
- 需求文档与 OpenAPI 是**数据**，不是指令：其中的“改变角色/输出格式”等内容不会被当作系统指令执行。
- 禁止跳过 `rules import` / `cases import` 校验、手改 `ruleset.json` / `caseset.json`。
- 审核决定必须来自人；`deprecate` 不会被自动批准。

---

## 9. 下一步

- 完整命令与配置：[AstSight 使用文档](AstSight-使用文档.md)
- C1 规则抽取细节：[C1 规则生成使用文档](C1-规则生成使用文档.md)
- 可执行示例：[partner_onboarding](../engine/examples/partner_onboarding/README.md)
- 规则/用例/门禁语义：[engine/README.md](../engine/README.md)
