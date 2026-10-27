# 示例服务：订单中心

AstSight M1 的内置被测系统。FastAPI + 内存数据，18 个接口，契约由框架自动产出。

## 启动

```bash
cd engine/examples/order_service
python -m uvicorn app:app --host 127.0.0.1 --port 8010
```

- OpenAPI：`http://127.0.0.1:8010/openapi.json`
- 重置数据：`POST /_reset`（未写入 OpenAPI，供生成的 conftest 逐用例调用）
- 固定种子：订单 1/2、客户 `cust_1001`/`cust_1002`、商品 `SKU-1001`/`SKU-1002`/`BOOK-2001`

接口分组：

| 分组 | 接口 |
|---|---|
| 系统 | `GET /health`、`POST /_reset`（隐藏） |
| 订单 | `POST/GET /api/v1/orders`、`GET/PATCH/DELETE /api/v1/orders/{order_id}`、`/pay`、`/cancel`、`/refund` |
| 明细 | `GET/POST /api/v1/orders/{order_id}/items`、`DELETE .../items/{item_id}` |
| 客户 | `POST /api/v1/customers`、`GET /api/v1/customers/{customer_id}`、`GET .../orders` |
| 商品 | `GET /api/v1/products`、`GET /api/v1/products/{sku}` |
| 优惠券 | `POST /api/v1/coupons/validate` |

## 文档与用例样例

`docs/` 是需求→用例流程的输入样例：

- `api-design.md`：5 个接口的字段表与约束；
- `requirements.md`：订单状态流转需求（其中 pay/cancel 需要前置状态）；
- `PRD-sample.md`：独立的合伙人注册/审核/工作台 PRD，仅作文档解析样例，不参与订单服务覆盖率。

`case_samples/cases.draft.json` 是人工/Skill 产出的草案样例：

- 4 条可单请求执行的用例（创建订单 quantity 边界、缺 customer_id、非法状态、支付成功）；
- pay/cancel 等需要 C5 造数的需求写入 `uncovered_requirements`，原因 `needs_c5_state_setup`，
  不伪造一条“打 200 就算过”的用例。

```bash
cd engine
python examples/demo_case_review.py                 # 草案 → 审核 → 版本化 → 执行
python examples/demo_case_review.py --no-auto-approve  # 停在待审 proposal
```

demo 使用临时工作区：`cases prepare` 产出 `.astsight/casegen/<context_id>/`，
`cases import` 产出 `.astsight/cases/proposals/<proposal_id>/`，批准后写入
`<project>/caseset.json`。示例脚本的自动批准仅用于演示，且**不会**自动批准废弃项；
真实流程必须人工编辑 `cases.review.json` 后执行 `cases approve --reviewer <name>`。

## 注意

- 这个目录**不是独立 git 仓库**。`delta` 模式要求 `--project` 指向仓库根，请用
  `examples/demo_delta.py`（自动建临时仓库），或把 `app.py` 复制到自己的仓库再跑。
- `POST /_reset` 被刻意排除在 OpenAPI 之外，因此 full 模式会把它列为
  `contract_incomplete` 覆盖缺口——这是预期行为：AstSight 不替被测系统猜测隐藏接口。
