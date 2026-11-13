"""下游调用点证据模型。

Python AST 扫描与 Java/Go/Node 轻量扫描共用同一种证据结构，交给
`astsight.mock.identity.build_dep_id_plan` 统一计算 `dep_id`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HttpCallSite:
    """静态识别出的一个出站 HTTP 调用点。"""

    call_site_id: str
    handler: str
    file: str
    line: int
    method: str
    target: str | None = None
    path: str | None = None
    config_key: str | None = None
    service_name: str | None = None
    client: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_site_id": self.call_site_id,
            "handler": self.handler,
            "file": self.file,
            "line": self.line,
            "method": self.method,
            "target": self.target,
            "path": self.path,
            "config_key": self.config_key,
            "service_name": self.service_name,
            "client": self.client,
        }


__all__ = ["HttpCallSite"]
