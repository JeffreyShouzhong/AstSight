"""Java / Go / Node 下游识别与路由扫描。"""
from __future__ import annotations

from pathlib import Path

from astsight.inventory import inventory_routes
from astsight.profile.builder import build_profiles
from astsight.profile.downstream import DownstreamScanner
from astsight.profile.langroutes import scan_language_routes


def _write(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


JAVA_SOURCE = """
package com.example.pay;

@RestController
@RequestMapping("/pay")
public class PayController {
    @Value("${pay.base-url}")
    private String payBaseUrl;

    @GetMapping("/charge")
    public String charge() {
        return restTemplate.getForObject(payBaseUrl + "/charge", String.class);
    }
}
"""


def test_java_resttemplate_config_target(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/com/example/pay/PayController.java",
        JAVA_SOURCE,
    )
    scan = DownstreamScanner(tmp_path).scan(
        "PayController.charge", file_hint="src/main/java/com/example/pay/PayController.java"
    )
    assert scan.checked is True
    assert len(scan.deps) == 1
    dep = scan.deps[0]
    assert dep["dep_id"].startswith("http:call:")
    assert dep["dep_id"].endswith("GET /charge")
    assert dep["config_keys"] == ["PAY_BASE_URL"]
    site = scan.call_sites[0]
    assert site.file == "src/main/java/com/example/pay/PayController.java"
    assert site.config_key == "PAY_BASE_URL"


def test_java_webclient_literal_target(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/com/example/pay/PricingClient.java",
        """
public class PricingClient {
    public Mono<String> quote() {
        return webClient.get().uri("http://pricing.internal/quote").retrieve().bodyToMono(String.class);
    }
}
""",
    )
    scan = DownstreamScanner(tmp_path).scan("PricingClient.quote")
    assert [dep["dep_id"] for dep in scan.deps] == [
        "http:host:pricing.internal GET /quote"
    ]


GO_SOURCE = """
package main

import "net/http"

const pricingBase = "http://pricing.internal"

func quoteHandler(w http.ResponseWriter, r *http.Request) {
    resp, err := http.Get(pricingBase + "/quote")
    if err != nil {
        return
    }
    defer resp.Body.Close()
}
"""


def test_go_net_http_target(tmp_path: Path) -> None:
    _write(tmp_path, "main.go", GO_SOURCE)
    scan = DownstreamScanner(tmp_path).scan("quoteHandler", file_hint="main.go")
    assert scan.checked is True
    assert [dep["dep_id"] for dep in scan.deps] == [
        "http:host:pricing.internal GET /quote"
    ]


def test_go_unresolved_target_is_unsupported(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.go",
        """
package main

func call(name string) {
    http.Get(buildURL(name))
}
""",
    )
    scan = DownstreamScanner(tmp_path).scan("call", file_hint="main.go")
    assert scan.deps == []
    assert scan.unsupported
    assert scan.unsupported[0]["reason"] in {"target_unresolved", "dynamic_call"}


NODE_SOURCE = """
const base = process.env.PRICING_BASE_URL;

async function charge() {
  const response = await fetch(`${base}/charge`, { method: 'POST' });
  return response.json();
}
"""


def test_node_fetch_template_config(tmp_path: Path) -> None:
    _write(tmp_path, "src/pay.js", NODE_SOURCE)
    scan = DownstreamScanner(tmp_path).scan("charge", file_hint="src/pay.js")
    assert scan.checked is True
    assert len(scan.deps) == 1
    dep = scan.deps[0]
    assert dep["dep_id"].startswith("http:call:")
    assert dep["dep_id"].endswith("POST /charge")
    assert dep["config_keys"] == ["PRICING_BASE_URL"]


def test_node_axios_instance(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/pricing.ts",
        """
import axios from 'axios';

const client = axios.create({ baseURL: 'http://pricing.internal' });

export async function quote(id: string) {
  return client.get('/quote');
}
""",
    )
    scan = DownstreamScanner(tmp_path).scan("quote", file_hint="src/pricing.ts")
    assert [dep["dep_id"] for dep in scan.deps] == [
        "http:host:pricing.internal GET /quote"
    ]


def test_spring_routes(tmp_path: Path) -> None:
    _write(tmp_path, "src/PayController.java", JAVA_SOURCE)
    routes, warnings = scan_language_routes(tmp_path)
    assert not warnings
    assert {
        (route["method"], route["path"], route["handler"]) for route in routes
    } == {("GET", "/pay/charge", "charge")}
    assert routes[0]["source"] == "framework_plugin"


def test_gin_routes_with_group(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.go",
        """
package main

import "github.com/gin-gonic/gin"

func main() {
    r := gin.Default()
    api := r.Group("/api")
    api.GET("/pay", payHandler)
    api.POST("/pay", createHandler)
}
""",
    )
    routes, _ = scan_language_routes(tmp_path)
    assert {
        (route["method"], route["path"], route["handler"]) for route in routes
    } == {
        ("GET", "/api/pay", "payHandler"),
        ("POST", "/api/pay", "createHandler"),
    }


def test_express_routes_with_prefix(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/app.js",
        """
const express = require('express');
const app = express();
const router = express.Router();

router.get('/:id', getOrder);
app.use('/orders', router);
app.post('/pay', async (req, res) => {
  res.json({});
});
""",
    )
    routes, _ = scan_language_routes(tmp_path)
    found = {(route["method"], route["path"]) for route in routes}
    assert ("GET", "/orders/{id}") in found
    assert ("POST", "/pay") in found


def test_express_inline_handler_scanned_by_route_line(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/app.js",
        """
const express = require('express');
const app = express();

app.post('/pay', async (req, res) => {
  const result = await fetch(process.env.PRICING_BASE_URL + '/charge', {
    method: 'POST',
  });
  res.json(result);
});
""",
    )
    routes, _ = scan_language_routes(tmp_path)
    assert len(routes) == 1
    assert routes[0]["handler"] == ""
    assert routes[0]["line"] > 0

    profiles, _ = build_profiles(routes, [], project_root=tmp_path)
    profile = profiles[0]
    assert profile["downstream_checked"] is True
    assert profile["downstream_deps"][0]["config_keys"] == ["PRICING_BASE_URL"]
    assert profile["downstream_deps"][0]["dep_id"].endswith("POST /charge")


def test_java_profile_has_downstream_dep(tmp_path: Path) -> None:
    _write(tmp_path, "src/PayController.java", JAVA_SOURCE)
    routes, _ = scan_language_routes(tmp_path)
    profiles, _ = build_profiles(routes, [], project_root=tmp_path)
    profile = profiles[0]
    assert profile["downstream_checked"] is True
    assert profile["downstream_deps"][0]["call_sites"]


def test_inventory_merges_language_routes(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "main.go",
        """
package main

import "github.com/gin-gonic/gin"

func main() {
    r := gin.Default()
    r.GET("/pay", payHandler)
}
""",
    )
    routes, audit, _client, notes = inventory_routes(tmp_path, codegraph="off")
    assert ("GET", "/pay") in {
        (route["method"], route["path"]) for route in routes
    }
    assert any("codegraph" in note for note in notes) or not notes
    assert audit.get("language_routes", {}).get("added", 0) >= 1
