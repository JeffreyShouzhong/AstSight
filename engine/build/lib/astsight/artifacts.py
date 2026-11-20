"""运行产物落盘。

设计约束（能力文档 2.2）：能力之间只通过**落盘的契约文件**通信，
不共享内存对象——保证任一环节可独立重跑、可人工干预中间产物。

目录约定：
    .astsight/runs/<run_id>/
        run_meta.json
        changes.json
        routes.json
        changed_apis.json
        profiles.json
        cases.json
        codegen.json
        generated/          pytest 资产
        results.xml
        report.json
        report.md
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def new_run_id(mode: str) -> str:
    # 带毫秒：同一秒内重复触发（CI 重跑、并发演示）不会互相覆盖产物
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return f"{stamp}-{mode}"


def slugify(text: str, limit: int = 60) -> str:
    """把接口/文件名转成安全的文件片段。"""
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", text).strip("_")
    return (cleaned or "item")[:limit]


class RunDir:
    """一次运行的产物目录。"""

    def __init__(self, root: str | Path, run_id: str):
        self.root = Path(root).resolve()
        self.run_id = run_id
        self.path = self.root / run_id
        self.path.mkdir(parents=True, exist_ok=True)

    def write_json(self, name: str, payload: Any) -> Path:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return target

    def write_text(self, name: str, text: str) -> Path:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def rel(self, path: Path | str) -> str:
        try:
            return Path(path).resolve().relative_to(self.path).as_posix()
        except ValueError:
            return str(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
