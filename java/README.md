# AstSight Java Agent

Java agent 在出站 HTTP 客户端调用前改写 URL，把请求定向到引擎内嵌的 Mock 服务。
它只做 URL 改写：响应、分支、会话和证据仍由 `engine/astsight/mock/` 产生。

## 模块

| 模块 | 作用 |
|---|---|
| `astsight-agent-core` | `premain`、admin 路由表拉取、路由匹配、心跳、适配器注册表、失败关闭判定 |
| `astsight-agent-http` | OkHttp / Feign / RestTemplate / Apache HttpClient 4/5 适配器（ASM 织入 + 反射 bridge） |
| `astsight-agent` | 最终 shaded jar，入口 `io.astsight.agent.core.AstSightAgent` |

字节码目标为 JDK 8；构建使用 JDK 8+（当前在 JDK 26 上验证，实测运行矩阵
仍待补 JDK 8/11/17/21）。ASM、ByteBuddy（预留给 Dubbo 等后续 adapter）与
Jackson 打进 shaded jar；OkHttp、Feign、Spring 均为 `provided`，不随 agent
分发。

所有 adapter 都使用裸 ASM 而非 ByteBuddy Advice：Spring Boot fat jar 由
`LaunchedURLClassLoader` 加载应用依赖时，ByteBuddy 解析方法签名会尝试从
agent 类加载器加载 `okhttp3.Request` 等类型并抛 `NoClassDefFoundError`。
ASM 只按名称与 JVM 描述符匹配方法，注入代码只引用 JDK 类型和对应 bridge；
bridge 全程反射操作应用侧类型。

## 构建

```bash
cd java
mvn -q -DskipTests package
# java/astsight-agent/target/astsight-agent.jar
```

## 启动

```bash
java -javaagent:astsight-agent/target/astsight-agent.jar=\
  admin=http://127.0.0.1:36000,token=<mock-token>,sut_id=<sut-id> \
  -cp <sut-classpath> <main-class>
```

`agent run` 自动注入时使用 `token_env`，token 不进入命令行：

```bash
export ASTSIGHT_AGENT_TOKEN=<mock-token>
export JAVA_TOOL_OPTIONS="-javaagent:/path/to/astsight-agent.jar=\
admin=http://127.0.0.1:36000,token_env=ASTSIGHT_AGENT_TOKEN,sut_id=<sut-id>"
```

可选参数：

- `token_env`：从指定环境变量读取 token；与 `token` 二选一，优先 `token`。
- `heartbeat_interval_ms`：默认 5000，最小 500。
- `route_refresh_interval_ms`：默认 5000，最小 500；agent 周期性重新拉取路由表。

agent 从 `GET /_admin/agent/routes` 拿到 Mock endpoint 与
`route_key/dep_id/method/path_key/host`，再按 Host + method + 路径匹配：

- 命中：改写为 `http://<mock>/_dep/<route_key><原路径>?<原 query>`。
- 计划内 host 未命中：改写为 `/_agent/unmatched`，Mock 返回 502 并记录
  `mock_route_unknown` / `mock_route_ambiguous`，不放行真实下游。
- 计划外 host：放行，心跳 `passthrough_hosts` 计数，报告必须列出。
- 路径已以 `/_dep/`、`/_agent/` 开头或目标就是 Mock 端点：跳过，防二次改写。
- 计划内请求改写失败（bridge 异常、Feign 签名不兼容等）：抛
  `AgentRewriteException` 让本次调用失败，绝不返回原始请求透传。

HTTPS 只在 `agent_rewrite` 策略下支持：agent 把 scheme 改写为 `http` 后访问
Mock，不建立 TLS、不安装测试 CA、不做 MITM。

## 证据

改写请求会附带以下头，Mock 服务写入 `mock_calls.jsonl`：

- `X-AstSight-Adapter`
- `X-AstSight-Mode: agent_rewrite`
- `X-AstSight-Original-Host`
- `X-AstSight-Original-Path`
- `X-AstSight-Original-Query`

心跳通过 `POST /_admin/agent/heartbeat` 上报，字段见
`doc/决策记录.md` §17。agent 侧在请求到达 Mock 前抛出的异常只计入
`http_errors`，不新增第二份调用日志。

## 当前范围

支持的客户端：

| 客户端 | 织入点 | 备注 |
|---|---|---|
| OkHttp 3/4/5 | `OkHttpClient#newCall(Request)` | 直接使用 OkHttp 的调用 |
| Feign（阻塞） | `SynchronousMethodHandler#targetRequest` | 默认 / OkHttp / Apache HC 传输；Feign 10–13 的 `Request.create` 重载动态适配 |
| Spring RestTemplate | `RestTemplate#doExecute` | Spring 5 四参与 Spring 6.2 五参；与 RequestFactory 无关 |
| Apache HttpClient 4.3+ | `InternalHttpClient#doExecute` | 直连 `CloseableHttpClient.execute(...)`；含内部使用 HC4 的 SDK |
| Apache HttpClient 5 classic | `InternalHttpClient#doExecute` | 直连 `CloseableHttpClient.execute(...)`；5.0–5.4 参数差异按描述符适配 |

内置使用上述客户端的第三方 SDK（例如 HC4/HC5 之上的对象存储、检索 SDK）
会顺带被对应 adapter 改写到 Mock；但计划期能否识别该依赖仍取决于画像能否
从源码或配置拿到 URL，未进入路由表的 host 一律放行（心跳单独计数）。

明确的非目标：WebClient/Reactor、使用自有传输或 shaded/relocated HTTP
客户端的第三方 SDK、JDK 11 `HttpClient`、`HttpURLConnection`、
async-http-client、Apache HttpClient 4.0–4.2 与 HC5 async。它们无法在 HTTP
边界统一改写，计划期标 `unsupported`，不会“看起来支持、实际透传”。Dubbo
仍按原方案走 Filter + ByteBuddy。

可选端到端测试（默认跳过）：

- `engine/tests/integration/test_java_agent_okhttp_e2e.py`：普通 JVM + OkHttp。
- `engine/tests/integration/test_java_agent_apache_httpclient_e2e.py`：同一 JVM
  内依次用 HC4 与 HC5 直连 HTTPS，验证两个 adapter 共存、证据与心跳。
- `engine/tests/integration/test_java_agent_spring_boot_e2e.py`：Spring Boot
  3.5.6 fat jar（`LaunchedURLClassLoader`），同一进程验证 OkHttp / Feign /
  RestTemplate 三条 HTTPS 出站、证据与心跳。

三者均已实测通过。

## 已知边界

- OkHttp 3/4/5 的 `newCall(Request)` 二进制签名相同；尚未逐一跑版本矩阵。
- Apache HttpClient 仅覆盖 4.3+ 与 5.x classic 的 `InternalHttpClient`；
  4.0–4.2 的 `AbstractHttpClient`、HC5 async 与自定义 `CloseableHttpClient`
  子类不在范围内（计划期不会因此标 supported）。
- 改写发生在 HTTP 边界，不绕过 SUT 的编解码、重试、降级链。
- 签名、pinning、连接池复用、重定向与异步链路的副作用尚未验证。
