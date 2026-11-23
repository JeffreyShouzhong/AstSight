# 贡献指南

感谢你想为 AstSight 贡献力量。这份指南说明如何本地开发、跑测试、提交改动，以及我们最需要哪类贡献。

## 一句话定位

AstSight 是**引擎，不是平台**：它不做用户管理、用例管理 UI、任务调度。判断一个 PR 是否合入，第一条标准就是它是否违背这个定位。

## 我们最需要的贡献

按优先级排序：

1. **文档纠错**：命令跑不通、参数名对不上、示例过时——这类是最容易合入的。
2. **边界澄清**：你在自己的项目上使用时发现某条“当前边界”写得不准，或某处行为与文档不符。
3. **新数据库方言 / 新框架路由 / 新文档格式**：当前还没有公开的插件接口，
   需要改代码或在现有模块内加分支；插件接口方向请先开 issue 讨论，
   见 ROADMAP 的“长期 / 待讨论”。
4. **Bug 修复**：附带一个能复现的最小用例的 PR，合入速度远快于只描述现象的 issue。

暂不欢迎：大范围重写架构、新增 UI/平台功能、改变“模型只出候选、人工审核是硬门禁”这两个设计原则的改动。这类想法请先开 issue 讨论。

## 环境

- Python 3.11+（仓库最近在 3.13 验证）
- 目标运行环境 Linux（服务器 / CI）；Windows 仅用于本地调试，不作为验收环境

```bash
cd engine
pip install -e ".[dev]"
astsight --help
```

默认安装不包含 `pytest`、`langgraph` 与 PostgreSQL/MySQL 驱动，它们在 `dev` extra 中。

## 跑测试

```bash
cd engine
python -m pytest
```

提交 PR 前请确保全部测试通过。如果你在 Windows 上跑，遇到临时目录路径过长（`WinError 206`），用短 `basetemp` 即可，与代码无关：

```bash
PYTHONPATH=. python -m pytest --basetemp=C:/tmp/pt
```

## 提交规范

- commit message 用英文，遵循 conventional commits：`feat:` / `fix:` / `docs:` / `test:` / `chore:`。
- 一个 PR 只做一件事。改动范围越聚焦，review 越快。
- 新增行为必须有对应测试；改动现有行为时更新受影响的测试。

## 发布

1. 更新 `engine/pyproject.toml` 的 `version`（使用稳定版本，如 `0.2.0`）并提交。
2. 打 tag 并推送：

```bash
git tag v0.2.0
git push origin v0.2.0
```

3. GitHub Actions 的 `Release` 工作流会自动：
   - 校验 tag 与包版本一致；
   - 构建 wheel/sdist 并 `twine check`；
   - 解析主包依赖里的 `astsight-codegraph-bin` 固定版本，检查 PyPI 上
     Linux x86_64/arm64、macOS x86_64/arm64、Windows x86_64 五个平台 wheel；
     缺失时自动构建并 `--skip-existing` 上传，主包随后才发布；
   - 上传主包到 PyPI；
   - 创建 GitHub Release 并附上构建产物。

仓库需要配置 secret `PYPI_API_TOKEN`（PyPI API token）。也可以改用 PyPI Trusted
Publishing：在 PyPI 项目设置里绑定该仓库和 `pypi` environment，然后删除工作流里的
`password` 行。

`workflow_dispatch` 只构建、不发布，用于验证打包。CodeGraph 平台 wheel 的版本独立
（当前 1.6.2），升级时用 `tools/build_codegraph_wheels.py` 单独构建和发布。

## 两个不可协商的边界

1. **模型只出候选**：任何模型输出都必须经过框架的 endpoint / quote / rule_key / FK 拓扑 / 断言全套校验，不能直接落库。
2. **审核不能自动化**：`cases approve` 必须显式给 `--reviewer`，不接受任何“自动全量批准”的参数。demo 的自动批准仅用于演示，且拒绝自动批准 `deprecate`。

如果你的 PR 试图绕过这两条，会被直接拒绝。

## 许可证

本项目采用 Apache License 2.0。贡献代码即表示你同意在该许可证下授权你的改动。

## 找我们

- 开 issue 描述问题或想法，附上最小复现步骤。
- 大方向性的改动，先开 issue 或 discussion 讨论，再动工，避免白做。
