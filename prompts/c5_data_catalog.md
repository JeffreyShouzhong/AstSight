# C5a 数据目录语义补全 · 主提示词

> 输入是确定性的 `data_catalog` 骨架、规则库和需求原文。
> 你补的是**字段业务语义**和**可造业务态候选**，不是 SQL，也不是测试用例。
> 输出必须符合调用方给的 `schema`（`SemanticDraft`）。

---

## 输入

- `catalog.tables[]`：表、列、类型、可空、默认值、主键、枚举、注释；
- `catalog.foreign_keys[]`：外键关系；
- `catalog.seed_states[]`：seed 已提供的初始数据状态；
- `rules[]`：已导入的业务规则（含 `rule_key` / `rule_id` / `text`）；
- `documents[]`：需求原文 section；
- `schema`：输出 JSON Schema。

---

## 任务

1. 为有业务意义的列补 `field_semantics`：
   - `semantic_type`：`identifier` / `reference` / `quantity` / `amount` / `status` /
     `enum` / `timestamp` / `boolean` / `text` / `unknown`；
   - `value_semantics[]`：取值及其业务含义；
   - `business_notes`：一句话说明；
   - `evidence`：只有需求原文或规则明确支撑时才可写 `verified`，
     否则写 `inferred`；无法判断则不要输出该列（框架保持 `unmapped`）。
2. 设计 `states[]`：A4 可引用的业务状态，每项必须有 `state_key` 和可执行数据：
   - `table_values`：`{table: [row, ...]}`，只含 catalog 中存在的表/列；
   - `target_state`：可选，表示断言/后续步骤期望的状态；
   - `rule_keys`：该状态服务的规则；只引用输入中存在的 key；
   - FK 完整性：子表行引用父表时，父表行必须出现在同一个 state 的 `table_values` 中。
3. 无法确定的内容放 `gaps` / `questions`，每项要有原因。

---

## 输出示例

```json
{
  "schema_version": 1,
  "field_semantics": [
    {
      "table": "products",
      "column": "stock",
      "semantic_type": "quantity",
      "value_semantics": [
        {"value": 0, "meaning": "无库存", "evidence": "inferred",
         "rule_keys": ["RK-0123456789ab"]}
      ],
      "business_notes": "可售库存；0 表示不可下单",
      "evidence": "inferred"
    }
  ],
  "states": [
    {
      "state_key": "state_stock_empty",
      "table_values": {
        "products": [{"sku_id": "S001", "stock": 0, "status": "active"}]
      },
      "target_state": {"products": {"stock": 0}},
      "evidence": "inferred",
      "rule_keys": ["RK-0123456789ab"]
    }
  ],
  "gaps": [
    {"kind": "unmapped_enum", "desc": "orders.status 的取值含义未在文档中定义",
     "reason": "DDL 无 COMMENT，需求也未给出状态机"}
  ],
  "questions": [
    {"text": "stock 是否允许为负？", "reason": "DDL 未加 CHECK 约束"}
  ]
}
```

---

## 铁律

1. 只能引用 catalog 中已有的表、列、枚举值；未知项会被整体拒绝。
2. 不得输出 SQL、DDL 或迁移语句。
3. 不得把 seed 之外的值当成既有数据；新值只能出现在 `table_values` 的候选态中。
4. `verified` 必须有需求/规则依据；否则用 `inferred`。
5. 不臆造状态机或枚举；无法确定就写 gap。
6. 只输出 JSON object。
