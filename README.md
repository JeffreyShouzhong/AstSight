# AstSight

> **名称释义**：AST（抽象语法树）+ Sight（视野） ｜ **CLI**：`astsight` ｜ **Maven**：`io.astsight:astsight-core` ｜ **设计基线**：v2.16

**上一代平台问「你测了多少条」；AstSight 只回答「这次变更带来什么质量结论」。**

这个名字说的是它的地基：**先看得见结构，才谈得上判定。** AST 是前半段——框架靠代码图谱（tree-sitter 解析出的语法树）知道「改了什么」；Sight 是后半段——再靠契约与不变量说清「对不对」。缺了前半段，判定没有着力点；缺了后半段，看得再清也只是个图形化工具。


---

## 这是什么

一套**接口自动化测试引擎**（不是平台，不做用户管理/用例管理 UI/任务调度）。输入是代码库、需求与设计文档、数据库结构；输出是**带证据等级的判定结论**，不是测试报告。

```
变更 / 全量 / 历史数据
        │
        ▼
   感知层（C2 变更感知 · C3 接口画像 · C5 数据准备）
        │
        ▼
   生成层（需求→用例上下文 · 人工审核 · C4 用例库 · C6 编码）
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
| `framework-capability-design.md` | **设计基线 v2.16**：C1–C7 能力规格、数据契约、门禁指标、风险对策 | 主文档 |
| `agent-architecture.md` | Agent 切分与内部架构（3 Agent + 2 能力服务），确定性状态机边界 | 与主文档对齐 |
| `CodeGraph-集成设计.md` | 代码图谱接入方式（**代码嵌入**，MCP 已否决）、库 API 全清单、路由静默缺陷与交叉校验方案 | 补丁 v2.0 |
| `第三方方案理论依据分析-腾讯云API智能测试平台.md` | 同源竞品的理论依据核对（含 MAST 多智能体失败实证） | 对照 |
| `implementation-feasibility.md` | 立项前可行性调研（四来源交叉验证） | 历史 |
| `api-test-platform-feasibility.md` | 腾讯云方案可行性专项研究 | 历史 |
| `similar-articles.md` | 同类文章与开源项目清单 | 参考 |
| `engine/README.md` | 正式实现进度（M1 delta 闭环 / 需求→审核用例→版本化 / CodeGraph 抽象） | 代码 |

> **设计文档口径更新**：`framework-capability-design.md` 与 `agent-architecture.md` 中的
> C1 规则库、A1.5 规则确认等章节已被实现层取代：当前流程是“需求文档 + OpenAPI →
> 用例草案 → 人工审核 → 版本化用例库 → pytest”，不再抽取中间规则层。两份文档保留历史
> 设计推演，相关章节以文首的 superseded 说明为准。

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

当前可运行的里程碑是 **M1 确定性闭环 + 需求→审核用例→版本化**（不接 LLM SDK）。
C2 变更感知已支持 **Python AST + CodeGraph 文件符号表**：Java/Spring 项目可做到
`git 分支 → 受影响 API 列表`（API 级）；非 Python 行的符号边界来自 CodeGraph，
框架内不另写解析器。

```bash
cd engine
pip install -r requirements.txt            # langgraph / pydantic / httpx / PyYAML / pytest / fastapi / uvicorn

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

# 需求驱动：先生成上下文包，再由 Skill/人工写草案
PYTHONPATH=. python -m astsight cases prepare \
  --project /path/to/repo \
  --requirements /path/to/repo/docs \
  --base-url http://127.0.0.1:8010

# 导入草案（只生成待审 proposal，不改用例库）
PYTHONPATH=. python -m astsight cases import \
  --project /path/to/repo \
  --context /path/to/repo/.astsight/casegen/<context_id> \
  --draft /path/to/cases.draft.json

# 人工编辑 proposal 下的 cases.review.json 后批准
PYTHONPATH=. python -m astsight cases approve \
  --project /path/to/repo \
  --proposal /path/to/repo/.astsight/cases/proposals/<proposal_id> \
  --reviewer alice

# 只执行已审核通过的用例
PYTHONPATH=. python -m astsight run --project /path/to/repo --mode cases \
  --base-url http://127.0.0.1:8010
```

项目级用例库默认是 `<project>/caseset.json`（`--case-base` 可改）。`TC-NNNN` 只在审核批准
写库时分配，编号永不复用；更新/恢复沿用原编号并升版本，旧版本进入 `history` 标
`superseded`。待审草案在 `.astsight/cases/proposals/`，默认不阻塞运行但报告标 `degraded`；
`--fail-on-pending-proposals` 可升级为门禁 blocker。

目录：`astsight/`（M1 流水线）· `astsight/cases/`（上下文/校验/proposal/用例库/模板用例）·
`contracts/`（数据契约）· `codegraph/`（图谱客户端抽象）· `orchestration/`（LangGraph 编排骨架，
当前 CLI 主路径未接入，实际执行走 `astsight/pipeline.py`）· `examples/`（示例服务与 demo）· `tests/`。

M1 的边界：用例断言仍以 L1 为主（状态码/字段存在/字段值），需要前置状态的需求进入
`uncovered_requirements`，不伪造单请求用例；自动“矫正”只含生成物自检与环境抖动重试；
审核必须人工完成（`cases approve --reviewer`），框架不提供自动全量批准。未覆盖范围、
未执行用例、待审 proposal、契约/需求漂移在报告中单列，不静默通过。

本机可直接用的解释器（已装齐依赖）：`C:\Users\magic\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
