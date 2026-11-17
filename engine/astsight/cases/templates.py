"""C4 确定性模板用例生成（M1）。

生成四类用例，全部由画像/契约驱动，不做业务联想（LLM 留给 M2）：
- positive         所有必填项取合法样例，断言 2xx + 响应字段存在
- missing_required 逐个去掉必填项，断言 4xx
- type_error       类型错配（integer/number/boolean），断言 4xx
- boundary         数值/长度/枚举的越界值，断言 4xx

边界：单接口最多 `max_per_endpoint` 条（默认 8），避免笛卡尔积爆炸。
断言强度由实际断言内容推导（状态码/字段存在为 L1，确定值为 L2）；
模板不假装能判业务对错。
"""
from __future__ import annotations

import copy
from typing import Any

from contracts import Assertion, AssertStrength, DubboCall, TestCase, Transport

from ..profile.openapi import sample_value
from .regexsample import sample_for_pattern
from .strength import derive_assert_strength

_NEGATIVE_STATUS = {"kind": "status_code", "op": "between", "expect": [400, 499]}
_MAX_BOUNDARY = 2
_MAX_COLLECTION_ITEMS = 10


def _sample_from_param(param: dict) -> Any:
    schema = dict(param.get("schema") or {})
    if param.get("example") is not None:
        schema["example"] = param["example"]
    if not schema:
        schema = {"type": param.get("type", "string"), **(param.get("constraints") or {})}
    return sample_value(schema, {})


def _split_params(params: list[dict]) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    path_params: dict[str, Any] = {}
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}
    for param in params:
        name = param.get("name", "")
        location = param.get("in", "query")
        if location == "path":
            path_params[name] = _sample_from_param(param)
        elif location == "query" and param.get("required"):
            query[name] = _sample_from_param(param)
        elif location == "header" and param.get("required"):
            headers[name] = str(_sample_from_param(param))
    return path_params, query, headers


def _body_from_schema(schema: dict, seed: str, only_required: bool = True) -> dict:
    properties = schema.get("properties") or {}
    required = schema.get("required")
    names = list(required) if required else ([] if only_required else list(properties))
    body: dict[str, Any] = {}
    for name in names:
        prop = properties.get(name) or {}
        body[name] = sample_value(prop, schema, f"{seed}_{name}")
    return body


def success_asserts(profile: dict) -> list[Assertion]:
    """正向用例断言：成功状态码 + 响应字段存在 + schema/Content-Type。"""
    success = profile.get("success_codes") or [200]
    first = str(success[0])
    asserts = [Assertion(**{"kind": "status_code", "op": "in", "expect": success})]
    fields_for_code = (profile.get("response_fields") or {}).get(first, [])
    for field_name in fields_for_code[:5]:
        asserts.append(
            Assertion(kind="body_field", target=field_name, op="exists", expect=None)
        )
    schema = (profile.get("response_schemas") or {}).get(first)
    if isinstance(schema, dict):
        asserts.append(
            Assertion(
                kind="body_schema", target=first, op="matches", expect=schema
            )
        )
    content_type = str(profile.get("response_content_type") or "")
    if content_type:
        asserts.append(
            Assertion(
                kind="header",
                target="content-type",
                op="contains",
                expect=content_type.split(";")[0].strip(),
            )
        )
    return asserts


def base_request(profile: dict) -> dict:
    """按画像构造一份合法基础请求，供模板用例复用。"""
    params = profile.get("params") or []
    path_params, query, headers = _split_params(params)
    body_schema = profile.get("request_body") or {}
    return {
        "path_params": path_params,
        "query": query,
        "headers": headers,
        "body": _body_from_schema(body_schema, "POS") if body_schema else None,
    }


def field_location(profile: dict, field_name: str) -> str | None:
    """字段在请求里的位置：body / path / query / header。"""
    body = profile.get("request_body") or {}
    if field_name in (body.get("properties") or {}):
        return "body"
    for param in profile.get("params") or []:
        if param.get("name") == field_name:
            location = param.get("in", "query")
            return location if location in {"path", "query", "header"} else "query"
    return None


def field_schema(profile: dict, field_name: str) -> dict:
    body = profile.get("request_body") or {}
    schema = (body.get("properties") or {}).get(field_name)
    if isinstance(schema, dict):
        return schema
    for param in profile.get("params") or []:
        if param.get("name") == field_name:
            return dict(param.get("schema") or {})
    return {}


# 向后兼容旧名字（M1 内部使用）。
_positive_asserts = success_asserts


def _negative_case(
    profile: dict, case_id: str, category: str, *,
    path_params: dict, query: dict, headers: dict, body: Any, note: str,
) -> TestCase:
    return TestCase(
        id=case_id,
        endpoint=profile["endpoint"],
        category=category,
        method=profile["method"],
        path=profile["path"],
        path_params=path_params,
        query=query,
        headers=headers,
        body=body,
        asserts=[Assertion(**_NEGATIVE_STATUS)],
        source=f"contract:{profile['endpoint']}",
        assert_strength=AssertStrength.L1,
        note=note,
    )


_DUBBO_SCALARS = {
    "java.lang.String": "sample",
    "java.lang.CharSequence": "sample",
    "java.util.UUID": "00000000-0000-0000-0000-000000000000",
    "java.net.URI": "https://example.com",
    "java.net.URL": "https://example.com",
    "int": 1,
    "java.lang.Integer": 1,
    "short": 1,
    "java.lang.Short": 1,
    "byte": 1,
    "java.lang.Byte": 1,
    "long": 1,
    "java.lang.Long": 1,
    "float": 1.0,
    "java.lang.Float": 1.0,
    "double": 1.0,
    "java.lang.Double": 1.0,
    "boolean": True,
    "java.lang.Boolean": True,
    "char": "a",
    "java.lang.Character": "a",
    "java.math.BigDecimal": 1,
    "java.math.BigInteger": 1,
}
_DUBBO_COLLECTIONS = (
    "java.util.List",
    "java.util.Set",
    "java.util.Collection",
    "java.util.Map",
    "java.util.HashMap",
)


def _dubbo_sample_arg(type_name: str) -> Any:
    if type_name in _DUBBO_SCALARS:
        return _DUBBO_SCALARS[type_name]
    if type_name.endswith("[]"):
        return []
    if type_name.startswith("java.util.Map") or type_name.startswith("java.util.HashMap"):
        return {}
    if type_name.startswith(_DUBBO_COLLECTIONS[:3]):
        return []
    return {}


_TEMPORAL_SAMPLES = {
    "date": "2024-01-01",
    "time": "00:00:00",
    "datetime": "2024-01-01T00:00:00",
    "instant": "2024-01-01T00:00:00Z",
    "duration": "PT1H",
    "period": "P1D",
}


def _constraint_number(constraints: dict, key: str) -> Any:
    value = constraints.get(key)
    return value if isinstance(value, (int, float)) else None


def _dubbo_scalar_sample(type_name: str, constraints: dict) -> Any:
    allowed = constraints.get("allowed_values")
    if isinstance(allowed, list) and allowed:
        return allowed[0]
    if type_name in {"java.lang.String", "java.lang.CharSequence"}:
        pattern = constraints.get("pattern")
        if pattern:
            generated = sample_for_pattern(str(pattern))
            if generated is not None:
                return generated
        format_name = str(constraints.get("format") or "")
        value = {
            "email": "test@example.com",
            "phone": "13800138000",
            "mobile": "13800138000",
            "url": "https://example.com",
            "uri": "https://example.com",
            "date": "2024-01-01",
            "datetime": "2024-01-01T00:00:00",
            "id_card": "110101199001011234",
        }.get(format_name, "sample")
        min_length = _constraint_number(constraints, "min_length")
        max_length = _constraint_number(constraints, "max_length")
        if min_length is not None and len(value) < min_length:
            value = value.ljust(int(min_length), "s")
        if max_length is not None and len(value) > max_length:
            value = value[: int(max_length)]
        return value
    base = _DUBBO_SCALARS.get(type_name)
    if isinstance(base, bool) or not isinstance(base, (int, float)):
        return base
    minimum = _constraint_number(constraints, "minimum")
    maximum = _constraint_number(constraints, "maximum")
    value = base
    if minimum is not None:
        value = minimum + (1 if constraints.get("minimum_exclusive") else 0)
    elif maximum is not None:
        value = maximum - (1 if constraints.get("maximum_exclusive") else 0)
    if maximum is not None and value > maximum:
        value = maximum
    if minimum is not None and value < minimum:
        value = minimum
    if isinstance(base, int) and not isinstance(base, bool):
        return int(value)
    return value


def _collection_count(constraints: dict) -> int:
    max_length = _constraint_number(constraints, "max_length")
    if max_length is not None and max_length <= 0:
        return 0
    min_length = _constraint_number(constraints, "min_length")
    count = 1 if min_length is None else max(1, int(min_length))
    count = min(count, _MAX_COLLECTION_ITEMS)
    if max_length is not None:
        count = min(count, int(max_length))
    return max(0, count)


def _dubbo_sample_schema(schema: dict, depth: int = 0) -> Any:
    kind = str(schema.get("kind") or "")
    type_name = str(schema.get("type") or "")
    constraints = schema.get("constraints") or {}
    if kind == "scalar":
        return _dubbo_scalar_sample(type_name, constraints)
    if kind == "temporal":
        return _TEMPORAL_SAMPLES.get(str(schema.get("format") or ""), "2024-01-01T00:00:00")
    if kind == "enum":
        values = schema.get("enum_values") or []
        return values[0] if values else ""
    if kind == "dto":
        if depth >= 4:
            return {}
        body: dict[str, Any] = {}
        for field in (schema.get("fields") or [])[:20]:
            field_schema = dict(field.get("schema") or {})
            constraints = field.get("constraints") or {}
            if constraints:
                field_schema["constraints"] = {
                    **(field_schema.get("constraints") or {}),
                    **constraints,
                }
            body[str(field.get("name"))] = _dubbo_sample_schema(
                field_schema, depth + 1
            )
        return body
    if kind in {"collection", "array"}:
        element = schema.get("element")
        if not isinstance(element, dict):
            return []
        return [
            _dubbo_sample_schema(element, depth + 1)
            for _ in range(_collection_count(constraints))
        ]
    if kind == "map":
        key_schema = schema.get("key")
        value_schema = schema.get("value")
        key = (
            _dubbo_sample_schema(key_schema, depth + 1)
            if isinstance(key_schema, dict)
            else "key"
        )
        value = (
            _dubbo_sample_schema(value_schema, depth + 1)
            if isinstance(value_schema, dict)
            else None
        )
        return {str(key): value}
    if kind in {"opaque", "unknown", "reference"}:
        return {}
    return None


def _constraint_notes(constraints: dict, label: str) -> list[str]:
    notes: list[str] = []
    pattern = constraints.get("pattern")
    if pattern:
        sample = sample_for_pattern(str(pattern))
        if sample is None:
            notes.append(f"{label} 的 pattern 无法自动生成匹配值")
        else:
            min_length = _constraint_number(constraints, "min_length")
            max_length = _constraint_number(constraints, "max_length")
            if (
                min_length is not None
                and len(sample) < min_length
                or max_length is not None
                and len(sample) > max_length
            ):
                notes.append(f"{label} 的 pattern 样例长度与长度约束冲突")
    custom = constraints.get("custom") or {}
    if custom:
        inferred = any(
            key in constraints
            for key in (
                "min_length",
                "max_length",
                "minimum",
                "maximum",
                "pattern",
                "format",
                "allowed_values",
            )
        )
        if not inferred:
            names = ",".join(str(name) for name in custom)
            notes.append(f"{label} 的自定义约束 {names} 需人工确认")
    return notes


def _schema_notes(schema: dict, prefix: str = "") -> list[str]:
    if not isinstance(schema, dict):
        return []
    kind = schema.get("kind")
    type_name = str(schema.get("type") or "")
    label = (prefix or type_name or "参数").rstrip(".")
    notes: list[str] = []
    if kind == "opaque":
        notes.append(f"无法静态采样 {type_name}，按空对象发送")
    elif kind == "unknown":
        notes.append(f"无法解析类型 {type_name or '?'}")
    constraints = schema.get("constraints") or {}
    notes.extend(_constraint_notes(constraints, label))
    if kind in {"collection", "array"}:
        min_length = _constraint_number(constraints, "min_length")
        if min_length is not None and min_length > _MAX_COLLECTION_ITEMS:
            notes.append(
                f"{label} 的集合最少 {min_length} 个元素，"
                f"模板最多生成 {_MAX_COLLECTION_ITEMS} 个"
            )
    for field in schema.get("fields") or []:
        name = str(field.get("name") or "")
        field_label = f"{prefix}{name}" if prefix else name
        field_constraints = field.get("constraints") or {}
        field_schema = dict(field.get("schema") or {})
        if field_constraints:
            field_schema["constraints"] = {
                **(field_schema.get("constraints") or {}),
                **field_constraints,
            }
        notes.extend(_schema_notes(field_schema, f"{field_label}."))
    element = schema.get("element")
    if isinstance(element, dict):
        notes.extend(_schema_notes(element, prefix))
    for key in ("key", "value"):
        nested = schema.get(key)
        if isinstance(nested, dict):
            notes.extend(_schema_notes(nested, prefix))
    return notes


def _dubbo_case(profile: dict, case_id: str) -> TestCase:
    types = list(profile.get("parameter_types") or [])
    schemas = list(profile.get("parameter_schemas") or [])
    notes: list[str] = []
    if schemas:
        arguments = [_dubbo_sample_schema(schema) for schema in schemas]
        for schema in schemas:
            notes.extend(_schema_notes(schema))
    else:
        arguments = [_dubbo_sample_arg(type_name) for type_name in types]
        complex_args = [
            type_name
            for type_name in types
            if type_name not in _DUBBO_SCALARS
            and not type_name.endswith("[]")
            and not type_name.startswith(_DUBBO_COLLECTIONS)
        ]
        if complex_args:
            notes.append(
                "无法静态采样的参数类型，字段值需人工补充: "
                + ", ".join(complex_args)
            )
    asserts = [Assertion(kind="rpc_status", op="eq", expect="OK")]
    return_type = str(profile.get("return_type") or "")
    if return_type and return_type not in {"void", "java.lang.Void"}:
        asserts.append(Assertion(kind="result_present", op="exists"))
    note = "；".join(dict.fromkeys(notes)) if notes else None
    return TestCase(
        id=case_id,
        title=f"{profile.get('service')}.{profile.get('method')}",
        transport=Transport.DUBBO,
        endpoint=profile["endpoint"],
        category="positive",
        method=str(profile.get("method") or ""),
        path="",
        asserts=asserts,
        source=f"dubbo:{profile['endpoint']}",
        assert_strength=AssertStrength.L1,
        note=note,
        dubbo=DubboCall(
            interface=str(profile.get("service") or ""),
            method=str(profile.get("method") or ""),
            parameter_types=types,
            arguments=arguments,
            url=profile.get("direct_url"),
            group=profile.get("group") or None,
            version=profile.get("version") or None,
            target_port=profile.get("protocol_port"),
        ),
    )


def generate_cases(
    profiles: list[dict],
    *,
    max_per_endpoint: int = 8,
    start_index: int = 1,
) -> list[TestCase]:
    """生成契约模板用例。

    `start_index` 用于批次内保持编号连续（默认从 TC-0001 开始）。
    """
    cases: list[TestCase] = []
    counter = max(0, start_index - 1)

    def next_id() -> str:
        nonlocal counter
        counter += 1
        return f"TC-{counter:04d}"

    for profile in profiles:
        transport = str(profile.get("transport", "http")).lower()
        if transport == "dubbo":
            cases.append(_dubbo_case(profile, next_id()))
            continue
        if transport != "http":
            # gRPC 等其他入口还没有模板用例与执行链路；保留画像，
            # 但不生成伪装成 HTTP 的假用例。
            continue
        if profile.get("contract_incomplete"):
            # 没有契约就不生成——"未生成"必须显式出现在报告的 unmapped 区，
            # 而不是用一条只会打 200 的假用例把缺口盖住。
            continue
        if profile["method"] in ("HEAD", "OPTIONS"):
            continue

        base = base_request(profile)
        path_params, query, headers = base["path_params"], base["query"], base["headers"]
        body_schema = profile.get("request_body") or {}
        required_body = list(profile.get("body_required") or [])
        positive_body = base["body"]

        endpoint_cases: list[TestCase] = []
        endpoint_cases.append(
            TestCase(
                id=next_id(),
                endpoint=profile["endpoint"],
                category="positive",
                method=profile["method"],
                path=profile["path"],
                path_params=dict(path_params),
                query=dict(query),
                headers=dict(headers),
                body=positive_body,
                asserts=_positive_asserts(profile),
                source=f"openapi:{profile['endpoint']}",
                assert_strength=AssertStrength.L1,
            )
        )

        # 缺必填（body 最多 2 个、required query 最多 1 个）
        for field_name in required_body[:2]:
            body = copy.deepcopy(positive_body) or {}
            body.pop(field_name, None)
            endpoint_cases.append(
                _negative_case(
                    profile, next_id(), "missing_required",
                    path_params=path_params, query=query, headers=headers, body=body,
                    note=f"缺少必填字段 {field_name}",
                )
            )
        for name in list(query)[:1]:
            reduced = {k: v for k, v in query.items() if k != name}
            endpoint_cases.append(
                _negative_case(
                    profile, next_id(), "missing_required",
                    path_params=path_params, query=reduced, headers=headers, body=positive_body,
                    note=f"缺少必填查询参数 {name}",
                )
            )

        # 类型错配（挑一个数值/布尔字段）
        properties = body_schema.get("properties") or {}
        for field_name in required_body:
            prop = properties.get(field_name) or {}
            if prop.get("type") in ("integer", "number", "boolean"):
                body = copy.deepcopy(positive_body) or {}
                body[field_name] = "not-a-valid-value"
                endpoint_cases.append(
                    _negative_case(
                        profile, next_id(), "type_error",
                        path_params=path_params, query=query, headers=headers, body=body,
                        note=f"{field_name} 类型错配",
                    )
                )
                break

        # 边界：数值 min/max、字符串长度、枚举
        boundary_made = 0
        for field_name in required_body:
            if boundary_made >= _MAX_BOUNDARY:
                break
            prop = properties.get(field_name) or {}
            if prop.get("enum"):
                body = copy.deepcopy(positive_body) or {}
                body[field_name] = "ASTSIGHT_INVALID_ENUM"
                endpoint_cases.append(
                    _negative_case(
                        profile, next_id(), "boundary",
                        path_params=path_params, query=query, headers=headers, body=body,
                        note=f"{field_name} 枚举越界",
                    )
                )
                boundary_made += 1
                continue
            if prop.get("type") in ("integer", "number"):
                for key, delta in (("minimum", -1), ("maximum", +1)):
                    if boundary_made >= _MAX_BOUNDARY or key not in prop:
                        continue
                    value = prop[key] + delta
                    body = copy.deepcopy(positive_body) or {}
                    body[field_name] = value
                    endpoint_cases.append(
                        _negative_case(
                            profile, next_id(), "boundary",
                            path_params=path_params, query=query, headers=headers, body=body,
                            note=f"{field_name}={value} 越过 {key}",
                        )
                    )
                    boundary_made += 1
            elif prop.get("type") == "string" and prop.get("maxLength"):
                body = copy.deepcopy(positive_body) or {}
                body[field_name] = "T" * (int(prop["maxLength"]) + 1)
                endpoint_cases.append(
                    _negative_case(
                        profile, next_id(), "boundary",
                        path_params=path_params, query=query, headers=headers, body=body,
                        note=f"{field_name} 超过 maxLength",
                    )
                )
                boundary_made += 1

        for case in endpoint_cases:
            case.assert_strength = derive_assert_strength(
                case.asserts, steps=1
            )
        cases.extend(endpoint_cases[:max_per_endpoint])

    return cases
