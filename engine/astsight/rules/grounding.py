"""规则值的确定性 grounding。

模型经常把 section 其他段落的值拼进当前 quote。这里只做机械检查：

- 值在 quote 中：已落地；
- 值只在 section 其他位置：证据不足，规则应降级/标 ungrounded；
- 值在 section 中完全不存在：视为编造，直接拒绝。

检查对象是规则文本中的数字、反引号内容、错误码，以及结构化约束的
`values` / `error_code`。语义解释是否正确不在这里判断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from contracts import RuleConstraint

from ..cases.requirements import normalize_text

_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
_BACKTICK_RE = re.compile(r"`([^`]{1,120})`")
_ERROR_CODE_RE = re.compile(
    r"\b(?:[A-Z]{1,8}[-_]?\d{3,}|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b"
)
_TRANSITION_SPLIT_RE = re.compile(r"\s*(?:->|→)\s*")


@dataclass
class GroundingResult:
    """一次 grounding 的机械分类结果。"""

    tokens: list[str] = field(default_factory=list)
    grounded: list[str] = field(default_factory=list)
    section_only: list[str] = field(default_factory=list)
    fabricated: list[str] = field(default_factory=list)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        text = normalize_text(value)
        if text:
            seen.setdefault(text, None)
    return list(seen)


def _text_tokens(text: str) -> list[str]:
    text = text or ""
    return [
        *_BACKTICK_RE.findall(text),
        *_NUMBER_RE.findall(text),
        *_ERROR_CODE_RE.findall(text),
    ]


def _constraint_tokens(constraint: RuleConstraint) -> list[str]:
    tokens = _text_tokens(constraint.condition)
    for value in constraint.values:
        raw = str(value)
        parts = _TRANSITION_SPLIT_RE.split(raw)
        tokens.extend(part for part in parts if part)
    if constraint.error_code:
        tokens.append(constraint.error_code)
    return tokens


def rule_value_tokens(
    text: str,
    constraints: Iterable[RuleConstraint] | None = None,
) -> list[str]:
    """提取规则文本/结构化约束中所有必须可核对的值。"""
    tokens = _text_tokens(text)
    for constraint in constraints or []:
        tokens.extend(_constraint_tokens(constraint))
    return _dedupe(tokens)


def classify_grounding(
    tokens: Iterable[str],
    quote: str,
    section_text: str,
) -> GroundingResult:
    quote_norm = normalize_text(quote)
    section_norm = normalize_text(section_text)
    result = GroundingResult()
    for token in _dedupe(tokens):
        result.tokens.append(token)
        if token in quote_norm:
            result.grounded.append(token)
        elif token in section_norm:
            result.section_only.append(token)
        else:
            result.fabricated.append(token)
    return result


__all__ = [
    "GroundingResult",
    "classify_grounding",
    "rule_value_tokens",
]
