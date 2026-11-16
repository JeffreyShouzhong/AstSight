package io.astsight.sample.dubbo;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

/**
 * `agent run` 全链路样例：HTTP 入口调用 Dubbo 下游。
 *
 * <p>启动参数：`--server.port=<port>`、
 * `--dubbo.url=dubbo://127.0.0.1:<port>?lazy=true`。</p>
 */
@SpringBootApplication
@RestController
public class DemoApplication {

    private final OrderClient orderClient;

    public DemoApplication(OrderClient orderClient) {
        this.orderClient = orderClient;
    }

    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }

    @GetMapping("/orders/{id}")
    public OrderView order(@PathVariable String id) {
        return orderClient.create(id);
    }
}
