"""Dubbo 消费端 Filter 版本矩阵：JDK × Dubbo 2.7 / 3.2。

默认跳过。启用条件：

- `ASTSIGHT_JAVA_AGENT_DUBBO_MATRIX_E2E=1`
- 已构建 `java/astsight-agent/target/astsight-agent.jar`
- `ASTSIGHT_JAVA_AGENT_DUBBO_MATRIX_JDKS` 指向一个或多个 JDK 家目录，用
  `;` 或 `,` 分隔；未设置时退回 `ASTSIGHT_JAVA_AGENT_E2E_JAVA` /
  `JAVA_HOME` / PATH
- `ASTSIGHT_JAVA_AGENT_DUBBO_MATRIX_VERSIONS` 可覆盖 Dubbo 版本列表，
  默认 `2.7.22,3.2.19`
- 首次运行需要 Maven 与网络下载 Dubbo 依赖；之后走
  `~/.astsight/java-agent-dubbo-matrix/` 缓存

每个组合都会编译 `DubboFilterSample.java`、带 `-javaagent` 运行 `stub`
模式，并核对 Mock 证据与心跳里的 Dubbo 版本。样例不启动注册中心与
provider，真实调用次数必须为 0。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from astsight.mock import MockHttpServer
from contracts import MockStub

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTSIGHT_JAVA_AGENT_DUBBO_MATRIX_E2E") != "1",
    reason="Dubbo 版本矩阵默认跳过：设置 ASTSIGHT_JAVA_AGENT_DUBBO_MATRIX_E2E=1 启用",
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "dubbo-matrix-token"
DEP_ID = "dubbo:OrderService#create(java.lang.String)?group=order&version=1.0.0"
CACHE = Path.home() / ".astsight" / "java-agent-dubbo-matrix"


def _versions() -> tuple[str, ...]:
    configured = os.environ.get(
        "ASTSIGHT_JAVA_AGENT_DUBBO_MATRIX_VERSIONS", ""
    ).strip()
    if configured:
        return tuple(
            item.strip() for item in re.split(r"[;,]", configured) if item.strip()
        )
    return ("2.7.22", "3.2.19")


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
    configured = os.environ.get(
        "ASTSIGHT_JAVA_AGENT_DUBBO_MATRIX_JDKS", ""
    ).strip()
    if configured:
        entries = [
            item.strip()
            for item in re.split(r"[;,]", configured)
            if item.strip()
        ]
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
        Path.home().glob(
            f".m2/wrapper/dists/apache-maven-*/*/apache-maven-*/bin/{name}"
        )
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


def _classpath(version: str, tmp_path: Path) -> list[Path]:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"org.apache.dubbo:dubbo:{version}".encode()).hexdigest()[:16]
    output = CACHE / f"{key}.txt"
    if not output.is_file():
        pom = tmp_path / f"pom-{key}.xml"
        pom.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n"
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>io.astsight.matrix</groupId>\n"
            "  <artifactId>dubbo-matrix</artifactId>\n"
            "  <version>1</version>\n"
            "  <dependencies>\n"
            "    <dependency>\n"
            "      <groupId>org.apache.dubbo</groupId>\n"
            "      <artifactId>dubbo</artifactId>\n"
            f"      <version>{version}</version>\n"
            "    </dependency>\n"
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
            timeout=900,
        )
        if completed.returncode != 0 or not output.is_file():
            pytest.fail(
                f"Maven 解析 Dubbo {version} 依赖失败：\n"
                f"{completed.stdout[-2000:]}\n{completed.stderr[-3000:]}"
            )
    jars = [
        Path(item)
        for item in output.read_text(encoding="utf-8").split(os.pathsep)
    ]
    return [jar for jar in jars if jar.is_file()]


@pytest.mark.parametrize(
    "jdk",
    JDK_PARAMS,
    ids=[f"jdk{_feature(home)}" if home else "jdk-none" for home in JDK_PARAMS],
)
@pytest.mark.parametrize("version", _versions(), ids=lambda value: f"dubbo-{value}")
def test_dubbo_matrix(version: str, jdk: Path | None, tmp_path: Path) -> None:
    if jdk is None:
        pytest.skip("未配置 JDK：设置 ASTSIGHT_JAVA_AGENT_DUBBO_MATRIX_JDKS")
    agent = ROOT / "java" / "astsight-agent" / "target" / "astsight-agent.jar"
    if not agent.is_file():
        pytest.skip("agent jar 未构建：cd java && mvn -q -DskipTests clean package")
    sample = ROOT / "java" / "samples" / "DubboFilterSample.java"
    if not sample.is_file():
        pytest.skip(f"缺少示例源码 {sample}")
    jars = _classpath(version, tmp_path)
    java = _tool(jdk, "java")
    javac = _tool(jdk, "javac")

    sut_id = f"sut-dubbo-matrix-{version}-jdk{_feature(jdk)}"
    server = MockHttpServer(
        sut_id=sut_id,
        token=TOKEN,
        history_path=tmp_path / "mock_calls.jsonl",
    )
    server.add_dubbo_dep(
        DEP_ID,
        branches={"success": MockStub(body={"orderId": "O-1", "amount": 10})},
    )
    server.start()
    try:
        admin = {"Authorization": f"Bearer {TOKEN}"}
        created = httpx.post(
            f"{server.endpoint}/_admin/sessions",
            headers=admin,
            json={
                "session_id": f"{sut_id}-s1",
                "namespace": sut_id,
                "case_id": "case-matrix",
                "run_scope": "R1",
                "bindings": [{"dep_id": DEP_ID, "branch": "success"}],
            },
            timeout=5,
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
            f"Dubbo {version} 在 JDK {_feature(jdk)} 下编译失败：\n"
            f"{compiled.stdout[-2000:]}\n{compiled.stderr[-4000:]}"
        )

        run_cp = os.pathsep.join([str(classes), *[str(path) for path in jars]])
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
                "DubboFilterSample",
                "stub",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert completed.returncode == 0, (
            f"Dubbo {version} 在 JDK {_feature(jdk)} 下运行失败：\n"
            f"{completed.stdout[-2000:]}\n{completed.stderr[-4000:]}"
        )
        assert "filter=io.astsight.agent.dubbo.AstSightDubboFilter" in completed.stdout
        assert "result=O-1,10" in completed.stdout
        assert "REAL_CALL" not in completed.stdout
        assert "real=0" in completed.stdout

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
                dubbo = heartbeat.get("dubbo") or {}
                if dubbo.get("filter_hits", 0) >= 1:
                    break
            time.sleep(0.2)
        assert heartbeat is not None
        dubbo = heartbeat["dubbo"]
        assert dubbo["installed"] is True
        assert str(dubbo["version"]).startswith(version.rsplit(".", 1)[0])
        assert dubbo["serialization_verified"] is False

        records = server.evidence.records
        assert len(records) == 1, [record.model_dump() for record in records]
        assert records[0].dep_id == DEP_ID
        assert records[0].adapter == "dubbo-filter"
        assert records[0].result.value == "ok"
    finally:
        server.stop()
