"""AstSight M1：确定性闭环。

链路 A：git diff → 变更符号 → 影响接口 → 画像 → 模板用例 → pytest 编码
        → 执行 → 确定性归因 → 报告。
链路 B：需求/设计文档 + OpenAPI → 用例草案 → 人工审核 → 版本化 case store
        → 编码 → 执行 → 审核/需求覆盖报告。

设计基线：`framework-capability-design.md` v2.16（C1/C2/C3/C4/C6/C7）。
M1 不含 LLM、不含数据库造数（C5）、不含 Mock：这几项是后续里程碑。
"""

__version__ = "0.1.0-m1"
