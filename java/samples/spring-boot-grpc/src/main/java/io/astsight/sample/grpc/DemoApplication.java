package io.astsight.sample.grpc;

import io.astsight.sample.inventory.InventoryServiceGrpc;
import io.astsight.sample.inventory.ReserveRequest;
import io.astsight.sample.inventory.ReserveResponse;
import io.grpc.ManagedChannel;
import io.grpc.ManagedChannelBuilder;
import jakarta.annotation.PreDestroy;
import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/** `agent run` 全链路样例：HTTP 入口经 Java gRPC stub 调用下游。 */
@SpringBootApplication
@RestController
public class DemoApplication {

    private final InventoryClient inventoryClient;

    public DemoApplication(InventoryClient inventoryClient) {
        this.inventoryClient = inventoryClient;
    }

    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }

    @GetMapping("/health")
    public Map<String, Object> health() {
        return Map.of("status", "ok");
    }

    @GetMapping("/orders")
    public Map<String, Object> order() {
        ReserveResponse response = inventoryClient.reserve("SKU-1");
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("available", response.getAvailable());
        body.put("reservation_id", response.getReservationId());
        return body;
    }
}

/** 配置键必须与画像规范化的环境变量名一致。 */
@Component
class InventoryClient {

    private final ManagedChannel channel;
    private final InventoryServiceGrpc.InventoryServiceBlockingStub stub;

    InventoryClient(@Value("${inventory.grpc.addr}") String target) {
        channel = ManagedChannelBuilder.forTarget(target).usePlaintext().build();
        stub = InventoryServiceGrpc.newBlockingStub(channel);
    }

    ReserveResponse reserve(String sku) {
        return stub.reserve(ReserveRequest.newBuilder().setSku(sku).build());
    }

    @PreDestroy
    void close() {
        channel.shutdownNow();
    }
}
