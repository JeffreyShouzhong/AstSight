"""路由清单与 OpenAPI 发现。

从 pipeline 抽出，供 `run` 与 `cases prepare` 共用，避免两套发现逻辑。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .profile.langroutes import scan_language_routes
from .profile.openapi import load_spec, normalize_path, parse_operations

# 路由清单健康度只做“是否可能漏扫”的告警，不参与符号映射或证据等级。
# 因此这里允许用轻量源码探测（数 Controller 文件），但绝不据此生成路由。
_CONTROLLER_PATTERN = re.compile(r"@(?:Rest)?Controller\b")
_SOURCE_SUFFIXES = {".java", ".kt"}
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_SKIP_PARTS = {
    ".git", ".codegraph", ".astsight", "node_modules",
    "target", "build", "out", "dist", "generated",
}
_CONTROLLER_SCAN_LIMIT = 5000

try:  # 图谱是可选基础设施
    from codegraph import CodeGraphUnavailable, EmbeddedCodeGraphClient, RoutePrefixRepair
except Exception:  # pragma: no cover - 理论上 contracts 同级必在
    CodeGraphUnavailable = RuntimeError  # type: ignore
    EmbeddedCodeGraphClient = None  # type: ignore
    RoutePrefixRepair = None  # type: ignore


def count_controller_files(project: Path, limit: int = _CONTROLLER_SCAN_LIMIT) -> int | None:
    """数带 `@Controller`/`@RestController` 的 Java/Kotlin 源文件。

    只作为路由清单的健康信号：生成接口、函数式路由、JAX-RS 等写法会让
    CodeGraph 提取不到方法级注解，这里给出一个“可能有 Controller 但没路由”的
    下界。扫描失败或超出上限返回 None，不把不确定当成 0。
    """
    src_main = project / "src" / "main"
    root = src_main if src_main.is_dir() else project
    count = 0
    scanned = 0
    try:
        for path in root.rglob("*"):
            if path.suffix not in _SOURCE_SUFFIXES:
                continue
            if any(part in _SKIP_PARTS for part in path.parts):
                continue
            scanned += 1
            if scanned > limit:
                return None
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if _CONTROLLER_PATTERN.search(text):
                count += 1
    except OSError:
        return None
    return count


def route_inventory_health(
    project: Path,
    routes: list[dict],
    *,
    openapi_operations: int | None = None,
) -> dict:
    """判断路由清单是否可能明显不完整或不可归因。

    信号：
    - 可归因路由（有 handler）显著少于 OpenAPI operation 数；
    - 可归因路由少于含 Controller 注解的源文件数；
    - 同一 `(method, path)` 对应多个不同 handler/owner（清单内自相矛盾）；
    - 路由没有 HTTP method，或 method 不是已知 HTTP 方法（如 DRF 的 `VIEWSET`）。

    任何一个成立就标 `degraded`。这是告警，不是阻塞：少数派写法（生成接口、
    Spring Data REST、函数式路由）提取不到路由时，必须让“少报”可见；
    Django 函数视图 method 未知时，也必须让“无法归因到具体接口”可见。
    """
    attributed = [route for route in routes if str(route.get("handler") or "").strip()]
    controller_files = count_controller_files(project)
    reasons: list[str] = []

    if openapi_operations is not None and openapi_operations >= 5:
        if len(attributed) * 2 < openapi_operations:
            reasons.append(
                f"attributed_routes_below_openapi:{len(attributed)}/{openapi_operations}"
            )
    if controller_files is not None and controller_files >= 1:
        if len(attributed) < controller_files:
            reasons.append(
                "attributed_routes_below_controller_files:"
                f"{len(attributed)}/{controller_files}"
            )

    endpoints: dict[tuple[str, str], set[str]] = {}
    without_method = 0
    unknown_method = 0
    for route in routes:
        method = str(route.get("method") or "").strip().upper()
        path = str(route.get("path") or "")
        if not method:
            without_method += 1
            continue
        if method not in _HTTP_METHODS:
            unknown_method += 1
            continue
        label = str(route.get("handler") or "").strip()
        owner = str(route.get("handler_owner") or "").strip()
        if owner:
            label = f"{owner}.{label}" if label else owner
        if not label:
            label = f"{route.get('source', '')}:{route.get('file', '')}"
        endpoints.setdefault((method, path), set()).add(label)
    duplicate_endpoints = sum(
        1 for labels in endpoints.values() if len(labels) > 1
    )
    if duplicate_endpoints:
        reasons.append(f"duplicate_route_endpoints:{duplicate_endpoints}")
    if without_method:
        reasons.append(f"routes_without_http_method:{without_method}")
    if unknown_method:
        reasons.append(f"routes_with_unknown_http_method:{unknown_method}")

    return {
        "status": "degraded" if reasons else "ok",
        "attributed_routes": len(attributed),
        "total_routes": len(routes),
        "controller_files": controller_files,
        "openapi_operations": openapi_operations,
        "duplicate_endpoints": duplicate_endpoints,
        "routes_without_http_method": without_method,
        "routes_with_unknown_http_method": unknown_method,
        "reasons": reasons,
        "note": (
            "路由清单可能不完整或存在冲突：受影响接口可能被少报或误报"
            if reasons else
            "路由清单与 Controller/OpenAPI 数量无明显缺口"
        ),
    }


def route_dicts(routes: list[Any]) -> list[dict]:
    out: list[dict] = []
    for route in routes:
        if isinstance(route, dict):
            out.append(route)
            continue
        inferred = bool(getattr(route, "inferred", False))
        confidence = getattr(route, "confidence", None)
        if not confidence:
            # RouteDeclaration 没有 confidence 字段：框架约定/字面量推导出的
            # 动作不能默认成 verified。
            confidence = "inferred" if inferred else "verified"
        out.append(
            {
                "method": getattr(route, "method", "") or "",
                "path": getattr(route, "path", "") or "",
                "handler": getattr(route, "handler", "") or "",
                "handler_owner": getattr(route, "handler_owner", "") or "",
                "file": getattr(route, "file", "") or "",
                "route_file": (
                    getattr(route, "route_file", "")
                    or getattr(route, "file", "")
                    or ""
                ),
                "source": getattr(route, "source", "framework_plugin"),
                "confidence": confidence,
                "framework": getattr(route, "framework", ""),
                "note": getattr(route, "note", "") or "",
                "line": (
                    getattr(route, "line", 0)
                    or getattr(route, "route_line", 0)
                    or 0
                ),
            }
        )
    return out


def _merge_openapi_routes(
    routes: list[dict], openapi_source: str | None
) -> tuple[list[dict], dict | None]:
    """把 OpenAPI operation 并入路由清单，补源码侧提取不到的接口。

    补进来的路由 `handler`/`file` 为空：它可以用于全量画像和接口清单，
    并保留 `operation_id` 供 impact 做低置信度兜底；没有 handler/file 意味着
    它**不参与**直接命中或调用链归因。归因缺口由
    `route_inventory_health` 显式告警，不在这里猜。
    """
    if not openapi_source:
        return routes, None
    if openapi_source.startswith(("http://", "https://")):
        # 远端契约留给后续画像阶段拉取；路由清单阶段不额外发起网络请求。
        return routes, {"added": 0, "skipped": "remote_source"}
    try:
        operations = parse_operations(load_spec(openapi_source))
    except Exception as exc:  # 契约坏了不能阻断已提取的路由
        return routes, {"added": 0, "error": f"{type(exc).__name__}: {exc}"[:300]}

    existing = {
        ((route.get("method") or "").upper(), normalize_path(route.get("path") or ""))
        for route in routes
    }
    added: list[dict] = []
    for operation in operations:
        key = (operation.method.upper(), normalize_path(operation.path))
        if key in existing:
            continue
        existing.add(key)
        added.append(
            {
                "method": operation.method.upper(),
                "path": operation.path,
                "handler": "",
                "handler_owner": "",
                "file": "",
                "source": "openapi",
                "confidence": "verified",
                "framework": "",
                # 生成接口型项目没有源码 handler，只能靠 operationId 做低置信度兜底。
                "operation_id": operation.operation_id,
            }
        )
    report = {
        "added": len(added),
        "operations": len(operations),
        "source": openapi_source,
    }
    return routes + added, report


def _merge_language_routes(
    routes: list[dict],
    project: Path,
    audit: dict,
    notes: list[str],
) -> list[dict]:
    """把 Java/Go/Node 源码扫描结果并入路由清单。

    已有带 handler 的同 `(method, path)` 路由优先（图谱证据更强）；只有
    OpenAPI 占位（handler 为空）时用源码路由替换，让下游 Mock 有调用点。
    """
    try:
        extra, warnings = scan_language_routes(project)
    except Exception as exc:  # 非 Python 路由扫描是补充证据，失败不阻断
        notes.append(
            f"language_route_scan_error:{type(exc).__name__}: {str(exc)[:300]}"
        )
        return routes
    if warnings:
        audit["language_scan_warnings"] = warnings[:50]
    if not extra:
        return routes

    key_index: dict[tuple[str, str], int] = {}
    for index, route in enumerate(routes):
        method = str(route.get("method") or "").upper()
        path = normalize_path(route.get("path") or "")
        if not method or not path:
            continue
        key_index.setdefault((method, path), index)

    added = 0
    replaced = 0
    merged = list(routes)
    for route in extra:
        key = (
            str(route.get("method") or "").upper(),
            normalize_path(route.get("path") or ""),
        )
        existing_index = key_index.get(key)
        if existing_index is None:
            key_index[key] = len(merged)
            merged.append(route)
            added += 1
            continue
        if not merged[existing_index].get("handler"):
            route_copy = dict(merged[existing_index])
            route_copy.update(route)
            merged[existing_index] = route_copy
            replaced += 1
    if added or replaced:
        audit["language_routes"] = {"added": added, "replaced_openapi": replaced}
    return merged


def inventory_routes(
    project: Path,
    *,
    codegraph: str = "auto",
    codegraph_module_root: str | None = None,
    full_index: bool | None = None,
    openapi_source: str | None = None,
) -> tuple[list[dict], dict, Any | None, list[str]]:
    """取得路由清单。图谱可用则用图谱+插件交叉校验，否则插件单源。

    `openapi_source` 只补「源码提取不到」的接口；补入项没有 handler，只能靠
    operationId 做低置信度归因。
    """
    notes: list[str] = []
    client = None

    if codegraph != "off" and EmbeddedCodeGraphClient is not None:
        try:
            client = EmbeddedCodeGraphClient(
                project,
                module_root=codegraph_module_root,
            )
            full = full_index
            if full is None:
                full = not (project / ".codegraph").exists()
            index_info = client.ensure_index(full=full)
            if not index_info.get("indexed"):
                notes.append(
                    f"codegraph_index_not_ready:{index_info.get('reason', 'unknown')}"
                )
            routes = client.find_routes()
            report = client.route_report
            audit = report.as_dict() if report else {}
            if routes:
                scanned = _merge_language_routes(
                    route_dicts(routes), project, audit, notes
                )
                merged, supplement = _merge_openapi_routes(scanned, openapi_source)
                if supplement:
                    audit["openapi_supplement"] = supplement
                return merged, audit, client, notes
            notes.append("codegraph_returned_no_routes")
        except CodeGraphUnavailable as exc:
            notes.append(f"codegraph_unavailable: {exc}")
            client = None
        except Exception as exc:  # 图谱故障不阻断主流程，但必须留下原因
            notes.append(f"codegraph_error: {type(exc).__name__}: {exc}"[:500])
            client = None

    if codegraph == "on":
        # 显式要求图谱时，降级到插件单源会让证据等级与配置承诺不一致——直接失败。
        raise RuntimeError(
            "CodeGraph 已要求启用但不可用: " + ("; ".join(notes) or "no routes")
        )

    if RoutePrefixRepair is None:
        audit = {"frameworks": [], "note": "route_plugin_unavailable"}
        scanned = _merge_language_routes([], project, audit, notes)
        merged, supplement = _merge_openapi_routes(scanned, openapi_source)
        if supplement:
            audit["openapi_supplement"] = supplement
        if notes:
            audit["notes"] = notes
        return merged, audit, client, notes
    repair = RoutePrefixRepair(project)
    declarations = repair.scan()
    audit = repair.last_report.as_dict()
    audit["source"] = "framework_plugin_only"
    scanned = _merge_language_routes(route_dicts(declarations), project, audit, notes)
    merged, supplement = _merge_openapi_routes(scanned, openapi_source)
    if supplement:
        audit["openapi_supplement"] = supplement
    if notes:
        audit["notes"] = notes
    return merged, audit, client, notes


def discover_openapi(
    project: Path, explicit: str | None, base_url: str | None
) -> str | None:
    if explicit:
        return explicit
    candidates = [
        "openapi.json",
        "openapi.yaml",
        "openapi.yml",
        "docs/openapi.json",
        "docs/openapi.yaml",
        "api/openapi.json",
        "app/openapi.json",
        # Spring Boot 常见位置（契约优先项目尤其常见）
        "src/main/resources/openapi.json",
        "src/main/resources/openapi.yaml",
        "src/main/resources/openapi.yml",
    ]
    for rel in candidates:
        candidate = project / rel
        if candidate.is_file():
            return str(candidate)
    if base_url:
        url = base_url.rstrip("/") + "/openapi.json"
        try:
            spec = load_spec(url)
            if spec.get("paths"):
                return url
        except Exception:
            return None
    return None


__all__ = [
    "count_controller_files",
    "discover_openapi",
    "inventory_routes",
    "route_dicts",
    "route_inventory_health",
]
