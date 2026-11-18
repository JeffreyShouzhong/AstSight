# Dubbo Provider Sample

常驻的 Dubbo 提供方样例，供 AstSight 的 sidecar 泛化调用与 Dubbo 入口用例使用。

## 提供什么

- 接口：`io.astsight.sample.provider.OrderService`
- group：`order`
- version：`1.0.0`
- 序列化：Hessian2
- 注册中心：不启（直连 `dubbo://127.0.0.1:<port>`）

方法：

| 方法 | 用途 |
|---|---|
| `OrderView create(CreateOrderRequest)` | DTO、约束、枚举、时间字段 |
| `OrderView get(String)` | 简单返回 |
| `String echo(String)` | 标量参数 |
| `String statusName(OrderStatus)` | 枚举参数 |
| `String ping()` | 无参数 |

`CreateOrderRequest` 带 `@NotBlank`、`@Size`、`@Min`、`@Max`、`@Email`、
`@Pattern` 等注解，用于验证 AstSight 的 DTO 字段/约束采样。

## 构建与启动

```bash
cd java/samples/dubbo-provider
mvn -q -DskipTests package

java -jar target/dubbo-provider-sample-0.1.0-SNAPSHOT.jar
# READY 20880
```

端口来自 `src/main/resources/application.yml` 的 `dubbo.protocol.port`，
也可以覆盖：

```bash
java -jar target/dubbo-provider-sample-0.1.0-SNAPSHOT.jar \
  --dubbo.protocol.port=20881
```

## 用 AstSight 跑

另开一个终端：

```bash
cd engine
PYTHONPATH=. python -m astsight run \
  --project ../java/samples/dubbo-provider \
  --mode full \
  --codegraph off \
  --dubbo-url dubbo://127.0.0.1:20880
```

这会扫描 `@DubboService` 提供方，生成 Dubbo 模板用例，启动 sidecar 调用并输出报告。

`@DubboService` 注解只用于 AstSight 静态画像；实际导出由 `DemoApplication`
里的 `ServiceConfig` 完成（Dubbo 2.7 的 Spring 注解集成与 Spring Boot 3 不兼容）。
