# C3.5.1 Mock 语义补全 · 主提示词

> 输入是确定性生成的 Mock 语义骨架（dep + 默认 success + 规则映射分支）。
> 你只补缺失的响应骨架/业务分支；不新增下游，不生成测试用例。
> 输出必须符合调用方提供的 `schema`（`MockSemanticDraft`）。

---

## 输入

- `profiles[]`：接口画像及其 `downstream_deps`；
- `deps[]`：已确定的 `dep_id`、`endpoint`、现有 `branches`；
- `rules[]`：可引用的业务规则；
- `schema`：JSON Schema。

---

## 任务

对每个 `dep_id`：

1. 如果 `success` 分支的 `stub.body` 为空，根据契约/用例/规则给出一个**最小合法响应体**；
2. 如果规则描述了错误/降级/超时分支，可新增分支：
   - `branch` 用稳定小写标识（如 `rule_BR-02` 已由框架生成，不要重名）；
   - `stub.status` 用 4xx/5xx；
   - `source.rule_keys` 必须引用输入规则；
   - `evidence` 只能是 `inferred`（`verified` 会被降级）。
3. 无法确定的内容写进 `gaps`，每项带 `dep_id` 和 `reason`。

---

## 输出示例

```json
{
  "deps": [
    {
      "dep_id": "http:call:app.py:41:25 GET /v1/pricing",
      "branches": [
        {
          "branch": "success",
          "semantics": "价格服务按 sku 返回单价",
          "stub": {"status": 200, "body": {"unit_price_cents": 2500}},
          "evidence": "inferred",
          "source": {"kind": "rule", "rule_keys": ["RK-0123456789ab"]}
        }
      ]
    }
  ],
  "gaps": [
    {"dep_id": "http:call:...", "reason": "响应字段结构未在契约中定义"}
  ],
  "questions": []
}
```

---

## 铁律

1. 只能使用输入中的 `dep_id`。
2. 只输出 JSON object，不输出代码或测试用例。
3. 不编造规则 key；无法引用规则时不要生成错误分支。
4. 不生成没有依据的业务分支；缺依据就写 gap。
5. success 的响应体应尽量小，但必须能被被测代码正常解析。
