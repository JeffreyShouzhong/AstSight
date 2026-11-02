# Legacy 提示词（规则层恢复前）

这里的提示词属于 v3.0「需求 → 语义分析 → 用例草案」旧链路，**不再被 `agent run`
规则驱动主流程调用**：

- `a1_semantic_analysis.md`：旧 A1 语义分析。规则层恢复后，A1 由
  `a1_requirement_analysis.md` 抽取 BR 规则；该文件只保留给
  `astsight.agent.semantic` 做兼容。
- `agent_case_generation.md`：旧 C4 直接生成用例。现由 `a4_case_design.md`
  基于规则、画像、Mock 语义和造数目录生成草案；该文件只保留给
  `astsight.agent.casegen` 做兼容。
- `requirements_to_cases.md`：旧 `cases prepare` 直接生成契约。现在
  `cases prepare` 的 `prompt.md` 改用 `a4_case_design.md`，该文件不再被代码引用。

新链路请使用：

- `prompts/a1_requirement_analysis.md`
- `prompts/c35_mock_semantics.md`
- `prompts/c5_data_catalog.md`
- `prompts/c5_data_plan.md`
- `prompts/a4_case_design.md`

旧模块与提示词保留的原因是可回滚与审计，不代表当前设计口径。不要在新代码中引用。
