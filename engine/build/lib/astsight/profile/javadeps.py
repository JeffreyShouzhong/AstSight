"""从 A 的构建文件发现 Java 依赖，并定位本地 jar。

下游 B 的 DTO/接口通常不在 A 的源码里，而在 A 依赖的 api/client jar 里。
这里只做三件事：读 pom/gradle 的坐标、在本地仓库找到 jar、列出可扫描的 jar。

不做的事：下载依赖、解析 parent pom 的完整继承链、处理 BOM 版本仲裁。
找不到就返回空，不猜路径。
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_MAVEN_NS = "{http://maven.apache.org/POM/4.0.0}"
_MAX_POM_BYTES = 2 * 1024 * 1024
_MAX_GRADLE_BYTES = 2 * 1024 * 1024

# 契约类 jar 的命名倾向：优先扫描这些，避免一上来打开几十个无关 jar。
_CONTRACT_HINT = re.compile(r"(api|client|sdk|dto|model|common|contract|feign|proto)", re.I)
_GRADLE_DEP = re.compile(
    r"""(?:implementation|api|compileOnly|runtimeOnly|compile|kapt)\s*\(?\s*
        ['"]([\w.\-]+):([\w.\-]+)(?::([^'"]+))?['"]""",
    re.X,
)


@dataclass(frozen=True)
class Dependency:
    group: str
    artifact: str
    version: str = ""
    scope: str = ""

    @property
    def coordinate(self) -> str:
        return f"{self.group}:{self.artifact}" + (f":{self.version}" if self.version else "")


def _read_text(path: Path, limit: int) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _resolve(value: str | None, properties: dict[str, str]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for _ in range(5):
        match = re.search(r"\$\{([^}]+)\}", text)
        if not match:
            break
        key = match.group(1).strip()
        replacement = properties.get(key)
        if replacement is None:
            return ""
        text = text.replace(match.group(0), replacement)
    return text


def parse_pom(path: str | Path) -> list[Dependency]:
    text = _read_text(Path(path), _MAX_POM_BYTES)
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    ns = _MAVEN_NS if root.tag.startswith("{") else ""
    properties: dict[str, str] = {}
    props_node = root.find(f"{ns}properties")
    if props_node is not None:
        for item in list(props_node):
            key = item.tag.split("}", 1)[-1]
            properties[key] = (item.text or "").strip()
    parent = root.find(f"{ns}parent")
    parent_version_raw = parent.findtext(f"{ns}version") if parent is not None else ""
    parent_group_raw = parent.findtext(f"{ns}groupId") if parent is not None else ""
    properties.setdefault("project.parent.version", _resolve(parent_version_raw, properties))
    properties.setdefault("revision", properties.get("revision", ""))
    properties.setdefault(
        "project.version", _resolve(root.findtext(f"{ns}version"), properties)
    )
    properties.setdefault(
        "project.groupId",
        _resolve(root.findtext(f"{ns}groupId"), properties)
        or _resolve(parent_group_raw, properties),
    )

    deps_node = root.find(f"{ns}dependencies")
    if deps_node is None:
        return []
    result: list[Dependency] = []
    seen: set[str] = set()
    for node in deps_node.findall(f"{ns}dependency"):
        group = _resolve(node.findtext(f"{ns}groupId"), properties)
        artifact = _resolve(node.findtext(f"{ns}artifactId"), properties)
        version = _resolve(node.findtext(f"{ns}version"), properties)
        scope = _resolve(node.findtext(f"{ns}scope"), properties) or "compile"
        if not group or not artifact:
            continue
        dep = Dependency(group=group, artifact=artifact, version=version, scope=scope)
        if dep.coordinate in seen:
            continue
        seen.add(dep.coordinate)
        result.append(dep)
    return result


def parse_gradle(path: str | Path) -> list[Dependency]:
    text = _read_text(Path(path), _MAX_GRADLE_BYTES)
    if not text:
        return []
    result: list[Dependency] = []
    seen: set[str] = set()
    for match in _GRADLE_DEP.finditer(text):
        group, artifact, version = match.group(1), match.group(2), match.group(3) or ""
        dep = Dependency(group=group, artifact=artifact, version=version)
        if dep.coordinate in seen:
            continue
        seen.add(dep.coordinate)
        result.append(dep)
    return result


def project_dependencies(project_root: str | Path) -> list[Dependency]:
    root = Path(project_root)
    deps: list[Dependency] = []
    seen: set[str] = set()
    for pom in sorted(root.rglob("pom.xml"))[:20]:
        if any(part in {"target", "build", ".git", "node_modules"} for part in pom.parts):
            continue
        for dep in parse_pom(pom):
            if dep.coordinate not in seen:
                seen.add(dep.coordinate)
                deps.append(dep)
    if not deps:
        for name in ("build.gradle", "build.gradle.kts"):
            for path in sorted(root.rglob(name))[:10]:
                for dep in parse_gradle(path):
                    if dep.coordinate not in seen:
                        seen.add(dep.coordinate)
                        deps.append(dep)
    return deps


def _maven_repo_root() -> Path | None:
    configured = os.environ.get("MAVEN_REPO_LOCAL") or os.environ.get("M2_REPO")
    if configured:
        return Path(configured).expanduser()
    home = Path.home()
    candidate = home / ".m2" / "repository"
    return candidate if candidate.is_dir() else None


def _gradle_cache_root() -> Path | None:
    home = Path.home()
    candidate = home / ".gradle" / "caches" / "modules-2" / "files-2.1"
    return candidate if candidate.is_dir() else None


def _local_jars(project_root: Path) -> list[Path]:
    jars: list[Path] = []
    for pattern in (
        "libs/*.jar",
        "lib/*.jar",
        "target/dependency/*.jar",
        "build/libs/*.jar",
        "**/libs/*.jar",
    ):
        for path in project_root.glob(pattern):
            if path.is_file() and path.suffix == ".jar":
                jars.append(path)
    return jars


def locate_jar(
    dep: Dependency,
    *,
    project_root: str | Path,
    m2_root: str | Path | None = None,
    gradle_root: str | Path | None = None,
) -> Path | None:
    root = Path(project_root)
    artifact = dep.artifact
    version = dep.version
    if version:
        for local in (
            root / "libs" / f"{artifact}-{version}.jar",
            root / "target" / "dependency" / f"{artifact}-{version}.jar",
            root / "build" / "libs" / f"{artifact}-{version}.jar",
        ):
            if local.is_file():
                return local
        m2 = Path(m2_root) if m2_root else _maven_repo_root()
        if m2 is not None:
            candidate = (
                m2
                / dep.group.replace(".", os.sep)
                / artifact
                / version
                / f"{artifact}-{version}.jar"
            )
            if candidate.is_file():
                return candidate
        gradle = Path(gradle_root) if gradle_root else _gradle_cache_root()
        if gradle is not None:
            base = gradle / dep.group / artifact / version
            if base.is_dir():
                matches = sorted(base.glob(f"*/{artifact}-{version}.jar"))
                if matches:
                    return matches[0]
    # 版本解析不出来时，按 group/artifact 在本地仓库找唯一版本。
    m2 = Path(m2_root) if m2_root else _maven_repo_root()
    if m2 is not None:
        base = m2 / dep.group.replace(".", os.sep) / artifact
        if base.is_dir():
            matches = sorted(base.glob(f"*/{artifact}-*.jar"))
            matches = [item for item in matches if not item.name.endswith("-sources.jar")]
            if len(matches) == 1:
                return matches[0]
    return None


def discover_jars(
    project_root: str | Path,
    *,
    max_jars: int = 200,
    m2_root: str | Path | None = None,
    gradle_root: str | Path | None = None,
) -> list[Path]:
    """本地可读的 jar，契约类 jar 优先。"""
    root = Path(project_root)
    ordered: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        key = str(path.resolve())
        if key in seen or not path.is_file():
            return
        seen.add(key)
        ordered.append(path)

    for jar in _local_jars(root):
        _add(jar)
    deps = project_dependencies(root)
    deps.sort(key=lambda item: 0 if _CONTRACT_HINT.search(item.artifact) else 1)
    for dep in deps:
        jar = locate_jar(
            dep,
            project_root=root,
            m2_root=m2_root,
            gradle_root=gradle_root,
        )
        if jar is not None:
            _add(jar)
        if len(ordered) >= max_jars:
            break
    return ordered[:max_jars]


__all__ = [
    "Dependency",
    "discover_jars",
    "locate_jar",
    "parse_gradle",
    "parse_pom",
    "project_dependencies",
]
