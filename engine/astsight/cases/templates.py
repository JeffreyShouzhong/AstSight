"""C4 确定性模板用例生成（M1）。

生成四类用例，全部由画像/契约驱动，不做业务联想（LLM 留给 M2）：
- positive         所有必填项取合法样例，断言 2xx + 响应字段存在
- missing_required 逐个去掉必填项，断言 4xx
- type_error       类型错配（integer/number/boolean），断言 4xx
- boundary         数值/长度/枚举的越界值，断言 4xx

边界：单接口最多 `max_per_endpoint` 条（默认 8），避免笛卡尔积爆炸。
断言强度一律 L1（状态码/字段存在）——模板不假装能判业务对错。
"""
from __future__ import annotations

import copy
from typing import Any

from contracts import Assertion, AssertStrength, TestCase

from ..profile.openapi import sample_value

_NEGATIVE_STATUS = {"kind": "status_code", "op": "between", "expect": [400, 499]}
_MAX_BOUNDARY = 2


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
    """正向用例断言：成功状态码 + 契约声明的响应字段存在。"""
    success = profile.get("success_codes") or [200]
    asserts = [Assertion(**{"kind": "status_code", "op": "in", "expect": success})]
    fields_for_code = (profile.get("response_fields") or {}).get(str(success[0]), [])
    for field_name in fields_for_code[:5]:
        asserts.append(
            Assertion(kind="body_field", target=field_name, op="exists", expect=None)
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

        cases.extend(endpoint_cases[:max_per_endpoint])

    return cases
