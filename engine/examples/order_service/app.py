"""AstSight 内置示例服务：订单中心（FastAPI）。

用途只有一个：给 M1 的 delta 闭环提供一个**真实可跑、契约完整、状态可重置**的
被测系统。刻意保持：

- 18 个接口，覆盖读写、路径参数、查询参数、枚举、数值/长度约束；
- OpenAPI 由 FastAPI 自动产出，不手写契约（避免契约与实现漂移）；
- `POST /_reset` 重置内存数据——生成器逐条用例前调用，保证正向用例可重复；
- 不接数据库、不接外部依赖，一条 `uvicorn` 命令即可启动。

注意：示例服务**不在本仓库根目录初始化 git**。delta 模式要求 project 是独立
git 仓库根，请参考 `examples/demo_delta.py`（自动建临时仓库）或自行 `git init`。
"""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

app = FastAPI(
    title="AstSight Order Service",
    version="1.0.0",
    description="AstSight M1 内置示例：订单、商品、客户与优惠券。",
)

OrderStatus = Literal["pending", "paid", "shipped", "cancelled"]


# ----------------------------------------------------------------- 模型


class OrderCreate(BaseModel):
    customer_id: str = Field(..., min_length=3, max_length=40)
    sku: str = Field(..., min_length=3, max_length=40)
    quantity: int = Field(..., ge=1, le=99)
    note: str | None = Field(default=None, max_length=100)


class OrderUpdate(BaseModel):
    status: OrderStatus = Field(..., description="目标状态")
    note: str | None = Field(default=None, max_length=100)


class OrderItem(BaseModel):
    item_id: int
    sku: str
    quantity: int


class Order(BaseModel):
    order_id: int
    customer_id: str
    status: OrderStatus
    total_cents: int
    note: str | None = None
    items: list[OrderItem] = Field(default_factory=list)


class ItemCreate(BaseModel):
    sku: str = Field(..., min_length=3, max_length=40)
    quantity: int = Field(..., ge=1, le=99)


class CustomerCreate(BaseModel):
    customer_id: str = Field(..., min_length=3, max_length=40)
    name: str = Field(..., min_length=1, max_length=60)
    email: str = Field(..., min_length=5, max_length=120)


class Customer(BaseModel):
    customer_id: str
    name: str
    email: str


class Product(BaseModel):
    sku: str
    name: str
    price_cents: int
    category: str


class CouponRequest(BaseModel):
    code: str = Field(..., min_length=3, max_length=40)
    order_total: float = Field(..., ge=0, le=1_000_000)


class CouponResult(BaseModel):
    code: str
    valid: bool
    discount_cents: int


# ----------------------------------------------------------------- 内存数据

_PRODUCTS: dict[str, Product] = {}
_CUSTOMERS: dict[str, Customer] = {}
_ORDERS: dict[int, dict] = {}
_NEXT_ORDER_ID = 1
_NEXT_ITEM_ID = 1


def seed() -> None:
    """重置为固定种子数据。固定 ID 是刻意的：确定性用例需要可引用的资源。"""
    global _NEXT_ORDER_ID, _NEXT_ITEM_ID
    _PRODUCTS.clear()
    _CUSTOMERS.clear()
    _ORDERS.clear()

    _PRODUCTS["SKU-1001"] = Product(
        sku="SKU-1001", name="机械键盘", price_cents=49900, category="electronics"
    )
    _PRODUCTS["SKU-1002"] = Product(
        sku="SKU-1002", name="无线鼠标", price_cents=12900, category="electronics"
    )
    _PRODUCTS["BOOK-2001"] = Product(
        sku="BOOK-2001", name="测试之美", price_cents=6900, category="books"
    )
    _CUSTOMERS["cust_1001"] = Customer(
        customer_id="cust_1001", name="张三", email="zhangsan@example.com"
    )
    _CUSTOMERS["cust_1002"] = Customer(
        customer_id="cust_1002", name="李四", email="lisi@example.com"
    )
    _ORDERS[1] = {
        "order_id": 1,
        "customer_id": "cust_1001",
        "status": "pending",
        "total_cents": 49900,
        "note": "种子订单",
        "items": [{"item_id": 1, "sku": "SKU-1001", "quantity": 1}],
    }
    _ORDERS[2] = {
        "order_id": 2,
        "customer_id": "cust_1002",
        "status": "paid",
        "total_cents": 12900,
        "note": None,
        "items": [{"item_id": 2, "sku": "SKU-1002", "quantity": 1}],
    }
    _NEXT_ORDER_ID = 3
    _NEXT_ITEM_ID = 3


seed()


def _order(order_id: int) -> dict:
    order = _ORDERS.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order


def _as_order(order: dict) -> Order:
    return Order(**order)


# ----------------------------------------------------------------- 系统


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/_reset", tags=["system"], include_in_schema=False)
def reset() -> dict[str, str]:
    seed()
    return {"status": "reset"}


# ----------------------------------------------------------------- 订单


@app.post("/api/v1/orders", response_model=Order, status_code=201, tags=["orders"])
def create_order(payload: OrderCreate) -> Order:
    global _NEXT_ORDER_ID, _NEXT_ITEM_ID
    price = _PRODUCTS[payload.sku].price_cents if payload.sku in _PRODUCTS else 1000
    order = {
        "order_id": _NEXT_ORDER_ID,
        "customer_id": payload.customer_id,
        "status": "pending",
        "total_cents": price * payload.quantity,
        "note": payload.note,
        "items": [
            {"item_id": _NEXT_ITEM_ID, "sku": payload.sku, "quantity": payload.quantity}
        ],
    }
    _ORDERS[_NEXT_ORDER_ID] = order
    _NEXT_ORDER_ID += 1
    _NEXT_ITEM_ID += 1
    return _as_order(order)


@app.get("/api/v1/orders", response_model=list[Order], tags=["orders"])
def list_orders(
    status: OrderStatus | None = Query(default=None, description="按状态过滤"),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[Order]:
    items = list(_ORDERS.values())
    if status is not None:
        items = [order for order in items if order["status"] == status]
    return [_as_order(order) for order in items[:limit]]


@app.get("/api/v1/orders/{order_id}", response_model=Order, tags=["orders"])
def get_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:
    return _as_order(_order(order_id))


@app.patch("/api/v1/orders/{order_id}", response_model=Order, tags=["orders"])
def update_order(
    payload: OrderUpdate,
    order_id: int = Path(..., ge=1, examples=[1]),
) -> Order:
    order = _order(order_id)
    order["status"] = payload.status
    if payload.note is not None:
        order["note"] = payload.note
    return _as_order(order)


@app.delete("/api/v1/orders/{order_id}", status_code=204, tags=["orders"])
def delete_order(order_id: int = Path(..., ge=1, examples=[1])) -> Response:
    _order(order_id)
    del _ORDERS[order_id]
    return Response(status_code=204)


@app.post("/api/v1/orders/{order_id}/pay", response_model=Order, tags=["orders"])
def pay_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:
    order = _order(order_id)
    if order["status"] == "cancelled":
        raise HTTPException(status_code=409, detail="cancelled order cannot be paid")
    order["status"] = "paid"
    return _as_order(order)


@app.post("/api/v1/orders/{order_id}/cancel", response_model=Order, tags=["orders"])
def cancel_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:
    order = _order(order_id)
    if order["status"] == "shipped":
        raise HTTPException(status_code=409, detail="shipped order cannot be cancelled")
    order["status"] = "cancelled"
    return _as_order(order)


@app.post("/api/v1/orders/{order_id}/refund", response_model=Order, tags=["orders"])
def refund_order(order_id: int = Path(..., ge=1, examples=[1])) -> Order:
    # 示例服务刻意做成幂等：退款只校验订单存在，便于正向用例反复执行。
    order = _order(order_id)
    order["status"] = "cancelled"
    return _as_order(order)


# ----------------------------------------------------------------- 订单明细


@app.get(
    "/api/v1/orders/{order_id}/items",
    response_model=list[OrderItem],
    tags=["items"],
)
def list_items(order_id: int = Path(..., ge=1, examples=[1])) -> list[OrderItem]:
    return [OrderItem(**item) for item in _order(order_id)["items"]]


@app.post(
    "/api/v1/orders/{order_id}/items",
    response_model=OrderItem,
    status_code=201,
    tags=["items"],
)
def add_item(
    payload: ItemCreate,
    order_id: int = Path(..., ge=1, examples=[1]),
) -> OrderItem:
    global _NEXT_ITEM_ID
    order = _order(order_id)
    item = {"item_id": _NEXT_ITEM_ID, "sku": payload.sku, "quantity": payload.quantity}
    _NEXT_ITEM_ID += 1
    order["items"].append(item)
    price = _PRODUCTS[payload.sku].price_cents if payload.sku in _PRODUCTS else 1000
    order["total_cents"] += price * payload.quantity
    return OrderItem(**item)


@app.delete(
    "/api/v1/orders/{order_id}/items/{item_id}",
    status_code=204,
    tags=["items"],
)
def delete_item(
    order_id: int = Path(..., ge=1, examples=[1]),
    item_id: int = Path(..., ge=1, examples=[1]),
) -> Response:
    order = _order(order_id)
    remaining = [item for item in order["items"] if item["item_id"] != item_id]
    if len(remaining) == len(order["items"]):
        raise HTTPException(status_code=404, detail="item not found")
    order["items"] = remaining
    return Response(status_code=204)


# ----------------------------------------------------------------- 客户


@app.post("/api/v1/customers", response_model=Customer, status_code=201, tags=["customers"])
def create_customer(payload: CustomerCreate) -> Customer:
    customer = Customer(**payload.model_dump())
    _CUSTOMERS[customer.customer_id] = customer
    return customer


@app.get(
    "/api/v1/customers/{customer_id}",
    response_model=Customer,
    tags=["customers"],
)
def get_customer(
    customer_id: str = Path(..., min_length=1, examples=["cust_1001"]),
) -> Customer:
    customer = _CUSTOMERS.get(customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="customer not found")
    return customer


@app.get(
    "/api/v1/customers/{customer_id}/orders",
    response_model=list[Order],
    tags=["customers"],
)
def customer_orders(
    customer_id: str = Path(..., min_length=1, examples=["cust_1001"]),
) -> list[Order]:
    if customer_id not in _CUSTOMERS:
        raise HTTPException(status_code=404, detail="customer not found")
    return [
        _as_order(order)
        for order in _ORDERS.values()
        if order["customer_id"] == customer_id
    ]


# ----------------------------------------------------------------- 商品


@app.get("/api/v1/products", response_model=list[Product], tags=["products"])
def list_products(
    category: str | None = Query(default=None, max_length=40),
) -> list[Product]:
    products = list(_PRODUCTS.values())
    if category is not None:
        products = [p for p in products if p.category == category]
    return products


@app.get("/api/v1/products/{sku}", response_model=Product, tags=["products"])
def get_product(
    sku: str = Path(..., min_length=1, examples=["SKU-1001"]),
) -> Product:
    product = _PRODUCTS.get(sku)
    if product is None:
        raise HTTPException(status_code=404, detail="product not found")
    return product


# ----------------------------------------------------------------- 优惠券


@app.post("/api/v1/coupons/validate", response_model=CouponResult, tags=["coupons"])
def validate_coupon(payload: CouponRequest) -> CouponResult:
    discount = 0
    if payload.code.upper().startswith("SAVE10"):
        discount = min(int(payload.order_total * 10), 5000)
    return CouponResult(code=payload.code, valid=discount > 0, discount_cents=discount)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8010)
