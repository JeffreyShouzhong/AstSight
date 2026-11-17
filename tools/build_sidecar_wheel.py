#!/usr/bin/env python3
"""把 Dubbo sidecar jar 打成 `astsight-dubbo-sidecar` wheel。

wheel 内包含 `manifest.json`（版本、协议号、sha256、大小），引擎在启动
sidecar 前逐项校验；源码树里的 `mvn package` 产物也会写同名
`.manifest.json`。

用法：
    python tools/build_sidecar_wheel.py              # mvn package + 打包
    python tools/build_sidecar_wheel.py --skip-build # 复用已有 jar
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
JAVA_DIR = REPO / "java"
POM = JAVA_DIR / "astsight-sidecar-dubbo" / "pom.xml"
DEFAULT_JAR = JAVA_DIR / "astsight-sidecar-dubbo" / "target" / "astsight-dubbo-sidecar.jar"
DEFAULT_OUT = REPO / "dist"

DIST_NAME = "astsight-dubbo-sidecar"
IMPORT_NAME = "astsight_dubbo_sidecar"
PROTOCOL = 1
MAIN_CLASS = "io.astsight.sidecar.dubbo.SidecarMain"
JAR_ENTRY = f"{IMPORT_NAME}/sidecar/astsight-dubbo-sidecar.jar"
MANIFEST_ENTRY = f"{IMPORT_NAME}/manifest.json"
_POM_NS = {"m": "http://maven.apache.org/POM/4.0.0"}


class BuildError(RuntimeError):
    pass


def normalize_version(raw: str) -> str:
    text = raw.strip()
    if not text.endswith("-SNAPSHOT"):
        return text
    parts = text[: -len("-SNAPSHOT")].split(".")
    if all(part.isdigit() for part in parts) and len(parts) < 3:
        parts += ["0"] * (3 - len(parts))
    return ".".join(parts) + ".dev0"


def maven_version(pom: Path = POM) -> str:
    root = ET.parse(pom).getroot()
    parent = root.find("m:parent/m:version", namespaces=_POM_NS)
    version = root.findtext("m:version", namespaces=_POM_NS) or (
        parent.text if parent is not None else None
    )
    if not version:
        raise BuildError(f"无法从 {pom} 读取 <version>")
    return version.strip()


def jar_manifest_version(jar: Path) -> str:
    try:
        with zipfile.ZipFile(jar) as archive:
            raw = archive.read("META-INF/MANIFEST.MF")
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise BuildError(f"无法读取 jar manifest：{jar}（{error}）") from error
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.lower().startswith("implementation-version:"):
            return line.split(":", 1)[1].strip()
    raise BuildError(f"jar manifest 缺少 Implementation-Version：{jar}")


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_manifest(jar: Path, *, version: str) -> dict[str, object]:
    jar_version = jar_manifest_version(jar)
    return {
        "schema": 1,
        "artifact": DIST_NAME,
        "version": version,
        "jar_version": jar_version,
        "protocol": PROTOCOL,
        "main_class": MAIN_CLASS,
        "jar": "sidecar/astsight-dubbo-sidecar.jar",
        "sha256": sha256_file(jar),
        "size": jar.stat().st_size,
    }


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _write_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def _metadata(version: str) -> bytes:
    return (
        "Metadata-Version: 2.1\n"
        f"Name: {DIST_NAME}\n"
        f"Version: {version}\n"
        "Summary: AstSight Dubbo sidecar jar (generic invoke client)\n"
        "Requires-Python: >=3.10\n"
        "\n"
        "把 AstSight Dubbo sidecar 的 shaded jar 与其校验清单一起随引擎分发。\n"
    ).encode("utf-8")


def _wheel_metadata() -> bytes:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: tools/build_sidecar_wheel.py\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")


def _init_py() -> bytes:
    return (
        '"""AstSight Dubbo sidecar 发行包：jar 与 manifest.json 只作为资源分发。"""\n'
    ).encode("utf-8")


def build_wheel(
    jar: Path,
    *,
    version: str | None = None,
    out_dir: Path = DEFAULT_OUT,
) -> Path:
    if not jar.is_file():
        raise BuildError(f"jar 不存在：{jar}")
    resolved = version or normalize_version(jar_manifest_version(jar))
    manifest = build_manifest(jar, version=resolved)
    out_dir.mkdir(parents=True, exist_ok=True)
    wheel = out_dir / f"{IMPORT_NAME}-{resolved}-py3-none-any.whl"
    dist_info = f"{IMPORT_NAME}-{resolved}.dist-info"
    payload = {
        f"{IMPORT_NAME}/__init__.py": _init_py(),
        MANIFEST_ENTRY: (
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8"),
        JAR_ENTRY: jar.read_bytes(),
        f"{dist_info}/METADATA": _metadata(resolved),
        f"{dist_info}/WHEEL": _wheel_metadata(),
    }
    record = [
        f"{name},{_record_hash(data)},{len(data)}" for name, data in payload.items()
    ]
    record.append(f"{dist_info}/RECORD,,")
    payload[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode("utf-8")
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in payload.items():
            _write_entry(archive, name, data)
    sidecar = jar.with_name(jar.name + ".manifest.json")
    sidecar.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return wheel


def run_maven() -> None:
    mvn = "mvn.cmd" if sys.platform == "win32" else "mvn"
    command = [mvn, "-q", "-DskipTests", "-pl", "astsight-sidecar-dubbo", "package"]
    result = subprocess.run(command, cwd=JAVA_DIR, check=False)
    if result.returncode != 0:
        raise BuildError(f"Maven 构建失败（exit={result.returncode}）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 astsight-dubbo-sidecar wheel")
    parser.add_argument("--jar", type=Path, default=DEFAULT_JAR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--version", help="覆盖 wheel/清单版本（默认由 jar 版本归一）")
    parser.add_argument("--skip-build", action="store_true", help="不执行 mvn package")
    args = parser.parse_args(argv)
    try:
        if not args.skip_build:
            run_maven()
        if args.version:
            version = args.version
        else:
            version = normalize_version(jar_manifest_version(args.jar))
        wheel = build_wheel(args.jar, version=version, out_dir=args.out)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(wheel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
