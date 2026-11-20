"""从常见正则生成一个可匹配的样例值。

只服务 DTO 采样，不追求完整正则引擎：支持字面量、字符类、范围、分类、
分组、分支、常见量词与锚点；lookaround、反向引用等复杂结构返回 None，
由调用方回退并提示人工确认。

实现基于 CPython 的 `re._parser`；不可用时整体禁用，不影响主流程。
"""
from __future__ import annotations

import re

try:  # CPython 私有 API；缺失时降级为“无法生成”
    from re import _constants as _sre
    from re import _parser as _parser
except Exception:  # pragma: no cover - 非 CPython 环境
    _sre = None  # type: ignore
    _parser = None  # type: ignore

_MAX_REPEAT = 32
_NEGATED_CANDIDATES = "aA0_- Z9z"


def _category_char(category) -> str | None:
    if category in {
        _sre.CATEGORY_DIGIT,
        _sre.CATEGORY_UNI_DIGIT,
    }:
        return "0"
    if category in {
        _sre.CATEGORY_WORD,
        _sre.CATEGORY_UNI_WORD,
        _sre.CATEGORY_LOC_WORD,
    }:
        return "a"
    if category in {
        _sre.CATEGORY_SPACE,
        _sre.CATEGORY_UNI_SPACE,
    }:
        return " "
    if category in {
        _sre.CATEGORY_LINEBREAK,
        _sre.CATEGORY_UNI_LINEBREAK,
    }:
        return "\n"
    if category in {
        _sre.CATEGORY_NOT_DIGIT,
        _sre.CATEGORY_UNI_NOT_DIGIT,
    }:
        return "a"
    if category in {
        _sre.CATEGORY_NOT_WORD,
        _sre.CATEGORY_UNI_NOT_WORD,
        _sre.CATEGORY_LOC_NOT_WORD,
    }:
        return "-"
    if category in {
        _sre.CATEGORY_NOT_SPACE,
        _sre.CATEGORY_UNI_NOT_SPACE,
    }:
        return "a"
    if category in {
        _sre.CATEGORY_NOT_LINEBREAK,
        _sre.CATEGORY_UNI_NOT_LINEBREAK,
    }:
        return "a"
    return None


def _char_matches(items, char: str) -> bool:
    value = ord(char)
    for op, arg in items:
        if op is _sre.LITERAL and value == arg:
            return True
        if op is _sre.RANGE and arg[0] <= value <= arg[1]:
            return True
        if op is _sre.CATEGORY:
            candidate = _category_char(arg)
            if candidate is None:
                return True
            # 分类匹配按候选字符的代表性判断；未知分类按宽松处理。
            if arg in {
                _sre.CATEGORY_DIGIT,
                _sre.CATEGORY_UNI_DIGIT,
            }:
                if char.isdigit():
                    return True
            elif arg in {
                _sre.CATEGORY_WORD,
                _sre.CATEGORY_UNI_WORD,
                _sre.CATEGORY_LOC_WORD,
            }:
                if char.isalnum() or char == "_":
                    return True
            elif arg in {
                _sre.CATEGORY_SPACE,
                _sre.CATEGORY_UNI_SPACE,
            }:
                if char.isspace():
                    return True
            elif arg in {
                _sre.CATEGORY_LINEBREAK,
                _sre.CATEGORY_UNI_LINEBREAK,
            }:
                if char in "\n\r":
                    return True
            elif arg in {
                _sre.CATEGORY_NOT_DIGIT,
                _sre.CATEGORY_UNI_NOT_DIGIT,
            }:
                if not char.isdigit():
                    return True
            elif arg in {
                _sre.CATEGORY_NOT_WORD,
                _sre.CATEGORY_UNI_NOT_WORD,
                _sre.CATEGORY_LOC_NOT_WORD,
            }:
                if not (char.isalnum() or char == "_"):
                    return True
            elif arg in {
                _sre.CATEGORY_NOT_SPACE,
                _sre.CATEGORY_UNI_NOT_SPACE,
            }:
                if not char.isspace():
                    return True
            elif arg in {
                _sre.CATEGORY_NOT_LINEBREAK,
                _sre.CATEGORY_UNI_NOT_LINEBREAK,
            }:
                if char not in "\n\r":
                    return True
    return False


def _class_char(items) -> str | None:
    negate = False
    real_items = []
    for op, arg in items:
        if op is _sre.NEGATE:
            negate = True
            continue
        real_items.append((op, arg))
    candidates = _NEGATED_CANDIDATES if negate else ""
    if negate:
        for candidate in candidates:
            if not _char_matches(real_items, candidate):
                return candidate
        return None
    for op, arg in real_items:
        if op is _sre.LITERAL:
            return chr(arg)
        if op is _sre.RANGE:
            return chr(arg[0])
        if op is _sre.CATEGORY:
            value = _category_char(arg)
            if value is not None:
                return value
    return None


def _repeat_count(minimum, maximum) -> int:
    if minimum > _MAX_REPEAT:
        return _MAX_REPEAT
    if minimum > 0:
        return int(minimum)
    if maximum == 0:
        return 0
    return 1


def _generate(items, depth: int = 0) -> str | None:
    if depth > 12:
        return None
    pieces: list[str] = []
    for op, arg in items:
        piece = _generate_token(op, arg, depth)
        if piece is None:
            return None
        pieces.append(piece)
    return "".join(pieces)


def _generate_token(op, arg, depth: int) -> str | None:
    if op is _sre.LITERAL:
        return chr(arg)
    if op is _sre.NOT_LITERAL:
        for candidate in _NEGATED_CANDIDATES:
            if ord(candidate) != arg:
                return candidate
        return None
    if op is _sre.ANY:
        return "a"
    if op is _sre.IN:
        return _class_char(arg)
    if op is _sre.CATEGORY:
        return _category_char(arg)
    if op in {_sre.MAX_REPEAT, _sre.MIN_REPEAT}:
        minimum, maximum, sub = arg
        count = _repeat_count(minimum, maximum)
        output: list[str] = []
        for _ in range(count):
            value = _generate(sub, depth + 1)
            if value is None:
                return None
            output.append(value)
        return "".join(output)
    if op is _sre.SUBPATTERN:
        return _generate(arg[-1], depth + 1)
    if op is _sre.BRANCH:
        for alternative in arg[1]:
            value = _generate(alternative, depth + 1)
            if value is not None:
                return value
        return None
    if op is _sre.AT:
        return ""
    return None


def sample_for_pattern(pattern: str | None) -> str | None:
    """返回一个能通过 `re.fullmatch(pattern, value)` 的样例；失败返回 None。"""
    if not pattern or _parser is None or _sre is None:
        return None
    try:
        parsed = _parser.parse(pattern)
    except (re.error, ValueError, TypeError):
        return None
    try:
        value = _generate(parsed)
    except Exception:
        return None
    if value is None:
        return None
    try:
        if re.fullmatch(pattern, value):
            return value
    except re.error:
        return None
    return None


__all__ = ["sample_for_pattern"]
