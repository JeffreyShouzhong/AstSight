"""Agent 流程错误：错误码稳定，detail 供 CLI/报告展示。"""
from __future__ import annotations

from typing import Any


class AgentError(RuntimeError):
    def __init__(self, code: str, detail: str = "", *, details: Any = None):
        self.code = code
        self.detail = detail
        self.details = details
        super().__init__(f"{code}: {detail}" if detail else code)


__all__ = ["AgentError"]
