# C5b 单用例造数计划 · 主提示词

> 输入是已批准用例、`data_catalog`、规则和 Mock 语义。
> 你输出逐用例的 fixture 候选；SQL 由框架从通过校验的结构参数化渲染。
> 输出必须符合调用方给的 `schema`（`DataPlanDraft`）。

---

## 输入

- `catalog.tables[]` / `enum_values` / `states[]`：可用表、列、枚举、业务态；
- `rules[]`：用例引用的规则；
- `cases[]`：已批准用例，含 `case_key`、`preconditions[].state_key`、请求和 Mock 绑定；
- `mock_semantics[]`：下游分支（仅用于判断状态是否需要配合）；
- `schema`：输出 JSON Schema。

---

## 任务

对**每条用例**判断它需要什么初始数据，输出：

```json
{
  "plans": {
    "order_stock_insufficient": {
      "fixtures": {
        "products": [{"sku_id": "S001", "stock": 0, "status": "active"}]
      },
      "target_state": {
        "orders": {"status": "cancelled"}
      }
    }
  },
  "gaps": ["orders.amount 的币种单位无法从文档确定"],
  "questions": ["是否需要清理历史订单？"]
}
```

### 规则

1. `plans` 的 key 必须是用例 `case_key`，不能造新 key。
2. `fixtures` 的表/列必须存在于 catalog；值类型必须匹配；枚举必须来自列定义。
3. 用例已有 `preconditions[].state_key` 时，框架会展开 catalog 中该状态；
   你只需补充该状态没有覆盖、但触发规则必需的字段/行，不要重复整行。
4. FK 闭包：子表行引用的父表行必须在同一用例的 fixtures 中出现。
5. `target_state` 只描述断言关心的期望数据；没有就省略或写 `{}`。
6. 不输出 SQL、`teardown`、`tables_order` 或 `fk_links`；这些由框架确定性生成。
7. 无法造出的状态不要硬编，写进 `gaps`，框架会把该用例标为 rejected/blocked。
8. 不引用 catalog 之外的表/列；不编造枚举值。

---

## 铁律

1. 只输出 JSON object。
2. 不产出 SQL。
3. 不修改用例的请求、断言或规则归属。
4. 每个 fixture 值都必须能用 catalog 的列类型/枚举解释。
5. 无法确定就留空并写 gap，不要猜。
