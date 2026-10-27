"""C2 影响分析：变更符号 → 受影响接口。

三条路径，证据等级不同：
1. **直接命中**：变更符号就是路由 handler（AST 可证）→ `high` / `verified`；
2. **图谱追溯**：沿调用链反向闭包 ∩ 路由表 → `medium` / `inferred`；
3. **operationId 兜底**：契约优先项目（源码里只有生成接口的 `@Override`）里，
   方法名唯一匹配某个 OpenAPI `operationId` 时归因 → `low` / `inferred`。

兜底只在直接命中和图谱都没找到接口时启用，且要求变更文件是 Controller；
它是启发式，宁可漏也不扩大误报。
深度打满（truncated）时闭包可能没走完，不得升为 `verified`。
追不到路由的符号进 `unresolved`，不静默丢弃——"没影响"和"没追到"必须分开报。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .symbol_diff import ChangedSymbol

_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
_ATTRIBUTION_RANK = {
    "openapi_operation_id": 1,
    "call_graph": 2,
    "direct_handler": 3,
}
_CONTROLLER_PATTERN = re.compile(r"@(?:Rest)?Controller\b")
_SOURCE_SUFFIXES = {".java", ".kt"}


@dataclass
class ImpactSummary:
    changed_apis: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    traces: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "changed_apis": self.changed_apis,
            "unresolved_symbols": self.unresolved,
            "traces": self.traces,
        }


def _normalize_file(path: str) -> str:
    normalized = (path or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _route_endpoint(route: Any) -> str:
    method = (getattr(route, "method", "") or "").upper()
    path = getattr(route, "path", "") or ""
    return f"{method} {path}".strip()


def _symbol_owner(symbol: ChangedSymbol) -> str:
    """方法符号的所属类名（取 qualified_name 倒数第二段）。"""
    if symbol.kind != "method":
        return ""
    parts = (symbol.qualified_name or "").split(".")
    return parts[-2] if len(parts) >= 2 else ""


def _direct_routes(
    symbol: ChangedSymbol,
    handler_index: dict[str, list[dict]],
    file_index: dict[str, list[dict]],
) -> tuple[list[dict], bool]:
    """直接命中路由。

    返回 (routes, file_level)。

    - `file` 符号（文件级兜底）：只按 handler 文件命中；
    - 方法/函数：同名 handler 还要用文件消歧。Java 项目里不同 Controller
      出现同名方法是常态，只按名匹配会把无关接口算成受影响。
      若同名路由全都没有文件信息，才退回按名匹配。
    - 类视图展开出的路由（`handler_owner` 非空）：还必须类名一致。
      DRF 的 `list`/`create` 在每个 ViewSet 里都叫同一个名字，只按名匹配
      会把所有 ViewSet 的接口一起算进来。
    """
    symbol_file = _normalize_file(symbol.file)
    if symbol.kind == "file":
        return file_index.get(symbol_file, []), True

    named = handler_index.get(symbol.name, [])
    if not named:
        return named, False

    symbol_owner = _symbol_owner(symbol)
    owned = [
        route for route in named
        if not route.get("handler_owner")
        or (symbol_owner and route.get("handler_owner") == symbol_owner)
    ]
    if not owned:
        return [], False

    if not symbol_file:
        return owned, False

    with_file = [route for route in owned if route.get("file")]
    if not with_file:
        return owned, False
    return [
        route for route in with_file
        if _normalize_file(route.get("file", "")) == symbol_file
    ], False


def _operation_index(routes: list[dict]) -> dict[str, list[dict]]:
    """只索引由 OpenAPI 补入、没有源码 handler 的路由。

    有 handler 的路由已经走直接命中/图谱，不需要名前匹配兜底。
    """
    index: dict[str, list[dict]] = {}
    for route in routes:
        if route.get("source") != "openapi" or route.get("handler"):
            continue
        operation_id = str(route.get("operation_id") or "").strip()
        if not operation_id:
            continue
        index.setdefault(operation_id, []).append(route)
    return index


def _is_controller_file(
    project_root: str | Path | None,
    rel_file: str,
    cache: dict[str, bool],
) -> bool:
    """粗筛变更文件是否 Controller。

    operationId 兜底本质是“方法名像”，必须限制在 Controller 里，否则同名
    Service 方法会被算成接口变更。Java/Kotlin 文件无法读取时保守拒绝；
    非 Java/Kotlin（如 Python）不做该校验，未提供 project_root 时也不校验。
    """
    if project_root is None:
        return True
    rel = _normalize_file(rel_file)
    if not rel:
        return False
    if Path(rel).suffix.lower() not in _SOURCE_SUFFIXES:
        return True
    if rel in cache:
        return cache[rel]

    root = Path(project_root).resolve()
    try:
        path = (root / rel).resolve()
        path.relative_to(root)  # 防止 symlink/.. 逃出项目
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        cache[rel] = False
        return False
    ok = bool(_CONTROLLER_PATTERN.search(text))
    cache[rel] = ok
    return ok


def _operation_id_routes(
    symbol: ChangedSymbol,
    operation_index: dict[str, list[dict]],
    project_root: str | Path | None,
    controller_cache: dict[str, bool],
) -> tuple[list[dict], str]:
    """方法名唯一匹配 OpenAPI operationId 时的低置信度归因。"""
    if symbol.kind not in ("function", "method"):
        return [], ""
    if not _is_controller_file(project_root, symbol.file, controller_cache):
        return [], ""
    candidates = operation_index.get(symbol.name) or []
    if not candidates:
        return [], ""
    if len(candidates) > 1:
        # operationId 本应唯一；重复时无法判断落到哪个接口，交给人工。
        return [], "ambiguous_operation_id"
    return candidates, ""


def _merge_operation_fallback(
    summary: ImpactSummary,
    by_endpoint: dict[str, dict],
    routes: list[dict],
    symbol: ChangedSymbol,
) -> None:
    for route in routes:
        method = (route.get("method") or "").upper()
        path = route.get("path") or ""
        if not method:
            summary.unresolved.append(
                {
                    "symbol": symbol.qualified_name,
                    "file": symbol.file,
                    "reason": "route_method_missing",
                    "note": f"operationId 命中 {path}，但该路由没有 HTTP method",
                }
            )
            continue
        operation_id = str(route.get("operation_id") or "").strip()
        _merge(
            by_endpoint,
            f"{method} {path}",
            route,
            symbol,
            confidence="low",
            evidence="inferred",
            trace=[symbol.qualified_name, f"operationId:{operation_id}"],
            attribution="openapi_operation_id",
        )


def analyze_impact(
    changed_symbols: list[ChangedSymbol],
    routes: list[dict],
    codegraph_client: Any | None = None,
    *,
    depth: int = 5,
    project_root: str | Path | None = None,
) -> ImpactSummary:
    summary = ImpactSummary()
    by_endpoint: dict[str, dict] = {}

    handler_index: dict[str, list[dict]] = {}
    file_index: dict[str, list[dict]] = {}
    for route in routes:
        handler = route.get("handler") or ""
        if handler:
            handler_index.setdefault(handler, []).append(route)
        route_file = _normalize_file(route.get("file", ""))
        if route_file:
            file_index.setdefault(route_file, []).append(route)

    operation_index = _operation_index(routes)
    controller_cache: dict[str, bool] = {}

    for symbol in changed_symbols:
        direct_routes, file_level = _direct_routes(
            symbol, handler_index, file_index
        )
        if direct_routes and (file_level or symbol.kind in ("function", "method")):
            evidence = (
                symbol.evidence
                if symbol.evidence in {"verified", "inferred", "unmapped"}
                else "verified"
            )
            for route in direct_routes:
                method = (route.get("method") or "").upper()
                path = route.get("path") or ""
                if not method:
                    summary.unresolved.append(
                        {
                            "symbol": symbol.qualified_name,
                            "file": symbol.file,
                            "reason": "route_method_missing",
                            "note": f"直接命中 {path}，但该路由没有 HTTP method",
                        }
                    )
                    continue
                _merge(
                    by_endpoint,
                    f"{method} {path}",
                    route,
                    symbol,
                    confidence="high" if evidence == "verified" else "medium",
                    evidence=evidence,
                    trace=[symbol.qualified_name],
                    attribution="direct_handler",
                )
            continue

        if symbol.kind == "module":
            summary.unresolved.append(
                {
                    "symbol": symbol.qualified_name,
                    "file": symbol.file,
                    "reason": "module_level_change",
                    "note": "模块级变更（import/常量）无法精确映射到接口，需人工确认",
                }
            )
            continue

        fallback_routes, fallback_reason = _operation_id_routes(
            symbol, operation_index, project_root, controller_cache
        )

        if codegraph_client is None:
            if fallback_routes:
                _merge_operation_fallback(
                    summary, by_endpoint, fallback_routes, symbol
                )
                continue
            if fallback_reason == "ambiguous_operation_id":
                summary.unresolved.append(
                    {
                        "symbol": symbol.qualified_name,
                        "file": symbol.file,
                        "reason": fallback_reason,
                        "note": "方法名匹配到多个 OpenAPI operationId，未自动归因",
                    }
                )
                continue
            summary.unresolved.append(
                {
                    "symbol": symbol.qualified_name,
                    "file": symbol.file,
                    "reason": "no_codegraph",
                    "note": "图谱不可用，无法反向追溯；不降级猜测",
                }
            )
            continue

        # 文件级兜底符号（删文件/纯删除/import 变更）可能没有可追溯的节点 id。
        # 有 id 时仍尝试闭包；没有就如实报 no_route_in_file，不假装影响面为空。
        if symbol.kind == "file" and not symbol.node_id:
            summary.unresolved.append(
                {
                    "symbol": symbol.qualified_name,
                    "file": symbol.file,
                    "reason": "no_route_in_file",
                    "note": "文件级变更未命中路由 handler 文件，且无可追溯节点",
                }
            )
            continue

        if symbol.node_id:
            try:
                result = codegraph_client.impact(
                    symbol.name, depth, node_id=symbol.node_id
                )
            except TypeError:
                # 兼容未实现 node_id 参数的第三方/测试替身
                result = codegraph_client.impact(symbol.name, depth)
        else:
            result = codegraph_client.impact(symbol.name, depth)
        affected_routes = list(getattr(result, "affected_routes", []) or [])
        trace = list(getattr(result, "trace", []) or [])
        truncated = bool(getattr(result, "truncated", False))

        if not affected_routes:
            if fallback_routes:
                _merge_operation_fallback(
                    summary, by_endpoint, fallback_routes, symbol
                )
                summary.traces.append(
                    {
                        "symbol": symbol.qualified_name,
                        "trace": trace,
                        "truncated": truncated,
                        "fallback": "openapi_operation_id",
                    }
                )
                continue
            reason = fallback_reason or "no_route_reached"
            note = "调用链反向闭包未命中任何路由" + ("（深度截断）" if truncated else "")
            if symbol.kind == "file":
                reason = "no_route_reached"
                note = "文件级节点反向闭包未命中任何路由" + ("（深度截断）" if truncated else "")
            elif fallback_reason == "ambiguous_operation_id":
                note = "方法名匹配到多个 OpenAPI operationId，未自动归因"
            summary.unresolved.append(
                {
                    "symbol": symbol.qualified_name,
                    "file": symbol.file,
                    "reason": reason,
                    "note": note,
                }
            )
            summary.traces.append(
                {"symbol": symbol.qualified_name, "trace": trace, "truncated": truncated}
            )
            continue

        for route in affected_routes:
            method = (getattr(route, "method", "") or "").upper()
            path = getattr(route, "path", "") or ""
            if not method:
                summary.unresolved.append(
                    {
                        "symbol": symbol.qualified_name,
                        "file": symbol.file,
                        "reason": "route_method_missing",
                        "note": f"追溯命中 {path}，但该路由没有 HTTP method",
                    }
                )
                continue
            route_dict = {
                "method": method,
                "path": path,
                "handler": getattr(route, "handler", ""),
                "handler_owner": getattr(route, "handler_owner", ""),
                "file": getattr(route, "file", ""),
                "source": getattr(route, "source", "graph_repaired"),
                "confidence": getattr(route, "confidence", "inferred"),
            }
            _merge(
                by_endpoint,
                f"{method} {path}",
                route_dict,
                symbol,
                confidence="low" if truncated else "medium",
                evidence="inferred" if not truncated else "unmapped",
                trace=trace or [symbol.qualified_name],
                attribution="call_graph",
            )
        summary.traces.append(
            {"symbol": symbol.qualified_name, "trace": trace, "truncated": truncated}
        )

    summary.changed_apis = list(by_endpoint.values())
    return summary


def _merge(
    bucket: dict[str, dict],
    endpoint: str,
    route: dict,
    symbol: ChangedSymbol,
    *,
    confidence: str,
    evidence: str,
    trace: list[str],
    attribution: str = "",
) -> None:
    record = bucket.get(endpoint)
    if record is None:
        bucket[endpoint] = {
            "handler": endpoint,
            "method": route.get("method", ""),
            "path": route.get("path", ""),
            "route_handler": route.get("handler", ""),
            "route_owner": route.get("handler_owner", ""),
            "confidence": confidence,
            "evidence": evidence,
            "attribution": attribution,
            "changed_symbols": [symbol.qualified_name],
            "change_files": [symbol.file],
            "trace": list(trace),
            "route_source": route.get("source", ""),
        }
        return

    if symbol.qualified_name not in record["changed_symbols"]:
        record["changed_symbols"].append(symbol.qualified_name)
    if symbol.file not in record["change_files"]:
        record["change_files"].append(symbol.file)
    for item in trace:
        if item not in record["trace"]:
            record["trace"].append(item)
    if _CONFIDENCE_RANK.get(confidence, 0) > _CONFIDENCE_RANK.get(record["confidence"], 0):
        record["confidence"] = confidence
    if _ATTRIBUTION_RANK.get(attribution, 0) > _ATTRIBUTION_RANK.get(
        record.get("attribution", ""), 0
    ):
        record["attribution"] = attribution
    if evidence == "unmapped":
        record["evidence"] = "unmapped"
    elif evidence == "verified" and record["evidence"] != "unmapped":
        record["evidence"] = "verified"
