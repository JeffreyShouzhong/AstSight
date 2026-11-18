# Dubbo Triple Provider 样例

使用 Dubbo 3.2.19 的 Triple 协议暴露一个 unary 服务，不使用注册中心：

```text
/io.astsight.triple.InventoryService/Reserve
```

构建与启动：

```bash
cd java/samples/dubbo-triple-provider
mvn -q -DskipTests package
java -jar target/dubbo-triple-provider.jar 50053
```

标准 gRPC-compatible 调用由引擎的 `TripleUnaryCaller` 完成，参数来自
`src/main/proto/inventory.proto`。
