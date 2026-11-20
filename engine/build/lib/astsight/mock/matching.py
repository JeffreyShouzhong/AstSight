"""HTTP Mock 请求级匹配：query/header/body 的受限子集规则。"""
from __future__ import annotations

from typing import Any, Mapping

from contracts import MockRequestMatch


def request_matches(
    match: MockRequestMatch,
    *,
    query: Mapping[str, Any],
    headers: Mapping[str, str],
    body: Any,
) -> bool:
    if not _mapping_matches(match.query, query, normalize=str):
        return False
    normalized_headers = {
        str(key).lower(): str(value) for key, value in headers.items()
    }
    if not _mapping_matches(
        {key.lower(): value for key, value in match.headers.items()},
        normalized_headers,
        normalize=str,
    ):
        return False
    if match.body is not None:
        return _body_subset(match.body, body)
    return True


def _mapping_matches(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    normalize,
) -> bool:
    for key, value in expected.items():
        if key not in actual:
            return False
        candidate = actual[key]
        if isinstance(value, list):
            if not isinstance(candidate, list):
                candidate = [candidate]
            if [normalize(item) for item in value] != [
                normalize(item) for item in candidate
            ]:
                return False
        elif isinstance(candidate, list):
            if len(candidate) != 1 or normalize(candidate[0]) != normalize(value):
                return False
        elif normalize(candidate) != normalize(value):
            return False
    return True


def _body_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and _body_subset(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        return all(
            _body_subset(left, right)
            for left, right in zip(expected, actual, strict=True)
        )
    return expected == actual


__all__ = ["request_matches"]
