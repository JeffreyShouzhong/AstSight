"""C3 画像构建（S3）：路由 × OpenAPI × 下游依赖。

事实源零 LLM。任何一路缺失都显式标记，不降级猜测：
- 无 OpenAPI → `contract_incomplete=true`，用例只能做状态码冒烟；
- 路由证据等级直接沿用路由审计结果（verified/inferred/unmapped）；
- 下游依赖来自 CodeGraph 正向 `callees`，无法识别时留空并标 `downstream_unmapped`。
"""
from __future__ import annotations

from typing import Any

from .openapi import EndpointSpec, normalize_path


def _route_key(method: str, path: str) -> tuple[str, str]:
    return (method.upper(), normalize_path(path))


def _endpoint_index(operations: list[EndpointSpec]) -> dict[tuple[str, str], EndpointSpec]:
    index: dict[tuple[str, str], EndpointSpec] = {}
    for operation in operations:
        index.setdefault(_route_key(operation.method, operation.path), operation)
    return index


def _classify_dep(name: str) -> str:
    """下游类型粗分类（仅标类型，不判读写；全 Mock 策略下读写不影响决策）。"""
    lowered = name.lower()
    if any(token in lowered for token in ("redis", "cache", "memcache")):
        return "cache"
    if any(token in lowered for token in ("kafka", "rabbit", "mq", "producer", "publish", "queue")):
        return "mq"
    if any(token in lowered for token in ("grpc", "rpc", "thrift", "dubbo")):
        return "rpc"
    if any(token in lowered for token in ("http", "requests", "httpx", "client", "api", "gateway", "payment", "pay")):
        return "http"
    return "unknown"


def build_profiles(
    routes: list[dict],
    operations: list[EndpointSpec],
    *,
    codegraph_client: Any | None = None,
    max_callees: int = 30,
) -> tuple[list[dict], list[dict]]:
    """返回 (profiles, unmapped)。

    routes 的每一项至少含 method/path/handler/confidence/source。
    """
    index = _endpoint_index(operations)
    profiles: list[dict] = []
    unmapped: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for route in routes:
        method = str(route.get("method") or "").upper()
        path = str(route.get("path") or "")
        if not path:
            continue
        if not method:
            # Django 常态：源码里没有 method。没有 method 就无法构造请求，
            # 如实进 unmapped，不猜 GET。
            unmapped.append(
                {
                    "endpoint": f"? {path}",
                    "handler": route.get("handler", ""),
                    "reason": "http_method_missing",
                    "file": route.get("file", ""),
                }
            )
            continue

        key = _route_key(method, path)
        if key in seen:
            continue
        seen.add(key)

        operation = index.get(key)
        aligned_from = ""
        if operation is not None and operation.path != path:
            # 契约是请求路径的最终依据：路由插件与 OpenAPI 若只差尾斜杠/参数写法，
            # 采用 OpenAPI 路径（这是服务实际暴露的地址）。差异进报告，不静默。
            aligned_from = path
            path = operation.path

        profile: dict[str, Any] = {
            "endpoint": f"{method} {path}",
            "method": method,
            "path": path,
            "handler": route.get("handler", ""),
            "file": route.get("file", ""),
            "route_source": route.get("source", "framework_plugin"),
            "route_confidence": route.get("confidence", "verified"),
            "path_aligned_from": aligned_from,
            "contract_incomplete": operation is None,
            "params": [],
            "request_body": {},
            "body_required": [],
            "success_codes": [200],
            "response_fields": {},
            "downstream_deps": [],
            "downstream_unmapped": False,
            "source": "openapi" if operation else "route",
        }

        if operation is not None:
            profile.update(
                {
                    "operation_id": operation.operation_id,
                    "summary": operation.summary,
                    "params": [p.as_dict() for p in operation.params],
                    "request_body": operation.body_schema,
                    "body_required": list(operation.body_required),
                    "success_codes": list(operation.success_codes),
                    "response_fields": {
                        str(code): fields for code, fields in operation.response_fields.items()
                    },
                }
            )
        else:
            unmapped.append(
                {
                    "endpoint": f"{method} {path}",
                    "handler": route.get("handler", ""),
                    "reason": "openapi_operation_missing",
                    "file": route.get("file", ""),
                }
            )

        handler = profile["handler"]
        if codegraph_client is not None and handler:
            try:
                callees = codegraph_client.callees(handler)[:max_callees]
            except Exception as exc:  # 图谱是辅助证据，单次失败不中断画像
                profile["codegraph_error"] = str(exc)[:300]
                callees = []
            deps = []
            for symbol in callees:
                deps.append(
                    {
                        "dep_id": getattr(symbol, "name", str(symbol)),
                        "dep_type": _classify_dep(getattr(symbol, "name", str(symbol))),
                        "evidence": "inferred",
                    }
                )
            profile["downstream_deps"] = deps
            profile["downstream_unmapped"] = not deps

        profiles.append(profile)

    return profiles, unmapped
