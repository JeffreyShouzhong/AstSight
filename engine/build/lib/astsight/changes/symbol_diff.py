"""变更行 → 变更符号（C2 第二步）。

两条映射路径：

1. **Python AST**：精确、可解释，行→符号一律 `verified`；
2. **CodeGraph 文件符号表**（Java/Kotlin/TS 等）：符号边界由 CodeGraph 的
   提取器给出，AstSight 只做 `git diff 行区间 ∩ startLine..endLine`，
   不在框架里重新实现一套代码图谱。

API 级判定允许粗一点，但证据等级必须诚实：

- 行落在方法/函数内 → `verified`（AST/提取器能证明改的是这个方法）；
- 行落在类声明、字段或 import 上 → 该类全部方法 / 整个文件标记为受影响，
  但只能算 `inferred`（共享状态可能影响 handler，但没证明具体哪条路径）；
- 纯删除（无新增行）无法用当前工作树的符号表定位旧代码 → 整个文件标
  `inferred`，不能假装知道被删的是哪个方法；
- 没有图谱且语言非 Python → `unsupported`，不猜。

`node_id` 会一路带到影响分析：Java 同名重载很多，按名反查会选错节点。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .git_diff import FileChange, file_at, repo_root

# Python 走 AST；其余语言若 CodeGraph 可用，走图谱符号表。
PYTHON_SUFFIXES = {".py"}

# CodeGraph NodeKind 中可作为 C2 追溯单位的类型。
GRAPH_FUNCTION_KINDS = {"function", "method"}
GRAPH_CONTAINER_KINDS = {
    "class", "struct", "interface", "trait", "protocol", "component",
}


@dataclass(frozen=True)
class SymbolSpan:
    name: str
    qualified_name: str
    kind: str           # function / method / class / module / file
    file: str
    start: int          # 含装饰器的起始行
    end: int
    node_id: str = ""   # CodeGraph 节点 id；Python AST 路径为空
    language: str = ""

    def contains(self, line: int) -> bool:
        return self.start <= line <= self.end


@dataclass
class ChangedSymbol:
    name: str
    qualified_name: str
    kind: str
    file: str
    side: str = "new"   # new（新增/修改）/ old（删除）
    lines: list[int] = field(default_factory=list)
    evidence: str = "verified"
    node_id: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "file": self.file,
            "side": self.side,
            "lines": self.lines,
            "evidence": self.evidence,
            "node_id": self.node_id,
        }


def _module_name(rel_path: str) -> str:
    path = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    return path.replace("/", ".")


def collect_spans(source: str, rel_path: str) -> list[SymbolSpan]:
    """解析源码，返回全部函数/方法与类的作用域。"""
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return []

    spans: list[SymbolSpan] = []
    module = _module_name(rel_path)

    def visit(node: ast.AST, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min(
                    [child.lineno] + [d.lineno for d in child.decorator_list]
                )
                qualified = ".".join([*prefix, child.name])
                kind = "method" if isinstance(node, ast.ClassDef) else "function"
                spans.append(
                    SymbolSpan(
                        name=child.name,
                        qualified_name=qualified,
                        kind=kind,
                        file=rel_path,
                        start=start,
                        end=getattr(child, "end_lineno", child.lineno) or child.lineno,
                    )
                )
                visit(child, [*prefix, child.name])
            elif isinstance(child, ast.ClassDef):
                qualified = ".".join([*prefix, child.name])
                spans.append(
                    SymbolSpan(
                        name=child.name,
                        qualified_name=qualified,
                        kind="class",
                        file=rel_path,
                        start=child.lineno,
                        end=getattr(child, "end_lineno", child.lineno) or child.lineno,
                    )
                )
                visit(child, [*prefix, child.name])

    visit(tree, [module])
    return spans


def _symbol_for_line(spans: list[SymbolSpan], line: int, rel_path: str) -> SymbolSpan:
    """取包含该行的最内层函数/方法；没有则返回模块符号。"""
    hits = [s for s in spans if s.contains(line)]
    # 函数优先、起始行更大（更内层）优先
    hits.sort(key=lambda s: (s.kind == "class", s.start), reverse=False)
    funcs = [s for s in hits if s.kind in ("function", "method")]
    if funcs:
        return max(funcs, key=lambda s: s.start)
    if hits:
        return max(hits, key=lambda s: s.start)
    return SymbolSpan(
        name=Path(rel_path).stem,
        qualified_name=_module_name(rel_path),
        kind="module",
        file=rel_path,
        start=1,
        end=10**9,
    )


def _normalize_rel(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _spans_from_graph(nodes: list[Any], rel_path: str) -> list[SymbolSpan]:
    """把 CodeGraph Node 收敛成可做区间相交的符号。

    只保留函数/方法与容器类型：字段、import 等交给「文件级兜底」，
    否则一个字段变更会被误当成一个可追溯的调用节点。
    """
    spans: list[SymbolSpan] = []
    for node in nodes:
        kind = getattr(node, "kind", "") or ""
        if kind not in GRAPH_FUNCTION_KINDS | GRAPH_CONTAINER_KINDS:
            continue
        start = int(getattr(node, "line", 0) or 0)
        end = int(getattr(node, "end_line", 0) or 0) or start
        if start <= 0 or end < start:
            continue
        file = _normalize_rel(getattr(node, "file", "") or rel_path)
        name = getattr(node, "name", "") or Path(file).stem
        qualified = getattr(node, "qualified_name", "") or name
        spans.append(
            SymbolSpan(
                name=name,
                qualified_name=qualified,
                kind=kind,
                file=file,
                start=start,
                end=end,
                node_id=getattr(node, "node_id", "") or "",
                language=getattr(node, "language", "") or "",
            )
        )
    return spans


def _file_span(nodes: list[Any], rel_path: str) -> SymbolSpan:
    """整文件兜底符号。

    优先复用 CodeGraph 的 file 节点 id；没有也能靠路由表按文件命中。
    """
    file_node_id = ""
    for node in nodes:
        if getattr(node, "kind", "") == "file":
            file_node_id = getattr(node, "node_id", "") or ""
            break
    return SymbolSpan(
        name=Path(rel_path).stem,
        qualified_name=_normalize_rel(rel_path),
        kind="file",
        file=_normalize_rel(rel_path),
        start=1,
        end=10**9,
        node_id=file_node_id,
    )


def _graph_spans_for_line(
    spans: list[SymbolSpan], line: int, file_span: SymbolSpan
) -> list[tuple[SymbolSpan, str]]:
    """返回 (符号, 证据等级)。

    - 方法/函数内：verified；
    - 类声明/字段：该类全部方法，inferred（共享状态可能影响 handler）；
    - 其它（import/包声明/空行）：整文件，inferred。
    """
    methods = [
        span for span in spans
        if span.kind in GRAPH_FUNCTION_KINDS and span.contains(line)
    ]
    if methods:
        # 最内层：起始行最大者（Java 无嵌套方法，但保留通用规则）
        return [(max(methods, key=lambda span: span.start), "verified")]

    containers = [
        span for span in spans
        if span.kind in GRAPH_CONTAINER_KINDS and span.contains(line)
    ]
    if containers:
        container = min(containers, key=lambda span: (span.end - span.start, -span.start))
        members = [
            span for span in spans
            if span.kind in GRAPH_FUNCTION_KINDS
            and span.start >= container.start
            and span.end <= container.end
        ]
        if members:
            return [(member, "inferred") for member in members]
        return [(container, "inferred")]

    return [(file_span, "inferred")]


_EVIDENCE_RANK = {"unmapped": 0, "inferred": 1, "verified": 2}


def _add_symbol(
    symbols: dict[tuple[str, str, str], ChangedSymbol],
    span: SymbolSpan,
    side: str,
    *,
    line: int | None = None,
    evidence: str = "verified",
) -> None:
    key = (span.qualified_name, span.file, side)
    record = symbols.get(key)
    if record is None:
        record = ChangedSymbol(
            name=span.name,
            qualified_name=span.qualified_name,
            kind=span.kind,
            file=span.file,
            side=side,
            evidence=evidence,
            node_id=span.node_id,
        )
        symbols[key] = record
    if span.node_id and not record.node_id:
        record.node_id = span.node_id
    if _EVIDENCE_RANK.get(evidence, 0) > _EVIDENCE_RANK.get(record.evidence, 0):
        record.evidence = evidence
    if line is not None and line not in record.lines:
        record.lines.append(line)


def symbols_for_changes(
    project: str | Path,
    base: str,
    target: str,
    changes: list[FileChange],
    codegraph_client: Any | None = None,
) -> tuple[list[ChangedSymbol], list[dict]]:
    """把文件级行区间映射为变更符号。

    返回 (符号列表, unsupported 列表)。unsupported 必须显式进报告——
    "没追到"和"追到没影响"是两件事，不能混为一谈。

    Python 走 AST，其它语言走 `codegraph_client.symbols_in_file()`。
    CodeGraph 不可用时，非 Python 文件标 `unsupported`，不降级猜测。
    """
    root = repo_root(project)
    symbols: dict[tuple[str, str, str], ChangedSymbol] = {}
    unsupported: list[dict] = []

    for change in changes:
        suffix = Path(change.path).suffix.lower()
        if change.is_binary:
            unsupported.append({"file": change.path, "reason": "binary"})
            continue

        if suffix in PYTHON_SUFFIXES:
            if change.status != "deleted" and change.added_ranges:
                source = file_at(root, target, change.path)
                spans = collect_spans(source, change.path)
                for start, end in change.added_ranges:
                    for line in range(start, end + 1):
                        _add_symbol(
                            symbols,
                            _symbol_for_line(spans, line, change.path),
                            "new",
                            line=line,
                        )

            if change.removed_ranges:
                old_path = change.old_path or change.path
                old_source = file_at(root, base, old_path)
                old_spans = collect_spans(old_source, old_path)
                for start, end in change.removed_ranges:
                    for line in range(start, end + 1):
                        _add_symbol(
                            symbols,
                            _symbol_for_line(old_spans, line, old_path),
                            "old",
                            line=line,
                        )

            # 纯重命名、内容未变：符号级 diff 为空，但 handler 所在文件变了，
            # API 级仍需让路由表按文件命中。
            if (
                change.status == "renamed"
                and not change.added_ranges
                and not change.removed_ranges
            ):
                _add_symbol(
                    symbols,
                    _file_span([], change.path),
                    "new",
                    evidence="inferred",
                )
            continue

        # ── 非 Python：符号边界只能来自 CodeGraph ──────────────────
        if codegraph_client is None:
            unsupported.append(
                {
                    "file": change.path,
                    "reason": f"language_not_supported:{suffix or 'unknown'}",
                    "note": "非 Python 文件需要 CodeGraph 提供符号行区间；未降级猜测",
                }
            )
            continue

        try:
            nodes = list(codegraph_client.symbols_in_file(change.path))
        except Exception as exc:  # 图谱单次失败不打断整条分析
            unsupported.append(
                {
                    "file": change.path,
                    "reason": f"codegraph_error:{type(exc).__name__}",
                    "note": str(exc)[:300],
                }
            )
            continue

        file_span = _file_span(nodes, change.path)

        if change.status == "deleted":
            # 删除文件的旧符号无法从当前索引取到；文件级影响仍可确定。
            _add_symbol(symbols, file_span, "old", evidence="inferred")
            continue

        if not nodes:
            unsupported.append(
                {
                    "file": change.path,
                    "reason": f"no_graph_symbols:{suffix or 'unknown'}",
                    "note": "CodeGraph 未返回该文件节点（语言不支持或索引未同步）",
                }
            )
            continue

        spans = _spans_from_graph(nodes, change.path)
        for start, end in change.added_ranges:
            for line in range(start, end + 1):
                for span, evidence in _graph_spans_for_line(spans, line, file_span):
                    _add_symbol(
                        symbols, span, "new", line=line, evidence=evidence
                    )

        # 纯删除没有新增行：删除行的行号属于 base，当前工作树的符号区间
        # 对不上。整文件标 inferred，不假装知道被删的是哪个方法。
        if change.removed_ranges and not change.added_ranges:
            _add_symbol(symbols, file_span, "old", evidence="inferred")

        # 重命名且内容未变：新增路径需要被路由表按文件命中。
        if (
            change.status == "renamed"
            and not change.added_ranges
            and not change.removed_ranges
        ):
            _add_symbol(symbols, file_span, "new", evidence="inferred")

    # 同一个方法/函数既有新增行又有删除行时，会分别生成 side=new 与 side=old
    # 两条记录。API 级归因只关心「这个符号变了」，两条记录会让同一条接口
    # 在报告里重复出现、也会让 unresolved 重复计数。这里按 (qualified, file)
    # 合并，保留 new 语义（old 仅在纯删除时单独存在）。
    merged: dict[tuple[str, str], ChangedSymbol] = {}
    ordered: list[ChangedSymbol] = []
    for record in symbols.values():
        record.lines.sort()
        key = (record.qualified_name, record.file)
        existing = merged.get(key)
        if existing is None:
            merged[key] = record
            ordered.append(record)
            continue
        existing.lines = sorted(set(existing.lines) | set(record.lines))
        if _EVIDENCE_RANK.get(record.evidence, 0) > _EVIDENCE_RANK.get(
            existing.evidence, 0
        ):
            existing.evidence = record.evidence
        if record.side == "new":
            existing.side = "new"
        if record.node_id and not existing.node_id:
            existing.node_id = record.node_id

    return ordered, unsupported
