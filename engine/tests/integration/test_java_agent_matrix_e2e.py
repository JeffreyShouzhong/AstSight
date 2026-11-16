"""Java agent 版本矩阵：JDK × 客户端版本，验证 HTTPS → 明文 Mock 改写。

默认跳过。启用条件：

- `ASTSIGHT_JAVA_AGENT_MATRIX_E2E=1`
- 已构建 `java/astsight-agent/target/astsight-agent.jar`
- `ASTSIGHT_JAVA_AGENT_MATRIX_JDKS` 指向一个或多个 JDK 家目录，用 `;` 或 `,`
  分隔；未设置时退回 `ASTSIGHT_JAVA_AGENT_E2E_JAVA` / `JAVA_HOME` / PATH
- 首次运行需要 Maven 与网络下载矩阵依赖；之后走
  `~/.astsight/java-agent-matrix/` 缓存
- Maven 需要 JDK 17+：用 `ASTSIGHT_MAVEN_JAVA_HOME` 指定，未指定时取矩阵中
  版本最高的 JDK

覆盖：

- OkHttp 3.14.9 / 4.12.0 / 5.5.1
- Feign 10.12 / 11.10 / 12.5 / 13.5
- Apache HttpClient 4.3.6 / 4.4.1 / 4.5.14
- Apache HttpClient 5.0 / 5.1 / 5.2.3 / 5.3.1 / 5.4.1 / 5.6.5

每个组合都会编译对应样例、带 `-javaagent` 运行，并核对 Mock 证据里的
`adapter` / `mode` / `original_host`。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from astsight.mock import DepIdEntry, MockHttpServer, assign_route_keys
from contracts import MockStub

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_JAVA_AGENT_MATRIX_E2E") != "1",
    reason="Java agent 版本矩阵默认跳过：设置 ASTSIGHT_JAVA_AGENT_MATRIX_E2E=1 启用",
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "matrix-e2e-token"
DEP_ID = "http:host:api.example.com GET /orders/{}"
CACHE = Path.home() / ".astsight" / "java-agent-matrix"


@dataclass(frozen=True)
class Case:
    id: str
    sample: str
    main: str
    adapter: str
    coords: tuple[str, ...]
    args: tuple[str, ...]
    original_path: str


CASES: tuple[Case, ...] = (
    Case(
        "okhttp-3.14.9",
        "OkHttpSample.java",
        "OkHttpSample",
        "okhttp",
        ("com.squareup.okhttp3:okhttp:3.14.9",),
        ("https://api.example.com/orders/61?trace=matrix",),
        "/orders/61",
    ),
    Case(
        "okhttp-4.12.0",
        "OkHttpSample.java",
        "OkHttpSample",
        "okhttp",
        ("com.squareup.okhttp3:okhttp:4.12.0",),
        ("https://api.example.com/orders/62?trace=matrix",),
        "/orders/62",
    ),
    Case(
        "okhttp-5.5.1",
        "OkHttpSample.java",
        "OkHttpSample",
        "okhttp",
        (
            "com.squareup.okhttp3:okhttp-jvm:5.5.1",
            "com.squareup.okio:okio-jvm:3.18.1",
            "org.jetbrains.kotlin:kotlin-stdlib:2.1.21",
        ),
        ("https://api.example.com/orders/63?trace=matrix",),
        "/orders/63",
    ),
    Case(
        "feign-10.12",
        "FeignSample.java",
        "FeignSample",
        "feign",
        ("io.github.openfeign:feign-core:10.12",),
        ("https://api.example.com",),
        "/orders/43",
    ),
    Case(
        "feign-11.10",
        "FeignSample.java",
        "FeignSample",
        "feign",
        ("io.github.openfeign:feign-core:11.10",),
        ("https://api.example.com",),
        "/orders/43",
    ),
    Case(
        "feign-12.5",
        "FeignSample.java",
        "FeignSample",
        "feign",
        ("io.github.openfeign:feign-core:12.5",),
        ("https://api.example.com",),
        "/orders/43",
    ),
    Case(
        "feign-13.5",
        "FeignSample.java",
        "FeignSample",
        "feign",
        ("io.github.openfeign:feign-core:13.5",),
        ("https://api.example.com",),
        "/orders/43",
    ),
    Case(
        "hc4-4.3.6",
        "ApacheHttpClient4Sample.java",
        "ApacheHttpClient4Sample",
        "httpclient4",
        ("org.apache.httpcomponents:httpclient:4.3.6",),
        ("https://api.example.com/orders/71?trace=matrix",),
        "/orders/71",
    ),
    Case(
        "hc4-4.4.1",
        "ApacheHttpClient4Sample.java",
        "ApacheHttpClient4Sample",
        "httpclient4",
        ("org.apache.httpcomponents:httpclient:4.4.1",),
        ("https://api.example.com/orders/72?trace=matrix",),
        "/orders/72",
    ),
    Case(
        "hc4-4.5.14",
        "ApacheHttpClient4Sample.java",
        "ApacheHttpClient4Sample",
        "httpclient4",
        ("org.apache.httpcomponents:httpclient:4.5.14",),
        ("https://api.example.com/orders/73?trace=matrix",),
        "/orders/73",
    ),
    Case(
        "hc5-5.0",
        "ApacheHttpClient5Sample.java",
        "ApacheHttpClient5Sample",
        "httpclient5",
        ("org.apache.httpcomponents.client5:httpclient5:5.0",),
        ("https://api.example.com/orders/81?trace=matrix",),
        "/orders/81",
    ),
    Case(
        "hc5-5.1",
        "ApacheHttpClient5Sample.java",
        "ApacheHttpClient5Sample",
        "httpclient5",
        ("org.apache.httpcomponents.client5:httpclient5:5.1",),
        ("https://api.example.com/orders/82?trace=matrix",),
        "/orders/82",
    ),
    Case(
        "hc5-5.2.3",
        "ApacheHttpClient5Sample.java",
        "ApacheHttpClient5Sample",
        "httpclient5",
        ("org.apache.httpcomponents.client5:httpclient5:5.2.3",),
        ("https://api.example.com/orders/83?trace=matrix",),
        "/orders/83",
    ),
    Case(
        "hc5-5.3.1",
        "ApacheHttpClient5Sample.java",
        "ApacheHttpClient5Sample",
        "httpclient5",
        ("org.apache.httpcomponents.client5:httpclient5:5.3.1",),
        ("https://api.example.com/orders/84?trace=matrix",),
        "/orders/84",
    ),
    Case(
        "hc5-5.4.1",
        "ApacheHttpClient5Sample.java",
        "ApacheHttpClient5Sample",
        "httpclient5",
        ("org.apache.httpcomponents.client5:httpclient5:5.4.1",),
        ("https://api.example.com/orders/85?trace=matrix",),
        "/orders/85",
    ),
    Case(
        "hc5-5.6.5",
        "ApacheHttpClient5Sample.java",
        "ApacheHttpClient5Sample",
        "httpclient5",
        ("org.apache.httpcomponents.client5:httpclient5:5.6.5",),
        ("https://api.example.com/orders/86?trace=matrix",),
        "/orders/86",
    ),
)


def _executable(path: Path) -> Path | None:
    if path.is_file():
        return path
    if os.name == "nt":
        exe = Path(str(path) + ".exe")
        if exe.is_file():
            return exe
    return None


def _home_from(value: str) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if _executable(candidate / "bin" / "java") is not None:
        return candidate
    if candidate.name in {"java", "java.exe"}:
        home = candidate.parent.parent
        if _executable(home / "bin" / "java") is not None:
            return home
    return None


def _feature(home: Path) -> int:
    release = home / "release"
    text = release.read_text(encoding="utf-8", errors="replace") if release.is_file() else ""
    match = re.search(r'JAVA_VERSION="([^"]+)"', text)
    if not match:
        return 0
    parts = match.group(1).split(".")
    return int(parts[1]) if parts[0] == "1" and len(parts) > 1 else int(parts[0])


def _jdk_homes() -> list[Path]:
    configured = os.environ.get("ASTSIGHT_JAVA_AGENT_MATRIX_JDKS", "").strip()
    if configured:
        entries = [item.strip() for item in re.split(r"[;,]", configured) if item.strip()]
    else:
        entries = [
            os.environ.get("ASTSIGHT_JAVA_AGENT_E2E_JAVA", ""),
            os.environ.get("JAVA_HOME", ""),
            shutil.which("java") or "",
        ]
    homes: list[Path] = []
    for entry in entries:
        home = _home_from(entry)
        if home is not None and home not in homes:
            homes.append(home)
    return homes


JDKS = _jdk_homes()
# 未配置 JDK 时保留参数化占位，让全量测试显式显示 skip 而不是静默不收集。
JDK_PARAMS: list[Path | None] = list(JDKS) or [None]


def _tool(jdk: Path, name: str) -> Path:
    tool = _executable(jdk / "bin" / name)
    assert tool is not None, f"{jdk} 缺少 {name}"
    return tool


def _maven() -> Path:
    configured = os.environ.get("ASTSIGHT_MAVEN", "").strip()
    if configured:
        resolved = _executable(Path(configured))
        if resolved is not None:
            return resolved
    found = shutil.which("mvn")
    if found:
        return Path(found)
    name = "mvn.cmd" if os.name == "nt" else "mvn"
    candidates = sorted(
        Path.home().glob(f".m2/wrapper/dists/apache-maven-*/*/apache-maven-*/bin/{name}")
    )
    if candidates:
        return candidates[-1]
    pytest.skip("未找到 Maven；设置 ASTSIGHT_MAVEN 或先执行 java/mvnw")


def _maven_java_home() -> Path:
    configured = os.environ.get("ASTSIGHT_MAVEN_JAVA_HOME", "").strip()
    home = _home_from(configured)
    if home is not None:
        return home
    candidates = [item for item in JDKS if _feature(item) >= 17]
    if candidates:
        return max(candidates, key=_feature)
    home = _home_from(os.environ.get("JAVA_HOME", ""))
    if home is not None:
        return home
    found = shutil.which("java")
    if found:
        home = _home_from(found)
        if home is not None:
            return home
    pytest.skip("Maven 需要 JDK 17+；设置 ASTSIGHT_MAVEN_JAVA_HOME")


def _classpath(case: Case, tmp_path: Path) -> list[Path]:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256("\n".join(case.coords).encode("utf-8")).hexdigest()[:16]
    output = CACHE / f"{key}.txt"
    if not output.is_file():
        pom = tmp_path / f"pom-{key}.xml"
        dependencies = "\n".join(
            "    <dependency>\n"
            f"      <groupId>{coord.split(':')[0]}</groupId>\n"
            f"      <artifactId>{coord.split(':')[1]}</artifactId>\n"
            f"      <version>{coord.split(':')[2]}</version>\n"
            "    </dependency>"
            for coord in case.coords
        )
        pom.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n"
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>io.astsight.matrix</groupId>\n"
            "  <artifactId>matrix</artifactId>\n"
            "  <version>1</version>\n"
            "  <dependencies>\n"
            f"{dependencies}\n"
            "  </dependencies>\n"
            "</project>\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["JAVA_HOME"] = str(_maven_java_home())
        completed = subprocess.run(
            [
                str(_maven()),
                "-q",
                "-f",
                str(pom),
                "dependency:build-classpath",
                f"-Dmdep.outputFile={output}",
                "-Dmdep.includeScope=runtime",
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if completed.returncode != 0 or not output.is_file():
            pytest.fail(
                f"Maven 解析 {case.id} 依赖失败：\n{completed.stdout[-2000:]}\n"
                f"{completed.stderr[-2000:]}"
            )
    jars = [Path(item) for item in output.read_text(encoding="utf-8").split(os.pathsep)]
    return [jar for jar in jars if jar.is_file()]


@pytest.mark.parametrize(
    "jdk",
    JDK_PARAMS,
    ids=[f"jdk{_feature(home)}" if home else "jdk-none" for home in JDK_PARAMS],
)
@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_matrix(case: Case, jdk: Path | None, tmp_path: Path) -> None:
    if jdk is None:
        pytest.skip("未配置 JDK：设置 ASTSIGHT_JAVA_AGENT_MATRIX_JDKS")
    agent = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
    if not agent.is_file():
        pytest.skip("agent jar 未构建：cd java && mvn -q -DskipTests clean package")
    sample = ROOT / "java" / "samples" / case.sample
    if not sample.is_file():
        pytest.skip(f"缺少示例源码 {sample}")
    jars = _classpath(case, tmp_path)
    java = _tool(jdk, "java")
    javac = _tool(jdk, "javac")

    sut_id = f"sut-matrix-{case.id}-jdk{_feature(jdk)}"
    dep = DepIdEntry(
        dep_id=DEP_ID,
        downstream_key="host:api.example.com",
        evidence="host",
        method="GET",
        path_key="/orders/{}",
        target="https://api.example.com/orders/{}",
        route_host="api.example.com",
    )
    route_key = assign_route_keys([dep])[dep.dep_id]
    server = MockHttpServer(
        sut_id=sut_id,
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dep(
        dep,
        route_key=route_key,
        branches={"success": MockStub(body={"ok": True, "case": case.id})},
    )
    server.start()
    try:
        admin = {"Authorization": f"Bearer {TOKEN}"}
        with httpx.Client(base_url=server.endpoint) as client:
            created = client.post(
                "/_admin/sessions",
                headers=admin,
                json={
                    "session_id": f"{sut_id}-s1",
                    "namespace": sut_id,
                    "case_id": case.id,
                    "run_scope": "R1",
                    "bindings": [{"dep_id": dep.dep_id, "branch": "success"}],
                },
            )
            assert created.status_code == 200, created.text

        classes = tmp_path / "classes"
        classes.mkdir()
        compile_cp = os.pathsep.join(str(path) for path in jars)
        compiled = subprocess.run(
            [
                str(javac),
                "-encoding",
                "UTF-8",
                "-cp",
                compile_cp,
                "-d",
                str(classes),
                str(sample),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        assert compiled.returncode == 0, (
            f"{case.id} 在 JDK {_feature(jdk)} 下编译失败：\n"
            f"{compiled.stdout[-2000:]}\n{compiled.stderr[-4000:]}"
        )

        run_cp = os.pathsep.join([str(classes)] + [str(path) for path in jars])
        java_args = (
            f"admin={server.endpoint},token={TOKEN},sut_id={sut_id},"
            "heartbeat_interval_ms=1000,route_refresh_interval_ms=1000"
        )
        completed = subprocess.run(
            [
                str(java),
                f"-javaagent:{agent}={java_args}",
                "-cp",
                run_cp,
                case.main,
                *case.args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        assert completed.returncode == 0, (
            f"{case.id} 在 JDK {_feature(jdk)} 下运行失败：\n"
            f"{completed.stdout[-2000:]}\n{completed.stderr[-4000:]}"
        )
        assert "status=200" in completed.stdout, completed.stdout
        assert '"ok":true' in completed.stdout, completed.stdout

        deadline = time.monotonic() + 10
        heartbeat = None
        while time.monotonic() < deadline:
            response = httpx.get(
                f"{server.endpoint}/_admin/agent/heartbeat",
                headers=admin,
                timeout=5,
            )
            if response.status_code == 200:
                heartbeat = response.json()
                if heartbeat.get("http_rewrites", 0) >= 1:
                    break
            time.sleep(0.2)
        assert heartbeat is not None
        adapters = {item["id"] for item in heartbeat["http_adapters"]}
        assert case.adapter in adapters, adapters
        assert heartbeat["http_rewrites"] >= 1
        assert heartbeat["http_errors"] == 0

        calls = server.evidence.records
        assert len(calls) == 1, [call.original_path for call in calls]
        call = calls[0]
        assert call.result == "ok"
        assert call.original_path == case.original_path
        assert call.adapter == case.adapter
        assert call.mode == "agent_rewrite"
        assert call.original_host == "api.example.com"
    finally:
        server.stop()
