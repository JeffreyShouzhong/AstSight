# C5a 数据目录语义补全 · 主提示词

> 输入是确定性的 `data_catalog` 骨架、规则库和需求原文。
> 你补的是**字段业务语义**和**可造业务态候选**，不是 SQL，也不是测试用例。
> 输出必须符合调用方给的 `schema`（`SemanticDraft`）。

---

## 输入

- `catalog.tables[]`：表、列、类型、可空、默认值、主键、枚举、注释；
  - `value_semantics` 首轮来自框架的**确定性候选**：DB 注释（`0=待支付`）、
    DBML `Enum` note、规则 `eq/in` 常量与代码枚举/常量组（Java/Python/Go/TS）折叠；
    证据固定为 `inferred`，`source` 标明来源（`comment`/`dbml`/`enum`/`rule`/
    `llm`/`code:<路径>`），带 `rule_keys` 的项说明有规则依据；
  - `purpose` 是表注释，`semantic_type` 已由命名/类型/注释预填。
- `catalog.foreign_keys[]`：外键关系；
- `catalog.seed_states[]`：seed 已提供的初始数据状态；`rule_*` 开头的状态是框架
  从规则跨表约束生成的骨架，`gaps` 里的 `state_placeholder` 值只是占位，不是业务事实，
  可以修正或补全，但不要当成已确认数据；
- `catalog.state_paths[]`：规则推导出的多步迁移路径；框架已把每条路径展开为
  `state_path_*` 的 DataState，每个迁移状态一行，并补齐父表/FK/NOT NULL。
  这些仍标 `inferred`，不要当成已确认的业务状态机，也不要重复生成同内容状态；
- `rules[]`：已导入的业务规则（含 `rule_key` / `rule_id` / `text`）；
- `documents[]`：需求原文 section；
- `schema`：输出 JSON Schema。

---

## 任务

1. 为有业务意义的列补 `field_semantics`：
   - `semantic_type`：`identifier` / `reference` / `quantity` / `amount` / `status` /
     `enum` / `timestamp` / `boolean` / `text` / `unknown`；
   - `value_semantics[]`：取值及其业务含义；框架已给出的候选可以修正或补全，
     但不要与 DB 枚举/CHECK 冲突；带 `rule_keys` 的候选优先保留其规则依据；
   - `business_notes`：一句话说明；
   - `evidence`：只有需求原文或规则明确支撑时才可写 `verified`，
     否则写 `inferred`；无法判断则不要输出该列（框架保持 `unmapped`）。
2. 设计 `states[]`：A4 可引用的业务状态，每项必须有 `state_key` 和可执行数据：
   - `table_values`：`{table: [row, ...]}`，只含 catalog 中存在的表/列；
   - 同一张表允许多行；父子业务态必须输出父表和全部子表行，不要只保留一行；
   - `states[].table_values` 中的子表外键直接写父表主键值，不使用 `$ref`；
   - `target_state`：可选，表示断言/后续步骤期望的状态；
   - `rule_keys`：该状态服务的规则；只引用输入中存在的 key；
   - FK 完整性：子表行引用父表时，父表行必须出现在同一个 state 的 `table_values` 中。
   - 路径展开状态已经包含逐状态多行；只有在文档能证明额外分支/行时才补充新状态。
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
        "orders": [
          {"id": 10, "status": "pending"},
          {"id": 20, "status": "paid"}
        ],
        "order_items": [
          {"id": 100, "order_id": 10, "sku": "S001"},
          {"id": 101, "order_id": 20, "sku": "S003"}
        ],
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
