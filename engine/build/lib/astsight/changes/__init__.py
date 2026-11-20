"""C2 变更感知的输入侧：git diff → 变更文件与行区间。"""

from .git_diff import FileChange, GitDiffError, diff_files
from .symbol_diff import ChangedSymbol, SymbolSpan, symbols_for_changes

__all__ = [
    "FileChange",
    "GitDiffError",
    "diff_files",
    "ChangedSymbol",
    "SymbolSpan",
    "symbols_for_changes",
]
