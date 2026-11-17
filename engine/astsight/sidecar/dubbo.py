"""Dubbo sidecar 的 Python 客户端。

职责边界：

- 只负责进程生命周期与 JSONL 协议，不做画像、断言或重试；
- RPC 失败（超时、无 provider、序列化错误）作为结果返回，不抛异常，
  由执行层决定归类；
- 进程崩溃、协议错、启动失败才抛 `DubboSidecarError`。

协议见 `java/astsight-sidecar-dubbo/src/main/java/io/astsight/sidecar/dubbo/SidecarServer.java`。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SIDECAR_PROTOCOL = 1
DEFAULT_TIMEOUT_MS = 5_000
MAX_TIMEOUT_MS = 600_000
_JAR_ENV = "ASTSIGHT_DUBBO_SIDECAR_JAR"
_JAVA_ENV = "ASTSIGHT_JAVA"
_DEFAULT_JAR = (
    Path(__file__).resolve().parents[3]
    / "java"
    / "astsight-sidecar-dubbo"
    / "target"
    / "astsight-dubbo-sidecar.jar"
)
# Dubbo 2.7 的 javassist / Hessian 在 JDK 9+ 需要打开这些包；
# JDK 8 不接受 --add-opens，因此按版本决定。
_ADD_OPENS = (
    "java.base/java.lang=ALL-UNNAMED",
    "java.base/java.math=ALL-UNNAMED",
    "java.base/java.util=ALL-UNNAMED",
)
_MAJOR = re.compile(r'version "(?:1\.)?(\d+)')


class DubboSidecarError(RuntimeError):
    """sidecar 无法启动、退出或协议不匹配。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DubboCallResult:
    ok: bool
    value: Any = None
    rpc_status: str = "OK"
    error: str | None = None
    exception_type: str | None = None
    message: str | None = None
    elapsed_ms: int = 0

    def raise_for_error(self) -> "DubboCallResult":
        """RPC 失败时抛出 `DubboSidecarError`，供不需要细分的调用方使用。"""
        if not self.ok:
            raise DubboSidecarError(
                self.error or "rpc_exception",
                self.message or "Dubbo 调用失败",
                detail=self.exception_type,
            )
        return self


def locate_sidecar_jar(explicit: str | Path | None = None) -> Path:
    """按 显式参数 → 环境变量 → 仓库构建产物 的顺序解析 sidecar jar。"""
    if explicit is not None:
        path = Path(explicit)
        if path.is_file():
            return path
        raise DubboSidecarError(
            "sidecar_jar_missing", f"sidecar jar 不存在：{path}"
        )

    configured = os.environ.get(_JAR_ENV, "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return path
        # 显式覆盖只认该路径，缺失不回退，避免悄悄跑旧包。
        raise DubboSidecarError(
            "sidecar_jar_missing", f"{_JAR_ENV} 指向的文件不存在：{path}"
        )

    if _DEFAULT_JAR.is_file():
        return _DEFAULT_JAR
    raise DubboSidecarError(
        "sidecar_jar_missing",
        "找不到 Dubbo sidecar jar；先执行 "
        "`cd java && mvn -pl astsight-sidecar-dubbo package`，"
        f"或设置 {_JAR_ENV}",
    )


def locate_java(explicit: str | Path | None = None) -> str:
    """按 显式参数 → `ASTSIGHT_JAVA` → `JAVA_HOME` → PATH 的顺序解析 java。"""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    configured = os.environ.get(_JAVA_ENV, "").strip()
    if configured:
        candidates.append(Path(configured))
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        candidates.append(Path(java_home) / "bin" / _java_name())

    for candidate in candidates:
        resolved = _java_executable(candidate)
        if resolved is not None:
            return str(resolved)
    found = shutil.which("java")
    if found:
        return found
    raise DubboSidecarError(
        "sidecar_java_missing",
        "找不到 java；设置 JAVA_HOME / ASTSIGHT_JAVA，或把 java 放进 PATH",
    )


class DubboSidecar:
    """管理一个 sidecar 进程；一个进程可连续调用多个服务。"""

    def __init__(
        self,
        *,
        jar: str | Path | None = None,
        java: str | Path | None = None,
        command: Sequence[str] | None = None,
        startup_timeout: float = 15.0,
        stderr_tail: int = 50,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._jar = jar
        self._java = java
        self._command = list(command) if command is not None else None
        self._startup_timeout = startup_timeout
        self._env = dict(env) if env is not None else None
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=stderr_tail)
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._counter = 0
        self._lock = threading.Lock()
        self._info: dict[str, Any] = {}

    # ------------------------------------------------------------ 生命周期

    def start(self) -> "DubboSidecar":
        if self._process is not None:
            return self
        command = self._command or self._default_command()
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self._env,
            )
        except OSError as error:
            raise DubboSidecarError(
                "sidecar_start_failed",
                f"无法启动 sidecar：{error}",
                detail=" ".join(command),
            ) from error

        self._reader = threading.Thread(
            target=self._read_stdout, name="astsight-sidecar-out", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name="astsight-sidecar-err", daemon=True
        )
        self._stderr_reader.start()

        try:
            self._info = self.ping(timeout=self._startup_timeout)
        except Exception:
            self.close()
            raise
        protocol = self._info.get("protocol")
        if protocol != SIDECAR_PROTOCOL:
            self.close()
            raise DubboSidecarError(
                "sidecar_protocol_mismatch",
                f"sidecar 协议号 {protocol} 与引擎期望的 {SIDECAR_PROTOCOL} 不一致",
                detail=self._stderr_tail(),
            )
        return self

    @property
    def info(self) -> dict[str, Any]:
        return dict(self._info)

    def ping(self, *, timeout: float | None = None) -> dict[str, Any]:
        response = self._request({"op": "ping"}, timeout=timeout)
        if not response.get("ok"):
            raise DubboSidecarError(
                "sidecar_ping_failed",
                str(response.get("message") or response.get("error") or "ping 失败"),
                detail=self._stderr_tail(),
            )
        return response

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        # 丢弃失败请求留下的迟到响应，避免 shutdown 等到 id 不匹配。
        while True:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                break
        try:
            if process.poll() is None:
                self._request({"op": "shutdown"}, timeout=2.0)
        except Exception:
            pass
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        for thread in (self._reader, self._stderr_reader):
            if thread is not None:
                thread.join(timeout=2)
        self._process = None

    def __enter__(self) -> "DubboSidecar":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -------------------------------------------------------------- 调用

    def invoke(
        self,
        interface: str,
        method: str,
        param_types: Sequence[str],
        arguments: Sequence[Any] = (),
        *,
        url: str,
        group: str | None = None,
        version: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        attachments: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> DubboCallResult:
        """调用一次 Dubbo 方法。RPC 失败作为结果返回，进程故障才抛异常。"""
        if not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
            raise ValueError(f"timeout_ms 必须在 1..{MAX_TIMEOUT_MS} 之间")
        payload: dict[str, Any] = {
            "op": "invoke",
            "interface": interface,
            "method": method,
            "param_types": list(param_types),
            "arguments": list(arguments),
            "url": url,
            "timeout_ms": timeout_ms,
        }
        if group:
            payload["group"] = group
        if version:
            payload["version"] = version
        if attachments:
            payload["attachments"] = dict(attachments)

        # sidecar 自己按 timeout_ms 收口；这里多留启动/序列化余量。
        response = self._request(
            payload,
            timeout=timeout if timeout is not None else timeout_ms / 1000.0 + 10.0,
        )
        return DubboCallResult(
            ok=bool(response.get("ok")),
            value=response.get("value"),
            rpc_status=str(response.get("rpc_status") or "OK"),
            error=response.get("error"),
            exception_type=response.get("exception_type"),
            message=response.get("message"),
            elapsed_ms=int(response.get("elapsed_ms") or 0),
        )

    # -------------------------------------------------------------- 内部

    def _default_command(self) -> list[str]:
        java = locate_java(self._java)
        jar = locate_sidecar_jar(self._jar)
        command = [java]
        if _java_major(java) >= 9:
            for opens in _ADD_OPENS:
                command.append(f"--add-opens={opens}")
        command.extend(["-jar", str(jar)])
        return command

    def _request(
        self, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        process = self._process
        if process is None or process.poll() is not None:
            raise DubboSidecarError(
                "sidecar_not_running",
                "sidecar 未启动或已退出",
                detail=self._stderr_tail(),
            )
        limit = self._startup_timeout if timeout is None else timeout
        with self._lock:
            self._counter += 1
            request_id = str(self._counter)
            body = dict(payload)
            body["id"] = request_id
            try:
                assert process.stdin is not None
                process.stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as error:
                raise DubboSidecarError(
                    "sidecar_write_failed",
                    f"写入 sidecar 失败：{error}",
                    detail=self._stderr_tail(),
                ) from error

            deadline = time.monotonic() + limit
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DubboSidecarError(
                        "sidecar_timeout",
                        f"等待 sidecar 响应超时（{limit:.1f}s）",
                        detail=self._stderr_tail(),
                    )
                try:
                    response = self._responses.get(timeout=remaining)
                except queue.Empty:
                    continue
                if response.get("_closed"):
                    raise DubboSidecarError(
                        "sidecar_exited",
                        "sidecar 在响应前退出",
                        detail=self._stderr_tail(),
                    )
                if response.get("_protocol_error"):
                    raise DubboSidecarError(
                        "sidecar_protocol_error",
                        "sidecar 输出了非 JSON 内容",
                        detail=str(response.get("_raw"))[:2000],
                    )
                if response.get("id") != request_id:
                    raise DubboSidecarError(
                        "sidecar_protocol_error",
                        f"响应 id {response.get('id')} 与请求 {request_id} 不一致",
                    )
                return response

    def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            for line in process.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    self._responses.put(
                        {"_protocol_error": True, "_raw": text}
                    )
                    continue
                if isinstance(value, dict):
                    self._responses.put(value)
                else:
                    self._responses.put(
                        {"_protocol_error": True, "_raw": text}
                    )
        finally:
            self._responses.put({"_closed": True})

    def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        for line in process.stderr:
            self._stderr.append(line.rstrip())

    def _stderr_tail(self) -> str:
        return "\n".join(self._stderr)


def _java_name() -> str:
    return "java.exe" if os.name == "nt" else "java"


def _java_executable(path: Path) -> Path | None:
    if path.is_file():
        return path
    if path.is_dir():
        candidate = path / "bin" / _java_name()
        if candidate.is_file():
            return candidate
    return None


def _java_major(java: str) -> int:
    try:
        completed = subprocess.run(
            [java, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    text = completed.stderr or completed.stdout
    match = _MAJOR.search(text)
    return int(match.group(1)) if match else 0
