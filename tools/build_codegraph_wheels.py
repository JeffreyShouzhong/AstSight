#!/usr/bin/env python3
"""构建 `astsight-codegraph-bin` 平台 wheel。

wheel 内自带 CodeGraph 平台包（含 Node 运行时），安装后引擎直接从包资源
解析 Node 与 node_modules，不依赖 PATH、npm、联网或用户级缓存。

用法：
    python tools/build_codegraph_wheels.py --platform win32-x64
    python tools/build_codegraph_wheels.py --platform linux-x64 --out dist
    python tools/build_codegraph_wheels.py --platform win32-x64 \
        --from-root <已有的 node_modules 目录>

跨平台构建通过 npm registry 下载主包与对应平台包，不执行安装脚本。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform as host_platform
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "dist"
DIST_NAME = "astsight-codegraph-bin"
IMPORT_NAME = "astsight_codegraph_bin"
CODEGRAPH_PACKAGE = "@colbymchenry/codegraph"
CODEGRAPH_VERSION = "1.6.2"
REGISTRY = "https://registry.npmjs.org"

PLATFORMS = {
    "linux-x64": {
        "wheel_tag": "manylinux_2_17_x86_64",
        "npm_os": "linux",
        "npm_cpu": "x64",
    },
    "linux-arm64": {
        "wheel_tag": "manylinux_2_17_aarch64",
        "npm_os": "linux",
        "npm_cpu": "arm64",
    },
    "darwin-x64": {
        "wheel_tag": "macosx_11_0_x86_64",
        "npm_os": "darwin",
        "npm_cpu": "x64",
    },
    "darwin-arm64": {
        "wheel_tag": "macosx_11_0_arm64",
        "npm_os": "darwin",
        "npm_cpu": "arm64",
    },
    "win32-x64": {
        "wheel_tag": "win_amd64",
        "npm_os": "win32",
        "npm_cpu": "x64",
    },
}
NODE_CANDIDATES = ("node", "node.exe", "bin/node", "bin/node.exe")


class BuildError(RuntimeError):
    pass


def _current_platform() -> str:
    system = {"win32": "win32", "linux": "linux", "darwin": "darwin"}.get(
        sys.platform, sys.platform
    )
    machine = host_platform.machine().lower()
    cpu = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    return f"{system}-{cpu}"


def _download(url: str, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=180) as response:
        data = response.read()
    target.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _tarball_url(package: str, version: str) -> str:
    short = package.rsplit("/", 1)[-1]
    return f"{REGISTRY}/{package}/-/{short}-{version}.tgz"


def _extract_package(tarball: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name
            if not name.startswith("package/"):
                continue
            relative = name[len("package/"):]
            if not relative:
                continue
            destination = target / relative
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    continue
                destination.write_bytes(source.read())
                mode = 0o755 if member.mode & 0o111 else 0o644
                destination.chmod(mode)


def _stage(
    asset_dir: Path,
    *,
    platform_key: str,
    from_root: Path | None = None,
) -> tuple[Path, dict]:
    spec = PLATFORMS[platform_key]
    work = Path(tempfile.mkdtemp(prefix="astsight-codegraph-"))
    package_root = work / IMPORT_NAME
    vendor = package_root / "node_modules" / "@colbymchenry"
    platform_package = (
        f"codegraph-{spec['npm_os']}-{spec['npm_cpu']}"
    )
    hashes: dict[str, str] = {}
    if from_root is not None:
        source = Path(from_root)
        main_source = source / "@colbymchenry" / "codegraph"
        platform_source = source / "@colbymchenry" / platform_package
        if not main_source.is_dir() or not platform_source.is_dir():
            raise BuildError(
                f"{source} 下缺少 codegraph 或 {platform_package}"
            )
        shutil.copytree(main_source, vendor / "codegraph")
        shutil.copytree(platform_source, vendor / platform_package)
    else:
        asset_dir.mkdir(parents=True, exist_ok=True)
        main_tarball = asset_dir / f"codegraph-{CODEGRAPH_VERSION}.tgz"
        platform_tarball = asset_dir / f"{platform_package}-{CODEGRAPH_VERSION}.tgz"
        hashes["codegraph"] = _download(
            _tarball_url(CODEGRAPH_PACKAGE, CODEGRAPH_VERSION),
            main_tarball,
        )
        hashes[platform_package] = _download(
            _tarball_url(
                f"@colbymchenry/{platform_package}", CODEGRAPH_VERSION
            ),
            platform_tarball,
        )
        _extract_package(main_tarball, vendor / "codegraph")
        _extract_package(platform_tarball, vendor / platform_package)

    node_path = None
    for candidate in NODE_CANDIDATES:
        path = vendor / platform_package / candidate
        if path.is_file():
            node_path = path
            break
    if node_path is None:
        raise BuildError(f"{platform_package} 内找不到 Node 可执行文件")
    node_path.chmod(0o755)
    node_entry = node_path.relative_to(package_root).as_posix()
    manifest = {
        "distribution": DIST_NAME,
        "version": CODEGRAPH_VERSION,
        "codegraph_version": CODEGRAPH_VERSION,
        "platform": platform_key,
        "wheel_tag": spec["wheel_tag"],
        "node_entry": node_entry,
        "module_root": "node_modules",
        "source": REGISTRY,
        "tarballs": hashes,
    }
    (package_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (package_root / "__init__.py").write_text(
        '"""AstSight CodeGraph 平台发行包：Node 运行时与 CodeGraph 库。"""\n',
        encoding="utf-8",
    )
    licenses = package_root / "licenses"
    licenses.mkdir()
    (licenses / "NOTICE").write_text(
        "AstSight bundles CodeGraph and its self-contained Node runtime.\n\n"
        f"CodeGraph: {CODEGRAPH_PACKAGE}@{CODEGRAPH_VERSION}\n"
        f"Registry: {REGISTRY}\n"
        "License: MIT\n",
        encoding="utf-8",
    )
    return package_root, manifest


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode(
        "ascii"
    )


def _write_entry(
    archive: zipfile.ZipFile, name: str, data: bytes, mode: int
) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(info, data)


def _metadata(version: str, platform_key: str) -> bytes:
    return (
        "Metadata-Version: 2.1\n"
        f"Name: {DIST_NAME}\n"
        f"Version: {version}\n"
        "Summary: AstSight bundled CodeGraph runtime and Node executable\n"
        "Requires-Python: >=3.11\n"
        "License: MIT\n"
        "\n"
        "随 AstSight 分发的固定版本 CodeGraph 与自包含 Node 运行时；"
        f"平台 {platform_key}。\n"
    ).encode("utf-8")


def _wheel_metadata(tag: str) -> bytes:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: tools/build_codegraph_wheels.py\n"
        "Root-Is-Purelib: false\n"
        f"Tag: py3-none-{tag}\n"
    ).encode("utf-8")


def _build_wheel(
    package_root: Path,
    manifest: dict,
    *,
    out: Path,
    max_bytes: int,
) -> Path:
    version = str(manifest["version"])
    tag = str(manifest["wheel_tag"])
    out.mkdir(parents=True, exist_ok=True)
    wheel = out / f"{IMPORT_NAME}-{version}-py3-none-{tag}.whl"
    dist_info = f"{IMPORT_NAME}-{version}.dist-info"
    payload: dict[str, tuple[bytes, int]] = {}
    for path in sorted(package_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(package_root.parent).as_posix()
        mode = 0o755 if os.access(path, os.X_OK) else 0o644
        payload[relative] = (path.read_bytes(), mode)
    payload[f"{dist_info}/METADATA"] = (
        _metadata(version, str(manifest["platform"])),
        0o644,
    )
    payload[f"{dist_info}/WHEEL"] = (_wheel_metadata(tag), 0o644)
    record = [
        f"{name},{_record_hash(data)},{len(data)}"
        for name, (data, _mode) in payload.items()
    ]
    record.append(f"{dist_info}/RECORD,,")
    payload[f"{dist_info}/RECORD"] = (
        ("\n".join(record) + "\n").encode("utf-8"),
        0o644,
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, (data, mode) in payload.items():
            _write_entry(archive, name, data, mode)
    size = wheel.stat().st_size
    if size > max_bytes:
        raise BuildError(
            f"wheel 大小 {size} 超过限制 {max_bytes}；需要拆分或裁剪依赖"
        )
    return wheel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="构建 astsight-codegraph-bin 平台 wheel"
    )
    parser.add_argument(
        "--platform",
        choices=sorted(PLATFORMS),
        default=_current_platform(),
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--asset-dir", type=Path, default=DEFAULT_OUT / "codegraph-assets"
    )
    parser.add_argument(
        "--from-root",
        type=Path,
        default=None,
        help="复用已有的 node_modules 目录（跳过下载）",
    )
    parser.add_argument(
        "--max-bytes", type=int, default=100 * 1024 * 1024
    )
    args = parser.parse_args(argv)
    if args.platform not in PLATFORMS:
        print(f"error: 不支持的平台 {args.platform}", file=sys.stderr)
        return 1
    try:
        package_root, manifest = _stage(
            args.asset_dir,
            platform_key=args.platform,
            from_root=args.from_root,
        )
        wheel = _build_wheel(
            package_root,
            manifest,
            out=args.out,
            max_bytes=args.max_bytes,
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(wheel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
