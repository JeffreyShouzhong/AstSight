"""AstSight M1：确定性闭环。

链路 A：git diff → 变更符号 → 影响接口 → 画像 → 模板用例 → pytest 编码
        → 执行 → 确定性归因 → 报告。
链路 B：需求/设计文档 + OpenAPI → 用例草案 → 人工审核 → 版本化 case store
        → 编码 → 执行 → 审核/需求覆盖报告。

设计基线：`framework-capability-design.md` v4.6.1（C1/C2/C3/C4/C6/C7）。
HTTP Mock 首版只支持明文 HTTP；下游 `dep_id` 含 downstream_key，运行期按 route_key 路由
（见 C3.5.5）。
目标支持 HTTP、gRPC（仅 unary）、Dubbo 2.7+（Java sidecar 泛化调用）；
HTTP 客户端（含 Feign）一律按普通出站调用处理，不做客户端框架专用支持；
v4.3 起 SUT → Dubbo 下游 Mock 走 agent + ByteBuddy + Filter（低侵入）。
当前实现覆盖 HTTP/OpenAPI 与 HTTP Mock 运行时核心及执行链路（C3.5.5：dep_id/route_key、
路由、会话分支、drain、晚到调用归因、证据），以及 Python 源码的 C3 出站 HTTP 调用点
自动识别与 MockPlan 草案；把注入写入 SUT 启动环境、gRPC/Dubbo、规则层与 C5 造数属后续里程碑。
"""

__version__ = "0.1.0-m1"
