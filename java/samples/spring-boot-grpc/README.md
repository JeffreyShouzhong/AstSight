# Spring Boot gRPC SUT 样例

该样例模拟被测应用：

```text
GET /orders
→ InventoryServiceBlockingStub
→ InventoryService/Reserve
```

下游 target 来自 `@Value("${inventory.grpc.addr}")`；静态画像会规范化为
`INVENTORY_GRPC_ADDR`，`agent run` 启动 SUT 前把该环境变量指向 gripmock。

构建：

```bash
cd java/samples/spring-boot-grpc
mvn -q -DskipTests package
```

真实全链路 e2e：

```bash
export GRIPMOCK_BIN=/path/to/gripmock
cd engine
python -m pytest tests/integration/test_java_grpc_agent_run_e2e.py -q
```

测试要求本机有可执行的 `java` 和已构建的 Spring Boot fat jar。
