"""用例级业务实体引用。

用例用 ``{"$data": "product.id"}`` 引用 `data` 里的实体字段；请求参数、
请求体和下游 Mock 响应补充都引用同一份数据，避免两处各写各的 ID。
"""
from __future__ import annotations

import copy
from typing import Any, Iterator

DATA_REF = "$data"


class DataRefError(ValueError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def is_data_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {DATA_REF}
        and isinstance(value[DATA_REF], str)
    )


def iter_data_refs(value: Any) -> Iterator[str]:
    """按出现顺序收集所有 ``$data`` 引用路径。"""
    if is_data_ref(value):
        yield value[DATA_REF]
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_data_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_data_refs(item)


def lookup_data(data: dict[str, Any], path: str) -> tuple[bool, Any]:
    """按点号路径取值；支持列表下标，找不到返回 (False, None)。"""
    current: Any = data
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return False, None
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, copy.deepcopy(current)


def resolve_data_refs(value: Any, data: dict[str, Any]) -> Any:
    """递归替换 ``$data`` 引用；未知路径抛 DataRefError。"""
    if is_data_ref(value):
        path = value[DATA_REF]
        found, resolved = lookup_data(data, path)
        if not found:
            raise DataRefError(path, "引用的业务实体字段不存在")
        return resolved
    if isinstance(value, dict):
        return {
            key: resolve_data_refs(item, data) for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve_data_refs(item, data) for item in value]
    return copy.deepcopy(value)


def _resolve_assertion(assertion: Any, data: dict[str, Any]) -> Any:
    return assertion.model_copy(
        update={
            "expect": resolve_data_refs(assertion.expect, data),
            "where": resolve_data_refs(assertion.where, data),
        }
    )


def _resolve_call(call: Any, data: dict[str, Any]) -> Any:
    if call is None:
        return None
    return call.model_copy(
        update={
            "arguments": resolve_data_refs(call.arguments, data),
            "attachments": resolve_data_refs(call.attachments, data),
        }
    )


def _resolve_step(step: Any, data: dict[str, Any]) -> Any:
    return step.model_copy(
        update={
            "path_params": resolve_data_refs(step.path_params, data),
            "query": resolve_data_refs(step.query, data),
            "body": resolve_data_refs(step.body, data),
            "dubbo": _resolve_call(step.dubbo, data),
            "asserts": [
                _resolve_assertion(assertion, data)
                for assertion in step.asserts
            ],
        }
    )


def resolve_case_data(case: Any) -> Any:
    """把用例里的 `$data` 引用换成实体值。

    只依赖字段名，不依赖具体模型；TestCase 与 DraftCase 都可传入。
    返回新对象，不改原用例。
    """
    data = getattr(case, "data", None) or {}
    if not data:
        return case
    return case.model_copy(
        update={
            "path_params": resolve_data_refs(case.path_params, data),
            "query": resolve_data_refs(case.query, data),
            "body": resolve_data_refs(case.body, data),
            "dubbo": _resolve_call(case.dubbo, data),
            "asserts": [
                _resolve_assertion(assertion, data)
                for assertion in case.asserts
            ],
            "steps": [_resolve_step(step, data) for step in case.steps],
            "mock_replies": resolve_data_refs(case.mock_replies, data),
            "preconditions": [
                item.model_copy(
                    update={
                        "where": resolve_data_refs(item.where, data),
                        "set": resolve_data_refs(item.set, data),
                    }
                )
                for item in case.preconditions
            ],
        }
    )


__all__ = [
    "DATA_REF",
    "DataRefError",
    "is_data_ref",
    "iter_data_refs",
    "lookup_data",
    "resolve_case_data",
    "resolve_data_refs",
]
