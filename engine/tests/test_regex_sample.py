"""正则样例生成：只覆盖常见 DTO 校验写法，复杂结构安全失败。"""
from __future__ import annotations

import re

import pytest

from astsight.cases.regexsample import sample_for_pattern


@pytest.mark.parametrize(
    "pattern,expected",
    [
        (r"^[A-Z]{2}$", "AA"),
        (r"^\d{3}-\d{4}$", "000-0000"),
        (r"^(yes|no)$", "yes"),
        (r"^[A-Z][a-z]+$", "Aa"),
        (r"^\w+@\w+\.com$", "a@a.com"),
        (r"^[a-z0-9]{4,8}$", "aaaa"),
        (r"^[^\d]{3}$", "aaa"),
        (r"^$", ""),
    ],
)
def test_common_patterns_generate_full_match(pattern: str, expected: str) -> None:
    sample = sample_for_pattern(pattern)
    assert sample == expected
    assert re.fullmatch(pattern, sample)


def test_lookahead_and_invalid_patterns_return_none() -> None:
    assert sample_for_pattern(r"^(?=a)a$") is None
    assert sample_for_pattern(r"[") is None
    assert sample_for_pattern("") is None
