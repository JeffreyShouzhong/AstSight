# AstSight Java Agent

Java 侧有两个产物：**agent**（下面两条接入路径）和 **Dubbo sidecar**
（见下一节，替测试端调用被测服务，不注入被测 JVM）。

1. **HTTP 客户端改写**：在出站 HTTP 调用前改写 URL，把请求定向到引擎内嵌的
   Mock 服务。它只做 URL 改写，响应、分支、会话和证据仍由
   `engine/astsight/mock/` 产生。
2. **Dubbo 消费端 Filter**：把 Filter 注入 Dubbo 的 `reference.filter` 链，
   计划内调用直接向 Mock 取桩并返回，不发起真实 RPC。首版只支持
   `-javaagent` 形态（v4.21）。

## Dubbo Sidecar（调用被测服务）

`astsight-sidecar-dubbo` 是**另一个方向的独立进程**：它不是 agent，不由被测
JVM 加载，而是替测试端调用被测 Dubbo 服务。引擎把调用写成 JSONL 送进 stdin，
sidecar 用 `ReferenceConfig<GenericService>` + Hessian2 泛化调用，再把结构化
结果写回 stdout。

- 不 Attach 被测 JVM、不装 agent、不执行被测仓库代码；
- 首版只支持直连 `dubbo://host:port`；注册中心与 Kryo/FST 显式失败；
- `retries=0`，超时/序列化/网络/无 provider 都归一成结构化 `rpc_status`；
- JDK 9+ 启动时自动加 `--add-opens java.base/{java.lang,java.math,java.util}`。

```bash
cd java
mvn -pl astsight-sidecar-dubbo package
# target/astsight-dubbo-sidecar.jar

printf '%s\n' '{"id":"1","op":"ping"}' '{"id":"2","op":"shutdown"}' \
  | java --add-opens=java.base/java.lang=ALL-UNNAMED \
         --add-opens=java.base/java.math=ALL-UNNAMED \
         --add-opens=java.base/java.util=ALL-UNNAMED \
         -jar astsight-sidecar-dubbo/target/astsight-dubbo-sidecar.jar
```

协议只有 `ping`（版本/协议号自检）、`invoke`、`shutdown`；stdout 只写协议，
Dubbo/Netty 日志全部转到 stderr。Python 客户端在
`engine/astsight/sidecar/dubbo.py`。

## 模块

| 模块 | 作用 |
|---|---|
| `astsight-agent-core` | `premain`、admin 路由表拉取、路由匹配、心跳、适配器注册表、失败关闭判定 |
| `astsight-agent-http` | OkHttp / Feign / RestTemplate / Apache HttpClient 4/5 适配器（ASM 织入 + 反射 bridge） |
| `astsight-agent-dubbo` | Dubbo 消费端 Filter、`DubboBridge`、Filter 注入器（`defineClass` 进应用类加载器） |
| `astsight-agent` | 最终 shaded jar，入口 `io.astsight.agent.core.AstSightAgent` |
| `astsight-sidecar-dubbo` | 独立进程，泛化调用被测 Dubbo 服务；不属于 agent，不注入被测 JVM |

字节码目标为 JDK 8；构建使用 JDK 8+。HTTP adapter 的运行矩阵已在
JDK 8/11/17/21/26 上实测；Dubbo Filter 在 Dubbo 2.7.22 与 3.2.19 ×
JDK 8/11/17/21/26 共 10 个组合上实测。
ASM、ByteBuddy 与 Jackson 打进 shaded jar；OkHttp、Feign、Spring、Dubbo
均为 `provided`，不随 agent 分发。

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

打包成随引擎分发的 wheel（会先执行 `mvn package`）：

```bash
python tools/build_agent_wheel.py
# dist/astsight_java_agent-0.1.0.dev0-py3-none-any.whl
```

wheel 内含 `astsight_java_agent/manifest.json`：`version`（PEP 440）、
`jar_version`（Maven 版本）、`protocol`、`sha256`、`size`。构建同时写 sidecar
`target/astsight-agent.jar.manifest.json`，源码树里的 jar 也能被校验。引擎侧
版本常量在 `engine/astsight/agent/jar.py`，测试断言它与 `java/pom.xml` 的版本
归一结果一致。

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
- 同一 host + method 下字面量路径优先于 `{}` 模板（`/orders/7` 先于
  `/orders/{}`）；多个字面量或多个模板仍按歧义处理。
- 计划内 host 未命中：改写为 `/_agent/unmatched`，Mock 返回 502 并记录
  `mock_route_unknown` / `mock_route_ambiguous`，不放行真实下游。
- 计划外 host：放行，心跳 `passthrough_hosts` 计数，报告必须列出。
- 路径已以 `/_dep/`、`/_agent/` 开头或目标就是 Mock 端点：跳过，防二次改写。
- 计划内请求改写失败（bridge 异常、Feign 签名不兼容等）：抛
  `AgentRewriteException` 让本次调用失败，绝不返回原始请求透传。

HTTPS 只在 `agent_rewrite` 策略下支持：agent 把 scheme 改写为 `http` 后访问
Mock，不建立 TLS、不安装测试 CA、不做 MITM。

## 分发与解析

引擎按顺序解析 agent jar，只有校验通过才会注入：

1. `ASTSIGHT_JAVA_AGENT_JAR`：显式覆盖，只认该路径，缺失不回退；
2. 已安装的 `astsight-java-agent` wheel：必须带 `manifest.json`，校验
   sha256、大小、版本与协议号；
3. 仓库默认 `java/astsight-agent/target/astsight-agent.jar`：有 sidecar 就校验，
   没有则按未校验的开发构建接受，但版本仍须匹配。

失败不静默降级，而是告警
`injection_agent_unavailable:<dep>:<reason>`，reason 包括
`agent_jar_missing`、`agent_jar_path_has_space`、`agent_jar_unreadable`、
`agent_jar_version_mismatch`、`agent_jar_manifest_missing`、
`agent_jar_manifest_invalid`、`agent_jar_checksum_mismatch`、
`agent_jar_size_mismatch`、`agent_jar_protocol_mismatch`、`token_missing`、
`endpoint_missing`。

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
不做 SDK 专用端点发现：区域/签名/加密配置里拼出来的地址扫不可靠，能识别就
纳入，识别不到就留空并在报告中列出。

明确的非目标：WebClient/Reactor、使用自有传输或 shaded/relocated HTTP
客户端的第三方 SDK、JDK 11 `HttpClient`、`HttpURLConnection`、
async-http-client、Apache HttpClient 4.0–4.2 与 HC5 async。它们无法在 HTTP
边界统一改写，计划期标 `unsupported`，不会“看起来支持、实际透传”。

可选端到端测试（默认跳过）：

- `engine/tests/integration/test_java_agent_okhttp_e2e.py`：普通 JVM + OkHttp。
- `engine/tests/integration/test_java_agent_apache_httpclient_e2e.py`：同一 JVM
  内依次用 HC4 与 HC5 直连 HTTPS，验证两个 adapter 共存、证据与心跳。
- `engine/tests/integration/test_java_agent_spring_boot_e2e.py`：Spring Boot
  3.5.6 fat jar（`LaunchedURLClassLoader`），同一进程验证 OkHttp / Feign /
  RestTemplate 三条 HTTPS 出站、证据与心跳。
- `engine/tests/integration/test_java_agent_matrix_e2e.py`：版本矩阵，按
  `ASTSIGHT_JAVA_AGENT_MATRIX_JDKS` 指定的 JDK 列表逐个编译/运行样例，覆盖
  OkHttp 3.14.9 / 4.12.0 / 5.5.1、Feign 10.12 / 11.10 / 12.5 / 13.5、
  HC4 4.3.6 / 4.4.1 / 4.5.14、HC5 5.0 / 5.1 / 5.2.3 / 5.3.1 / 5.4.1 / 5.6.5。
- `engine/tests/integration/test_java_agent_h2_prior_knowledge_e2e.py`：h2c 负例。
  OkHttp `Protocol.H2_PRIOR_KNOWLEDGE` 直连 Mock，断言改写发生、SUT 显式失败、
  真实下游 0 连接（零透传）。
- `engine/tests/integration/test_java_agent_dubbo_e2e.py`：Dubbo 消费端 Filter，
  验证取桩返回 DTO、未绑定失败关闭、计划外放行，以及异步/声明 future/
  oneway/泛化四种形态（详见下一节）。
- `engine/tests/integration/test_java_agent_dubbo_consumer_e2e.py`：真实
  `ReferenceConfig` 代理调用（直连 URL、不启 provider），普通 classpath 与
  Spring Boot fat jar 两种类加载器都验证：返回桩对象、目标端口零建连零请求、
  Filter 与 bridge 跨类加载器可调用。
- `engine/tests/integration/test_java_agent_dubbo_flow_e2e.py`：`agent run`
  全链路（HTTP 入口 + Dubbo 下游），覆盖画像 → `dubbo_routes` →
  `JAVA_TOOL_OPTIONS` + `token_env` 注入 → 执行 → 报告，并断言目标 Dubbo
  端口零建连、门禁 pass。

以上 HTTP 相关用例均已实测通过；版本矩阵在 JDK 8/11/17/21/26 上共 80 个
JDK×客户端组合全部通过，Spring Boot 样例在 JDK 17/21/26 通过。Dubbo e2e
（含真实消费端代理与 `agent run` 全链路）在 JDK 17 与 21 上跑过；Dubbo 版本矩阵另见
`test_java_agent_dubbo_matrix_e2e.py`，2.7.22 与 3.2.19 × JDK 8/11/17/21/26
共 10 个组合全部通过。

## Dubbo 消费端 Filter

`astsight-agent-dubbo` 把 `AstSightDubboFilter` 注册为 Dubbo 消费端 Filter
（SPI 名 `astSightMock`，`@Activate(group = "consumer")`）。agent jar 在系统
类加载器，Filter 必须在应用类加载器里才认得 Dubbo 类型，所以注入方式是：
监听 `org/apache/dubbo/common/extension/ExtensionLoader` 的加载，反射调用该
加载器的 `ClassLoader#defineClass` 定义 Filter 字节（JDK 9+ 先用
`Instrumentation.redefineModule` 把 `java.lang` 同时打开给 agent 自己与应用
类加载器对应的 unnamed module——fat jar 里两者是不同模块，只开给 agent 时
Filter 注入和 Dubbo 2.7.x 自带的 javassist 编译都会失败）。不增强 Dubbo
内部类，也不使用 `URLClassLoader#addURL` 或动态代理。

它注册在 `astsight-agent` 的分发 jar 里，随 `-javaagent` 一起生效。注入的
Filter 只引用 Dubbo 类型和父类加载器里的 `DubboBridge`；bridge 对
`Invoker`/`Invocation`/`URL`/`Result`/`AsyncRpcResult` 全走反射。因为 Filter
与应用可能不在同一个类加载器，两者的运行时包不同，bridge 的
`filterInstantiated` / `intercept` 必须是 `public`，包级可见会抛
`IllegalAccessError`（fat jar e2e 已覆盖）。

调用路径：

- `dep_id` 形如
  `dubbo:<接口>#<方法>(<参数类型,...>)?group=<g>&version=<v>`，参数类型用
  JVM 名（如 `java.lang.String`、`int`），无参数写 `()`，用于区分重载；
  泛化调用从 `args[0]`/`args[1]` 取接口和方法名，从 `args[2]` 取参数类型，
  并读取 `interface` attachment。
- 计划内调用向 `POST /_admin/dubbo/invoke` 取桩，Mock 按会话绑定返回
  `{route_key, dep_id, branch, body, delay_ms}`；返回体按接口方法的
  `getGenericReturnType()` 用 Jackson 构造，void 返回 null。
- 计划内取桩失败、未绑定、分支缺失、admin 不可达：抛 `RpcException`（消息带
  `mock_*` 子码），真实调用 0 次。
- 路由快照已加载且该 dep 不在计划内：调用下一个 Invoker 放行，不产生证据。
- agent 未激活：`agent_not_active`，同样失败关闭，不静默透传。

证据写入 `mock_calls.jsonl`：`adapter=dubbo-filter`、`mode=agent_filter`、
`original_path=dubbo:<接口>#<方法>`。心跳的 `dubbo` 块上报
`installed / version / filter_instantiated / filter_hits / passthrough /
errors / serialization_verified`，其中 `serialization_verified` 固定为
`false`（首版不校验序列化保真）。

**支持范围**：只支持 `-javaagent` 形态。同步、老式异步、声明
`CompletableFuture<T>`、oneway 与泛化 `$invoke`/`$invokeAsync` 都已处理。
已实测的版本矩阵为 Dubbo 2.7.22 与 3.2.19，各自跑过 JDK
8/11/17/21/26，共 10 个组合全部通过。classpath + `dubbo.consumer.filter`
形态与本地心跳文件**明确不支持**（classpath 只对 Dubbo 有效，无法覆盖 HTTP）。
未出现在矩阵里的 Dubbo 版本不做运行期试探，画像期按 `unsupported` 处理。

端到端测试默认跳过，启用方式：

```bash
cd java
mvn -pl astsight-agent -am -DskipTests clean package
# 生成 target/dubbo-classpath.txt（2.7.22 及其依赖）
# 真实消费端样例：普通 classpath 与 Spring Boot fat jar
cd samples/dubbo-consumer && mvn -q -DskipTests package
# 全链路样例：HTTP 入口 + Dubbo 下游
cd ../spring-boot-dubbo && mvn -q -DskipTests package
cd ../../..
cd engine
ASTSIGHT_JAVA_AGENT_E2E=1 \
python -m pytest tests/integration/test_java_agent_dubbo_e2e.py \
  tests/integration/test_java_agent_dubbo_consumer_e2e.py -q

ASTSIGHT_JAVA_AGENT_DUBBO_FLOW_E2E=1 \
python -m pytest tests/integration/test_java_agent_dubbo_flow_e2e.py -q
```

版本矩阵另行启用（首次运行会经 Maven 下载 Dubbo 依赖）：

```bash
cd engine
ASTSIGHT_JAVA_AGENT_DUBBO_MATRIX_E2E=1 \
ASTSIGHT_JAVA_AGENT_DUBBO_MATRIX_JDKS="<JDK8>;<JDK11>;<JDK17>;<JDK21>;<JDK26>" \
python -m pytest tests/integration/test_java_agent_dubbo_matrix_e2e.py -q
```

样例 `java/samples/DubboFilterSample.java` 不启动注册中心与 provider：它经
Dubbo SPI 取出 Filter，对计数假 Invoker 调用，分别验证取桩成功、未绑定失败
关闭与计划外放行。

样例 `java/samples/dubbo-consumer` 再往前一步：用真实
`ReferenceConfig` 直连 `dubbo://127.0.0.1:<计数端口>`（`check=false`、
`lazy=true`，不启注册中心与 provider），经真实消费端代理调用。Python 侧在
该端口监听，断言 accept 次数与收到的字节数都是 0；`mvn package` 同时产出
普通 classpath 用的依赖清单和 Spring Boot fat jar，分别覆盖
`AppClassLoader` 与 `LaunchedClassLoader` 两种类加载场景。

样例 `java/samples/spring-boot-dubbo` 是 `agent run` 的 SUT：HTTP 入口
`GET /orders/{id}` 调用 `@DubboReference` 字段声明的 `OrderApi`，
`--dubbo.url` 指向计数端口。注解字段供画像读取；运行期由
`ReferenceConfig` 直连 URL 填同一字段（Dubbo 2.7.x 的注解处理依赖
Spring 4/5 集成，与 Spring Boot 3 不兼容），接口/group/version 与注解一致，
因此 dep_id 与画像产出相同。全链路断言 HTTP 响应体是桩数据、证据
`adapter=dubbo-filter`/`mode=agent_filter`、目标端口零建连零字节。

## 已知边界

- OkHttp 3.14.9 / 4.12.0 / 5.5.1 已实测；`newCall(Request)` 二进制签名相同，
  5.x 需使用 `okhttp-jvm` 坐标与 Kotlin 运行时。
- Apache HttpClient 实测 HC4 4.3.6 / 4.4.1 / 4.5.14 与 HC5 5.0–5.6.5；仅覆盖
  4.3+ 与 5.x classic 的 `InternalHttpClient`，4.0–4.2 的 `AbstractHttpClient`、
  HC5 async 与自定义 `CloseableHttpClient` 子类不在范围内（计划期不会因此标
  supported）。
- 改写发生在 HTTP 边界，不绕过 SUT 的编解码、重试、降级链。
- HTTP/2 prior-knowledge（h2c）不支持：Mock 只讲 HTTP/1.1，改写后的 h2c
  请求会显式失败；负例 e2e 验证此时真实下游 0 连接，不会悄悄回落。
- 签名、pinning、连接池复用、重定向与异步链路的副作用尚未验证。
- Dubbo Filter 只覆盖 `-javaagent` 形态，已实测 Dubbo 2.7.22 与 3.2.19 ×
  JDK 8/11/17/21/26 共 10 个组合；矩阵外版本不承诺。
  `serialization_verified` 固定为 `false`，不宣称序列化保真。classpath
  形态明确不支持。
