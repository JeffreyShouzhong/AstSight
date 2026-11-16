# Spring Boot + OkHttp agent 示例

用于验证 Spring Boot fat jar / `LaunchedURLClassLoader` 下 agent 的类可见性与
URL 改写。controller `GET /call` 会请求
`https://api.example.com/orders/7?trace=boot`；agent 激活时应改写为 Mock 的
`/_dep/<route_key>/orders/7?trace=boot`。

```bash
cd java/samples/spring-boot-okhttp
mvn -q -DskipTests package

java -javaagent:../../astsight-agent/target/astsight-agent.jar=\
admin=http://127.0.0.1:36000,token=<token>,sut_id=sut-boot \
  -jar target/spring-boot-okhttp-sample-0.1.0-SNAPSHOT.jar
curl http://127.0.0.1:8080/call
```

注意：未启用 agent 时该请求会真实访问 `api.example.com`；只在测试 Mock 环境使用。
