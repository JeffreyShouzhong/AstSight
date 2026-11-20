"""AstSight M1：确定性闭环。

链路 A：git diff → 变更符号 → 影响接口 → 画像 → 模板用例 → pytest 编码
        → 执行 → 确定性归因 → 报告。
链路 B：需求/设计文档 + ContractModel → 规则库 → 用例草案 → 人工审核 → 版本化 case store
        → 编码 → 执行 → 审核/需求覆盖报告。

设计基线：`framework-capability-design.md` v4.95（C1/C2/C3/C4/C5/C6/C7）。
HTTP Mock 首版只支持明文 HTTP；下游 `dep_id` 含 downstream_key，运行期按 route_key 路由
（见 C3.5.5）。
目标支持 HTTP、gRPC（仅 unary）、Dubbo 2.7+（Java sidecar 泛化调用）；
HTTP 客户端（含 Feign）一律按普通出站调用处理，不做客户端框架专用支持；
v4.3 起 SUT → Dubbo 下游 Mock 走 agent + ByteBuddy + Filter（低侵入）。
当前实现覆盖规则层、HTTP/gRPC/Dubbo 契约源、画像、审核用例库、C5 造数、
pytest codegen、执行归因与统一 Mock 运行时。
"""

__version__ = "0.1.0.dev0"
