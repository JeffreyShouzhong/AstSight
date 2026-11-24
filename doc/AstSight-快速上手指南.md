# AstSight 快速上手指南

> 目标：5 分钟内把 AstSight 跑起来，在自己的项目上生成并执行第一轮用例。
> 完整参数和原理见 [AstSight 使用文档](AstSight-使用文档.md)。

## 1. 安装

```bash
pip install astsight
astsight init
export ASTSIGHT_LLM_API_KEY=你的key
```

`astsight init` 会生成 `~/.astsight/llm.json`。如果你已经设置过
`ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN` 或 `OPENAI_API_KEY`，它会自动识别。

检查安装：

```bash
astsight --help
astsight codegraph status
```

`codegraph status` 显示 `source=packaged` 即可，不需要另装 Node。

## 2. 在你的项目上跑

前提：

- 项目是 git 仓库，当前分支能对比 `main`；
- 有一份需求文档，例如 `docs/requirements.md`；
- 被测服务已经在运行，例如 `http://127.0.0.1:8010`。

```bash
cd /path/to/your-project

astsight agent run \
  --project . \
  --requirements docs/requirements.md \
  --mode delta --base main --target HEAD \
  --base-url http://127.0.0.1:8010
```

如果 SUT 还没启动，让 AstSight 帮你启动：

```bash
astsight agent run \
  --project . \
  --requirements docs/requirements.md \
  --mode delta --base main --target HEAD \
  --base-url http://127.0.0.1:8010 \
  --sut-command "python -m uvicorn app:app --host 127.0.0.1 --port 8010"
```

它会完成：规则抽取 → Mock 语义 → 数据目录 → 用例设计 → 待审 proposal →
造数计划 → 启动 Mock/SUT → 执行 → 失败诊断与修复。

## 3. 第一次会被审核门挡住

默认不加 `--approve-as` 时，流程停在待审 proposal，这是有意的：

```bash
astsight cases status --project .
astsight cases review \
  --proposal .astsight/cases/proposals/<proposal_id> --pending
astsight cases approve \
  --project . \
  --proposal .astsight/cases/proposals/<proposal_id> \
  --reviewer <你的名字>
```

审核完重新执行第 2 步的命令。只有演示环境才建议直接加
`--approve-as <审核人>` 自动批准 `new/update/revive`。

## 3.1 Java 项目

Java 项目和普通项目用同一条命令，只需要把 SUT 启动命令换成 `java`：

```bash
astsight agent run \
  --project . \
  --requirements docs/requirements.md \
  --mode delta --base main --target HEAD \
  --base-url http://127.0.0.1:8010 \
  --sut-command "java -jar target/app.jar"
```

- `--project` 仍然是 git 仓库根；
- `--sut-command` 换成你的实际启动命令，Spring Boot fat jar 就是
  `java -jar <包名>.jar`；
- 不需要额外配置 agent、`JAVA_TOOL_OPTIONS` 或启动脚本，AstSight 会在启动 SUT 时处理；
- 服务已经启动时，去掉 `--sut-command`，只保留 `--base-url`。

需要排查注入细节时，再看[使用文档](AstSight-使用文档.md)的 Agent 章节。

## 4. 看结果

```text
<项目>/.astsight/runs/<run_id>/report.md
```

先看 `report.md` 的门禁结论、失败归因、未映射范围和 Mock 证据。

## 5. 常见问题

**模型没配置**

```bash
astsight init
```

**项目不是 git 仓库，或第一次没有基线**

```bash
astsight agent run ... --mode full
```

**只想生成，不执行**

去掉 `--base-url`，或加 `--skip-execute`。

**CodeGraph 不生效**

```bash
astsight codegraph status
```

不是 `source=packaged` 时执行 `astsight codegraph install`。

**门禁失败，想反馈**

run 目录会自动生成 `feedback.zip`；也可以：

```bash
astsight feedback collect --run .astsight/runs/<run_id>
astsight feedback open --run .astsight/runs/<run_id>
```

## 6. 想跑源码示例

以下 demo 只存在于源码仓库的 `engine/examples/`，PyPI 安装不包含：

```bash
cd engine
python examples/demo_delta.py
python examples/demo_case_review.py
python examples/demo_agent_flow.py --keep   # 需要模型
```

## 7. 下一步

- 需要分步控制规则、审核、造数：看[使用文档](AstSight-使用文档.md)的 `rules`、`cases`、`data` 命令；
- 需要 Mock、MQ、Dubbo、gRPC：看使用文档对应章节；
- 只用确定性链路、不接模型：`astsight run --mode delta`。
