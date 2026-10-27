"""JSON 持久化通用能力：canonical JSON、sha256、原子写。

case store 是审核后的唯一事实源，写入必须是“整文件替换”，不能留下半截 JSON。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    """稳定序列化：键排序、紧凑分隔符，用于指纹/哈希。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json_atomic(path: str | Path, value: Any) -> Path:
    """先写同目录临时文件，fsync 后 `os.replace`，避免半截文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temp_path, target)
    except Exception:
        handle.close()
        temp_path.unlink(missing_ok=True)
        raise
    return target


__all__ = [
    "canonical_json",
    "load_json",
    "save_json_atomic",
    "sha256_bytes",
    "sha256_file",
    "sha256_json",
    "sha256_text",
]
