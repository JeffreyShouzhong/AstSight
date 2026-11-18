# C5b 单用例造数计划 · 主提示词

> 输入是已批准用例、`data_catalog`、规则和 Mock 语义。
> 你输出逐用例的 fixture 候选；SQL 由框架从通过校验的结构参数化渲染。
> 输出必须符合调用方给的 `schema`（`DataPlanDraft`）。

---

## 输入

- `catalog.tables[]` / `enum_values` / `states[]`：可用表、列、枚举、业务态；
- `rules[]`：用例引用的规则；
- `cases[]`：已批准用例，含 `case_key`、`preconditions[].state_key`、
  `data`（用例声明的业务实体）、已解析 `$data` 后的实际请求和 Mock 绑定；
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
        "orders": [
          {"id": 10, "status": "pending"},
          {"id": 20, "status": "paid"}
        ],
        "order_items": [
          {"id": 100, "order_id": {"$ref": "orders[0].id"}, "sku": "S001"},
          {"id": 101, "order_id": {"$ref": "orders[0].id"}, "sku": "S002"},
          {"id": 102, "order_id": {"$ref": "orders[1].id"}, "sku": "S003"}
        ]
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
   多父多子时推荐用 `{"$ref": "orders[0].id"}` 引用同批行；只有一个候选
   父键时框架可自动补子表外键，多个候选且未显式引用会被拒绝。
   关联表要一起构造，不能只插主表、指望库里已有父行/子行。
5. **用例直接用造数数据**：请求里出现的业务键（主键、订单号、SKU、手机号等）
   必须在 fixtures 里有一行完全相同的值；不要插入另一组 ID 再让用例去碰运气，
   也不要假设环境里已有真实记录。`data` 实体里的 ID 就是插入行的 ID。
6. 同一张表可以输出多行；每行仍必须是完整、可类型校验的对象。父子多行
   必须保持插入拓扑和可回收主键，不要只返回一行样例。
7. `target_state` 只描述断言关心的期望数据；没有就省略或写 `{}`。
8. 不输出 SQL、`teardown`、`tables_order` 或 `fk_links`；这些由框架确定性生成。
9. 无法造出的状态不要硬编，写进 `gaps`，框架会把该用例标为 rejected/blocked。
10. 不引用 catalog 之外的表/列；不编造枚举值。
11. 下游服务的数据不靠插表或调接口构造：下游返回由 Mock 负责；这里只造
    本服务数据库里的数据。

---

## 铁律

1. 只输出 JSON object。
2. 不产出 SQL。
3. 不修改用例的请求、断言或规则归属。
4. 每个 fixture 值都必须能用 catalog 的列类型/枚举解释。
5. 不通过调用业务接口造数，也不依赖环境里的真实数据；所有前置数据都落到
   `fixtures`，由框架插入、用例直接使用。
6. 无法确定就留空并写 gap，不要猜。
