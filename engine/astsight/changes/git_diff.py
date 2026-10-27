"""git 分支差异解析（C2 第一步，纯确定性）。

只做一件事：把 `git diff base..target` 变成
「哪个文件的哪些行被增/删」。符号级映射由 symbol_diff 完成。

为什么不用 `git diff --name-only` 就够：
  只有文件名无法回答"改了函数还是改了注释"。C2 需要函数级变更符号，
  否则任何一次格式化都会把整个文件判成变更，影响面爆炸。

比较方式：默认三点 `base...target`（取 merge-base），符合 PR/分支的语义；
仓库没有共同祖先时回退两点 `base..target`，并在结果里如实标明。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class GitDiffError(RuntimeError):
    """git 命令失败（分支不存在、不是仓库等）。早失败，不静默返回空。"""


@dataclass
class FileChange:
    """单个文件的变更（路径均为仓库相对 POSIX 路径）。"""

    path: str
    old_path: str = ""
    status: str = "modified"          # added/modified/deleted/renamed
    added_ranges: list[tuple[int, int]] = field(default_factory=list)
    removed_ranges: list[tuple[int, int]] = field(default_factory=list)
    is_binary: bool = False

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "old_path": self.old_path or self.path,
            "status": self.status,
            "added_ranges": [list(r) for r in self.added_ranges],
            "removed_ranges": [list(r) for r in self.removed_ranges],
            "is_binary": self.is_binary,
        }


def _git(project: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise GitDiffError(
            f"git {' '.join(args)} 失败（{proc.returncode}）: {proc.stderr.strip()[:800]}"
        )
    return proc.stdout


def resolve_range(project: str | Path, base: str, target: str = "HEAD") -> tuple[str, str]:
    """返回 (range_arg, comparison_mode)。

    优先三点（merge-base）。单分支/无共同祖先仓库回退两点，并说明模式。
    """
    root = Path(project).resolve()
    merge_base = subprocess.run(
        ["git", "merge-base", base, target],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
    )
    if merge_base.returncode == 0 and merge_base.stdout.strip():
        return f"{base}...{target}", "three_dot"
    return f"{base}..{target}", "two_dot"


def diff_files(
    project: str | Path,
    base: str,
    target: str = "HEAD",
    *,
    paths: list[str] | None = None,
) -> tuple[list[FileChange], dict]:
    """解析分支差异。返回 (变更文件列表, 元信息)。"""
    root = Path(project).resolve()
    if not (root / ".git").exists() and not _is_git_worktree(root):
        raise GitDiffError(f"不是 git 仓库: {root}")

    range_arg, mode = resolve_range(root, base, target)
    args = [
        "diff", "--no-color", "--no-ext-diff", "--unified=0",
        "--find-renames", "--find-copies", range_arg,
    ]
    if paths:
        args += ["--", *paths]
    raw = _git(root, *args)

    changes: list[FileChange] = []
    current: FileChange | None = None
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            # diff --git a/x b/x
            parts = line.split(" ")
            old_path = parts[2][2:] if len(parts) > 2 else ""
            new_path = parts[3][2:] if len(parts) > 3 else old_path
            current = FileChange(path=new_path, old_path=old_path)
            changes.append(current)
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current.status = "added"
            continue
        if line.startswith("deleted file mode"):
            current.status = "deleted"
            continue
        if line.startswith("rename from "):
            current.old_path = line[len("rename from "):].strip()
            current.status = "renamed"
            continue
        if line.startswith("rename to "):
            current.path = line[len("rename to "):].strip()
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.is_binary = True
            continue
        match = _HUNK_RE.match(line)
        if match:
            old_start = int(match.group(1))
            old_count = int(match.group(2) or 1)
            new_start = int(match.group(3))
            new_count = int(match.group(4) or 1)
            if new_count:
                current.added_ranges.append((new_start, new_start + new_count - 1))
            if old_count:
                current.removed_ranges.append((old_start, old_start + old_count - 1))

    # 纯新增文件没有 hunk 时，按"整个文件都是新增"处理
    for change in changes:
        if change.status == "added" and not change.added_ranges:
            total = len(file_at(root, target, change.path).splitlines())
            if total:
                change.added_ranges.append((1, total))

    meta = {
        "base": base,
        "target": target,
        "range": range_arg,
        "comparison": mode,
        "changed_files": len([c for c in changes if not c.is_binary]),
        "binary_files": [c.path for c in changes if c.is_binary],
    }
    return changes, meta


def file_at(project: str | Path, rev: str, path: str) -> str:
    """取某个版本的文件内容；文件不存在返回空串（删除场景）。"""
    root = Path(project).resolve()
    proc = subprocess.run(
        ["git", "show", f"{rev}:{path}"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else ""


def _is_git_worktree(root: Path) -> bool:
    return (root / ".git").is_file()


def repo_root(project: str | Path) -> Path:
    """项目可能位于仓库子目录，向上找 git 根。"""
    root = Path(project).resolve()
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise GitDiffError(f"不是 git 仓库: {root}")
    return Path(proc.stdout.strip()).resolve()
