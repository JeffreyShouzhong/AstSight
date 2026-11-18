#!/usr/bin/env python3
"""构建 Linux amd64/arm64 的 `astsight-gripmock-bin` 平台 wheel。

默认读取已经下载且经 sha256 校验的官方 slim release 压缩包；传
``--download`` 时才在构建阶段联网补齐。运行期引擎不会下载二进制，也不会
查询 PATH。

用法：
    python tools/build_gripmock_wheels.py --download
    python tools/build_gripmock_wheels.py --arch amd64 --asset-dir <dir>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import stat
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "dist"
DEFAULT_ASSET_DIR = DEFAULT_OUT / "gripmock-assets"

DIST_NAME = "astsight-gripmock-bin"
IMPORT_NAME = "astsight_gripmock_bin"
VERSION = "3.23.8"
SOURCE_COMMIT = "871d06f802573d6ca731f84331a1a18b2cc15188"
RELEASE_URL = "https://github.com/bavix/gripmock/releases/download/v3.23.8"
BINARY_ENTRY = f"{IMPORT_NAME}/bin/gripmock"
MANIFEST_ENTRY = f"{IMPORT_NAME}/manifest.json"


@dataclass(frozen=True)
class Asset:
    architecture: str
    wheel_tag: str
    archive_name: str
    archive_sha256: str
    archive_size: int

    @property
    def url(self) -> str:
        return f"{RELEASE_URL}/{self.archive_name}"


ASSETS = {
    "amd64": Asset(
        architecture="amd64",
        wheel_tag="manylinux_2_17_x86_64",
        archive_name="gripmock-slim_3.23.8_linux_amd64.tar.gz",
        archive_sha256=(
            "d391140622dac4018e4bbdbacff3abe2d6cd4c3ab5520ecb047bef5db39c55b0"
        ),
        archive_size=16945483,
    ),
    "arm64": Asset(
        architecture="arm64",
        wheel_tag="manylinux_2_17_aarch64",
        archive_name="gripmock-slim_3.23.8_linux_arm64.tar.gz",
        archive_sha256=(
            "448f14bf23dd10d982438bda0ee2b4e66df80a5439ef69b0d624d0a736ca3e7b"
        ),
        archive_size=15786772,
    ),
}


class BuildError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def download_assets(asset_dir: Path, architectures: list[str]) -> None:
    asset_dir.mkdir(parents=True, exist_ok=True)
    for architecture in architectures:
        asset = ASSETS[architecture]
        target = asset_dir / asset.archive_name
        if target.is_file() and _asset_matches(target, asset):
            continue
        if target.exists():
            target.unlink()
        temporary = target.with_name(target.name + ".part")
        try:
            with urllib.request.urlopen(asset.url, timeout=60) as response:
                with temporary.open("wb") as output:
                    for chunk in iter(lambda: response.read(1 << 20), b""):
                        output.write(chunk)
            if not _asset_matches(temporary, asset):
                raise BuildError(
                    f"{asset.archive_name} 下载后 sha256 或大小不符"
                )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def _asset_matches(path: Path, asset: Asset) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == asset.archive_size
        and sha256_file(path) == asset.archive_sha256
    )


def read_release_archive(
    archive: Path,
    *,
    verify_digest: bool = True,
) -> tuple[bytes, bytes]:
    """从 release tar.gz 读取 gripmock 与 LICENSE，不落盘解压。"""
    if not archive.is_file():
        raise BuildError(f"压缩包不存在：{archive}")
    matched = next(
        (asset for asset in ASSETS.values() if asset.archive_name == archive.name),
        None,
    )
    if verify_digest:
        if matched is None:
            raise BuildError(f"无法识别压缩包平台：{archive.name}")
        if not _asset_matches(archive, matched):
            raise BuildError(f"{archive.name} sha256 或大小不符")
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            binary = _read_tar_member(bundle, "gripmock", max_size=128 << 20)
            license_text = _read_tar_member(bundle, "LICENSE", max_size=1 << 20)
    except (OSError, tarfile.TarError) as error:
        raise BuildError(f"无法读取 {archive}：{error}") from error
    return binary, license_text


def _read_tar_member(
    bundle: tarfile.TarFile,
    name: str,
    *,
    max_size: int,
) -> bytes:
    member = bundle.getmember(name)
    if not member.isfile() or member.size < 1 or member.size > max_size:
        raise BuildError(f"{name} 不是预期大小的普通文件")
    handle = bundle.extractfile(member)
    if handle is None:
        raise BuildError(f"无法读取 {name}")
    data = handle.read(max_size + 1)
    if len(data) != member.size:
        raise BuildError(f"{name} 读取不完整")
    return data


def build_manifest(
    binary: bytes,
    *,
    asset: Asset,
    version: str,
    source_commit: str,
) -> dict[str, object]:
    return {
        "schema": 1,
        "artifact": DIST_NAME,
        "version": version,
        "upstream_version": f"v{VERSION}",
        "binary": "bin/gripmock",
        "sha256": sha256_bytes(binary),
        "size": len(binary),
        "platform": "linux",
        "architecture": asset.architecture,
        "goos": "linux",
        "goarch": asset.architecture,
        "wheel_tag": asset.wheel_tag,
        "source_commit": source_commit,
        "source_url": asset.url,
    }


def build_wheel(
    binary: bytes,
    license_text: bytes,
    *,
    architecture: str,
    version: str = VERSION,
    source_commit: str = SOURCE_COMMIT,
    out_dir: Path = DEFAULT_OUT,
) -> Path:
    try:
        asset = ASSETS[architecture]
    except KeyError:
        raise BuildError(f"不支持的架构：{architecture}") from None
    if not binary:
        raise BuildError("gripmock 二进制为空")
    if not license_text:
        raise BuildError("LICENSE 为空")
    if len(source_commit) != 40 or any(
        char not in "0123456789abcdef" for char in source_commit.lower()
    ):
        raise BuildError("source_commit 必须是 40 位十六进制 commit")

    manifest = build_manifest(
        binary,
        asset=asset,
        version=version,
        source_commit=source_commit.lower(),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    wheel = (
        out_dir
        / f"{IMPORT_NAME}-{version}-py3-none-{asset.wheel_tag}.whl"
    )
    dist_info = f"{IMPORT_NAME}-{version}.dist-info"
    license_entry = f"{IMPORT_NAME}/licenses/LICENSE"
    notice_entry = f"{IMPORT_NAME}/licenses/NOTICE"
    payload: dict[str, tuple[bytes, int]] = {
        f"{IMPORT_NAME}/__init__.py": (_init_py(), 0o644),
        MANIFEST_ENTRY: (
            (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(
                "utf-8"
            ),
            0o644,
        ),
        BINARY_ENTRY: (binary, 0o755),
        license_entry: (license_text, 0o644),
        notice_entry: (_notice(), 0o644),
        f"{dist_info}/METADATA": (_metadata(version), 0o644),
        f"{dist_info}/WHEEL": (_wheel_metadata(asset.wheel_tag), 0o644),
        f"{dist_info}/licenses/LICENSE": (license_text, 0o644),
        f"{dist_info}/licenses/NOTICE": (_notice(), 0o644),
    }
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
    return wheel


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode(
        "ascii"
    )


def _write_entry(
    archive: zipfile.ZipFile,
    name: str,
    data: bytes,
    mode: int,
) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(info, data)


def _metadata(version: str) -> bytes:
    return (
        "Metadata-Version: 2.1\n"
        f"Name: {DIST_NAME}\n"
        f"Version: {version}\n"
        "Summary: AstSight bundled gripmock binary for gRPC downstream mocks\n"
        "Requires-Python: >=3.10\n"
        "License: MIT\n"
        f"Project-URL: Source, https://github.com/bavix/gripmock/tree/{SOURCE_COMMIT}\n"
        "\n"
        "随 AstSight gRPC Mock runtime 分发的固定版本 gripmock 可执行文件。\n"
    ).encode("utf-8")


def _wheel_metadata(tag: str) -> bytes:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: tools/build_gripmock_wheels.py\n"
        "Root-Is-Purelib: false\n"
        f"Tag: py3-none-{tag}\n"
    ).encode("utf-8")


def _init_py() -> bytes:
    return (
        '"""AstSight gripmock 平台发行包：二进制、清单与许可证资源。"""\n'
    ).encode("utf-8")


def _notice() -> bytes:
    return (
        "AstSight bundles gripmock as a separate executable.\n\n"
        f"gripmock version: v{VERSION}\n"
        f"source commit: {SOURCE_COMMIT}\n"
        f"source: {RELEASE_URL}\n"
        "license: MIT\n"
    ).encode("utf-8")


def _selected_architectures(raw: str) -> list[str]:
    return ["amd64", "arm64"] if raw == "all" else [raw]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="构建 astsight-gripmock-bin Linux 平台 wheels"
    )
    parser.add_argument(
        "--asset-dir",
        type=Path,
        default=DEFAULT_ASSET_DIR,
        help="预下载 release tar.gz 的目录",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--arch",
        choices=("amd64", "arm64", "all"),
        default="all",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="构建阶段下载缺失的固定 release 资产",
    )
    parser.add_argument(
        "--no-verify-digest",
        action="store_true",
        help="仅用于测试或受控离线镜像，跳过 release 压缩包校验",
    )
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    args = parser.parse_args(argv)

    architectures = _selected_architectures(args.arch)
    try:
        if args.download:
            download_assets(args.asset_dir, architectures)
        wheels = []
        for architecture in architectures:
            asset = ASSETS[architecture]
            archive = args.asset_dir / asset.archive_name
            binary, license_text = read_release_archive(
                archive,
                verify_digest=not args.no_verify_digest,
            )
            wheels.append(
                build_wheel(
                    binary,
                    license_text,
                    architecture=architecture,
                    version=args.version,
                    source_commit=args.source_commit,
                    out_dir=args.out,
                )
            )
    except (BuildError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for wheel in wheels:
        print(wheel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
