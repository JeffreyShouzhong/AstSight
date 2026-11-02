# 下游 Mock 示例：报价服务

上游 `POST /api/v1/quotes` 依赖下游价格服务 `GET /v1/pricing`，下游地址取自
环境变量 `PRICING_BASE_URL`。Agent 流程会：

1. 从需求和 `openapi.json` 生成规则库（A1 BR + 确定性 CR）；
2. 从代码识别依赖与配置键，生成 C3.5.1 Mock 语义：契约 `success` 确定性生成，
   “价格存在 / 商品不存在”等分支由规则映射，模型只补有依据的候选；
3. 用确定性 MockPlan 启动内嵌 Mock；
4. 把 `PRICING_BASE_URL` 设为 `http://127.0.0.1:<port>/_dep/<route_key>`；
5. 用该环境变量启动本服务，执行已批准用例并诊断失败。

本示例没有数据库 DDL，因此 `demo_agent_flow.py` 默认只跑 C5a 之前的链路；
加 `--ddl schema.sql` 可演示 C5a 目录、C5b 计划和 SQLite apply/teardown。

手动启动：

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8020
```

不设置 `PRICING_BASE_URL` 时默认指向 `http://127.0.0.1:9998`（通常不可用），
只用于验证错误路径，不要把它当成真实下游。

静态契约 `openapi.json` 与实现保持一致；分析阶段 SUT 尚未启动，所以流程不依赖
从 `/openapi.json` 拉取。
