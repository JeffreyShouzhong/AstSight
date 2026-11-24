#!/usr/bin/env python3
"""构建 AstSight 离线安装包。

在联网且与目标环境相同平台/Python 版本的机器上运行：

    python tools/build_offline_bundle.py

产出 `dist/offline/astsight-offline-<version>-py<xy>-<platform>.zip`，包含：
- astsight 及其全部依赖 wheel；
- 对应平台的 astsight-codegraph-bin wheel；
- 可选的 Java agent / Dubbo sidecar wheel；
- SHA256SUMS、install.sh、install.ps1、README。

内网已有 wheel 时可用 `--wheels-dir` 跳过下载。
"""
from __future__ import annotations

import argparse
import hashlib
import platform as host_platform
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "dist" / "offline"
JAVA_WHEELS = (
    ("astsight_java_agent", "astsight-java-agent"),
    ("astsight_dubbo_sidecar", "astsight-dubbo-sidecar"),
)


class BuildError(RuntimeError):
    pass


def _platform_label() -> str:
    system = {"win32": "win32", "linux": "linux", "darwin": "darwin"}.get(
        sys.platform, sys.platform
    )
    machine = host_platform.machine().lower()
    cpu = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    return f"{system}-{cpu}"


def _project_version() -> str:
    payload = tomllib.loads(
        (REPO / "engine" / "pyproject.toml").read_text(encoding="utf-8")
    )
    return str(payload["project"]["version"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(version: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        f"astsight=={version}",
        "--dest",
        str(target),
        "--only-binary=:all:",
        "--disable-pip-version-check",
    ]
    result = subprocess.run(command, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise BuildError(
            "pip download 失败；确认当前平台/Python 有可用的 wheel，"
            "或改用 --wheels-dir 提供已下载的 wheel"
        )


def _copy_wheels(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    wheels = sorted(source.glob("*.whl"))
    if not wheels:
        raise BuildError(f"{source} 下没有 wheel")
    for wheel in wheels:
        shutil.copy2(wheel, target / wheel.name)


def _collect_java_wheels(
    target: Path, explicit: list[Path]
) -> list[Path]:
    collected: list[Path] = []
    for path in explicit:
        if not path.is_file():
            raise BuildError(f"Java wheel 不存在: {path}")
        shutil.copy2(path, target / path.name)
        collected.append(target / path.name)
    for prefix, _dist_name in JAVA_WHEELS:
        if any(path.name.startswith(prefix + "-") for path in collected):
            continue
        candidates = sorted((REPO / "dist").glob(f"{prefix}-*.whl"))
        if candidates:
            latest = candidates[-1]
            shutil.copy2(latest, target / latest.name)
            collected.append(target / latest.name)
    return collected


def _require(name: str, wheels: list[Path]) -> None:
    if not any(path.name.startswith(name) for path in wheels):
        raise BuildError(f"离线包缺少 {name} wheel；检查平台/Python 版本")


def _write_metadata(
    bundle: Path,
    wheels: Path,
    *,
    version: str,
    platform_label: str,
    python_tag: str,
) -> None:
    wheel_files = sorted(wheels.glob("*.whl"))
    sums = "".join(
        f"{_sha256(path)}  {path.name}\n" for path in wheel_files
    )
    (bundle / "SHA256SUMS").write_text(sums, encoding="utf-8")

    install_sh = bundle / "install.sh"
    install_sh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'pip install --no-index --find-links "$(dirname "$0")/wheels" astsight=={version}\n',
        encoding="utf-8",
    )
    install_sh.chmod(0o755)
    (bundle / "install.ps1").write_text(
        "pip install --no-index --find-links "
        f'"$PSScriptRoot\\wheels" astsight=={version}\n',
        encoding="utf-8",
    )
    (bundle / "README.md").write_text(
        "# AstSight 离线安装包\n\n"
        f"- AstSight 版本：`{version}`\n"
        f"- 目标平台：`{platform_label}`\n"
        f"- Python wheel 标签：`{python_tag}`\n\n"
        "安装：\n\n"
        "```bash\n"
        "./install.sh\n"
        "```\n\n"
        "Windows：\n\n"
        "```powershell\n"
        ".\\install.ps1\n"
        "```\n\n"
        "校验（可选）：\n\n"
        "```bash\n"
        "sha256sum -c SHA256SUMS\n"
        "```\n\n"
        "Java 项目如不允许外部 jar，可设置：\n\n"
        "```bash\n"
        "export ASTSIGHT_JAVA_AGENT_JAR=/opt/approved/astsight-agent.jar\n"
        "export ASTSIGHT_DUBBO_SIDECAR_JAR=/opt/approved/astsight-dubbo-sidecar.jar\n"
        "```\n\n"
        "如果禁止加载 agent，HTTP 下游 Mock 仍可用 `url_override` / "
        "`discovery_override`；MQ 生产者拦截和 Dubbo Filter 必须加载 agent。\n",
        encoding="utf-8",
    )


def _zip(bundle: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(bundle.parent).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            mode = 0o755 if path.name.endswith(".sh") else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, path.read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 AstSight 离线安装包")
    parser.add_argument("--version", default=None)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--wheels-dir",
        type=Path,
        default=None,
        help="已有 wheel 目录；不提供则执行 pip download",
    )
    parser.add_argument(
        "--agent-wheel",
        type=Path,
        action="append",
        default=[],
        help="额外的 Java agent wheel（可重复）",
    )
    parser.add_argument(
        "--sidecar-wheel",
        type=Path,
        action="append",
        default=[],
        help="额外的 Dubbo sidecar wheel（可重复）",
    )
    parser.add_argument(
        "--skip-java", action="store_true", help="不包含 Java agent/sidecar"
    )
    parser.add_argument("--keep", action="store_true", help="保留解包目录")
    args = parser.parse_args(argv)

    version = args.version or _project_version()
    platform_label = _platform_label()
    python_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    bundle_name = f"astsight-offline-{version}-{python_tag}-{platform_label}"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="astsight-offline-"))
    bundle = work / bundle_name
    wheels = bundle / "wheels"
    wheel_count = 0
    try:
        if args.wheels_dir:
            _copy_wheels(args.wheels_dir, wheels)
        else:
            _download(version, wheels)
        wheel_files = sorted(wheels.glob("*.whl"))
        _require("astsight-", wheel_files)
        _require("astsight_codegraph_bin-", wheel_files)
        if not args.skip_java:
            java_wheels = _collect_java_wheels(
                wheels, list(args.agent_wheel) + list(args.sidecar_wheel)
            )
            if not java_wheels:
                print(
                    "warning: 未找到 Java agent/sidecar wheel；离线包不含 Java 注入。"
                    "可用 --agent-wheel/--sidecar-wheel 指定内网构建产物。",
                    file=sys.stderr,
                )
        wheel_count = len(sorted(wheels.glob("*.whl")))
        _write_metadata(
            bundle,
            wheels,
            version=version,
            platform_label=platform_label,
            python_tag=python_tag,
        )
        target = args.out_dir / f"{bundle_name}.zip"
        _zip(bundle, target)
    except (BuildError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    print(target)
    print(f"files={wheel_count} size={target.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
