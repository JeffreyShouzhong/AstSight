# 订单服务接口设计

本文档描述订单中心关键接口的请求字段约束，供需求→用例生成与人工审核使用。

## POST /api/v1/orders

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| customer_id | string | 是 | 长度 3-40 |
| sku | string | 是 | 长度 3-40 |
| quantity | integer | 是 | 1 <= quantity <= 99 |
| note | string | 否 | 最大长度 100 |

## PATCH /api/v1/orders/{order_id}

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| status | string | 是 | 枚举 pending / paid / shipped / cancelled |
| note | string | 否 | 最大长度 100 |

## POST /api/v1/orders/{order_id}/items

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| sku | string | 是 | 长度 3-40 |
| quantity | integer | 是 | 1 <= quantity <= 99 |

## POST /api/v1/customers

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| customer_id | string | 是 | 长度 3-40 |
| name | string | 是 | 长度 1-60 |
| email | string | 是 | 长度 5-120 |

## POST /api/v1/coupons/validate

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| code | string | 是 | 长度 3-40 |
| order_total | number | 是 | 订单金额，范围 0-1000000 |
