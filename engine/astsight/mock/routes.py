"""HTTP Mock 路由表：`/_dep/<route_key>`、Host 组、唯一 method+path 兜底。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from contracts import MockRoute, MockRouteMatch, MockStub

from .identity import (
    DepIdError,
    DepIdEntry,
    assign_route_keys,
    normalize_method,
    normalize_path_key,
)


@dataclass
class RouteEntry:
    route: MockRoute
    branches: dict[str, MockStub] = field(default_factory=dict)
    _pattern: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.route.method = normalize_method(self.route.method)
        self.route.path_key = normalize_path_key(self.route.path_key)
        self.route.host = _normalize_route_host(self.route.host)
        self._pattern = _compile_path(self.route.path_key)

    def matches_path(self, path: str) -> bool:
        return bool(self._pattern.match(path))

    def branch(self, name: str) -> MockStub | None:
        return self.branches.get(name)


@dataclass
class RouteResolution:
    entry: RouteEntry | None = None
    route_match: MockRouteMatch | None = None
    error: str | None = None
    candidates: list[str] = field(default_factory=list)


def _normalize_route_host(host: str | None) -> str | None:
    if not host:
        return None
    value = host.strip().lower().rstrip(".")
    return value or None


def _compile_path(path_key: str) -> re.Pattern[str]:
    if path_key == "/":
        return re.compile(r"^/$")
    segments = path_key.strip("/").split("/")
    chunks: list[str] = []
    for segment in segments:
        literal = "{}".join(re.escape(part) for part in segment.split("{}"))
        chunks.append(literal.replace("{}", "[^/]+"))
    return re.compile("^/" + "/".join(chunks) + "$")


def _host_candidates(host_header: str | None) -> list[str]:
    if not host_header:
        return []
    value = host_header.strip().lower().rstrip(".")
    candidates = [value]
    if value.startswith("["):
        close = value.find("]")
        if close != -1:
            bare = value[: close + 1]
            if bare != value:
                candidates.append(bare)
            else:
                candidates.append(f"{bare}:80")
    elif ":" in value:
        candidates.append(value.rsplit(":", 1)[0])
    else:
        candidates.append(f"{value}:80")
    return list(dict.fromkeys(candidates))


class RouteTable:
    """不可变语义的路由表；add 只在启动前调用。"""

    def __init__(self) -> None:
        self._by_key: dict[str, RouteEntry] = {}
        self._by_dep: dict[str, RouteEntry] = {}
        self._entries: list[RouteEntry] = []

    def add_route(
        self, route: MockRoute, branches: dict[str, MockStub] | None = None
    ) -> RouteEntry:
        entry = RouteEntry(route=route, branches=dict(branches or {}))
        if entry.route.route_key in self._by_key:
            raise DepIdError(
                "mock_route_conflict", f"route_key={entry.route.route_key}"
            )
        if entry.route.dep_id in self._by_dep:
            raise DepIdError("mock_route_conflict", f"dep_id={entry.route.dep_id}")
        self._by_key[entry.route.route_key] = entry
        self._by_dep[entry.route.dep_id] = entry
        self._entries.append(entry)
        return entry

    def add_dep(
        self,
        dep: DepIdEntry,
        *,
        route_key: str,
        strategy=None,
        source: str = "profile",
        branches: dict[str, MockStub] | None = None,
    ) -> RouteEntry:
        kwargs = {
            "route_key": route_key,
            "dep_id": dep.dep_id,
            "method": dep.method,
            "path_key": dep.path_key,
            "host": dep.route_host,
            "target": dep.target,
            "source": source,
        }
        if strategy is not None:
            kwargs["strategy"] = strategy
        route = MockRoute(**kwargs)
        return self.add_route(route, branches)

    def add_deps(
        self,
        deps: list[DepIdEntry],
        branches: dict[str, dict[str, MockStub]] | None = None,
    ) -> dict[str, str]:
        """按 dep 清单批量建表，返回 dep_id → route_key。"""
        keys = assign_route_keys(deps)
        all_branches = branches or {}
        for dep in deps:
            self.add_dep(
                dep,
                route_key=keys[dep.dep_id],
                branches=all_branches.get(dep.dep_id, {}),
            )
        return keys

    def get_by_key(self, route_key: str) -> RouteEntry | None:
        return self._by_key.get(route_key)

    def get_by_dep(self, dep_id: str) -> RouteEntry | None:
        return self._by_dep.get(dep_id)

    @property
    def entries(self) -> list[RouteEntry]:
        return list(self._entries)

    @property
    def route_modes(self) -> list[MockRouteMatch]:
        modes: list[MockRouteMatch] = [MockRouteMatch.PATH_PREFIX]
        if any(entry.route.host for entry in self._entries):
            modes.append(MockRouteMatch.HOST_PATH)
        if self._entries:
            modes.append(MockRouteMatch.METHOD_PATH)
        return modes

    def resolve(
        self,
        method: str,
        path: str,
        *,
        host: str | None = None,
        direct_route_key: str | None = None,
    ) -> RouteResolution:
        if direct_route_key is not None:
            entry = self._by_key.get(direct_route_key)
            if entry is None:
                return RouteResolution(error="mock_route_unknown")
            if entry.route.method not in ("", normalize_method(method)):
                return RouteResolution(error="mock_route_unknown")
            return RouteResolution(
                entry=entry, route_match=MockRouteMatch.PATH_PREFIX
            )

        method = normalize_method(method)
        path_key = normalize_path_key(path)

        host_matches: list[RouteEntry] = []
        candidates = set(_host_candidates(host))
        if candidates:
            for entry in self._entries:
                if entry.route.host in candidates and entry.route.method == method:
                    if entry.matches_path(path_key):
                        host_matches.append(entry)
        if len(host_matches) == 1:
            return RouteResolution(
                entry=host_matches[0], route_match=MockRouteMatch.HOST_PATH
            )
        if len(host_matches) > 1:
            return RouteResolution(
                error="mock_route_ambiguous",
                candidates=[entry.route.route_key for entry in host_matches],
            )

        global_matches = [
            entry
            for entry in self._entries
            if entry.route.method == method and entry.matches_path(path_key)
        ]
        if len(global_matches) == 1:
            return RouteResolution(
                entry=global_matches[0], route_match=MockRouteMatch.METHOD_PATH
            )
        if len(global_matches) > 1:
            return RouteResolution(
                error="mock_route_ambiguous",
                candidates=[entry.route.route_key for entry in global_matches],
            )
        return RouteResolution(error="mock_route_unknown")
