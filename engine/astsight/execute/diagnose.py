"""确定性失败归因（C7.3 / A5 的确定性前置路径）。

只做规则可判的部分，判不了就 `unknown` 交人工——**不猜、不错误重试**。
LLM 兜底是 M2 的事，M1 不引入。

归因口径对齐设计文档：
- 环境抖动 → 重试理由，不是根因，不入 user/framework 归属；
- 5xx / 契约内行为异常 → `code_defect`（user，永不重试）；
- 用例/断言写错 → `case_error`（user）；
- 变更导致的既有行为变化 → `change_induced`（user）；
- 生成物自身不可运行 → `framework_error`（framework）。
"""
from __future__ import annotations

import re
from typing import Any

_STATUS_RE = re.compile(r"(?:got|status)\s+(\d{3})", re.I)

_ENV_SIGNS = (
    "connecterror", "connectionerror", "connection refused", "connection reset",
    "timed out", "timeout", "temporary failure in name resolution",
    "name or service not known", "remote protocol error", "503 service unavailable",
    "502 bad gateway", "504 gateway",
)
_FRAMEWORK_SIGNS = (
    "importerror", "modulenotfounderror", "fixture", "collected 0 items",
    "errors during collection", "syntaxerror", "attributeerror: module",
)
# 契约声明返回 JSON，2xx 却给不出 JSON：这是被测系统的问题，不是用例问题。
_SUT_BODY_SIGNS = ("jsondecodeerror", "expecting value: line 1 column 1")


def _actual_status(message: str) -> int | None:
    match = _STATUS_RE.search(message or "")
    return int(match.group(1)) if match else None


def classify(
    record: Any,
    case: Any | None,
    changed_endpoints: set[str] | None = None,
) -> dict:
    """返回 {reason, root_cause, action_target, retryable, detail}。"""
    message = (getattr(record, "message", "") or "").lower()
    outcome = getattr(record, "outcome", "unknown")
    endpoint = getattr(case, "endpoint", "") if case is not None else ""
    category = getattr(case, "category", "") if case is not None else ""
    changed_endpoints = changed_endpoints or set()

    if outcome == "passed":
        return {
            "reason": "passed", "root_cause": None, "action_target": None,
            "retryable": False, "detail": "",
        }
    if outcome == "skipped":
        return {
            "reason": "skipped", "root_cause": None, "action_target": None,
            "retryable": False, "detail": "",
        }
    if outcome == "blocked":
        return {
            "reason": "data_unavailable",
            "root_cause": None,
            "action_target": "user",
            "retryable": False,
            "detail": "造数计划或数据库未就绪：用例被阻断，不计入通过，也不是 SUT 缺陷",
        }

    if any(sign in message for sign in _ENV_SIGNS):
        return {
            "reason": "env_flake",
            "root_cause": None,
            "action_target": None,
            "retryable": True,
            "detail": "连接/超时类失败：环境抖动，原样重试一次",
        }

    if any(sign in message for sign in _FRAMEWORK_SIGNS) and outcome == "error":
        return {
            "reason": "framework_error",
            "root_cause": None,
            "action_target": "framework",
            "retryable": False,
            "detail": "生成物无法被收集/导入：框架侧问题，不重试",
        }

    if any(sign in message for sign in _SUT_BODY_SIGNS):
        return {
            "reason": "sut_bug",
            "root_cause": "code_defect",
            "action_target": "user",
            "retryable": False,
            "detail": "契约声明 JSON 响应，实际无法解析：按被测系统缺陷处理",
        }

    status = _actual_status(message)
    if status is not None:
        if status >= 500:
            return {
                "reason": "sut_bug",
                "root_cause": "code_defect",
                "action_target": "user",
                "retryable": False,
                "detail": f"接口返回 {status}：按被测系统缺陷处理，永不重试",
            }
        if 400 <= status < 500:
            if endpoint in changed_endpoints and category == "positive":
                return {
                    "reason": "change_induced",
                    "root_cause": "change_induced",
                    "action_target": "user",
                    "retryable": False,
                    "detail": f"变更接口的正向用例返回 {status}：可能是本次变更引入的行为变化",
                }
            return {
                "reason": "case_error",
                "root_cause": "case_error",
                "action_target": "user",
                "retryable": False,
                "detail": f"正向用例返回 {status}：输入构造或契约假设有误（M1 不具备造数能力）",
            }

    return {
        "reason": "unknown",
        "root_cause": None,
        "action_target": None,
        "retryable": False,
        "detail": "确定性规则未命中，交人工复核（不重试）",
    }
