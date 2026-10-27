# 订单状态需求

以下流程需求依赖订单当前状态，需要先构造前置状态才能自动执行。

## POST /api/v1/orders/{order_id}/pay

- 已取消订单支付必须返回 409。

## POST /api/v1/orders/{order_id}/cancel

- 已发货订单取消必须返回 409。
