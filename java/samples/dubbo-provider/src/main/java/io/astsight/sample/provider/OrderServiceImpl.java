package io.astsight.sample.provider;

import org.apache.dubbo.config.annotation.DubboService;

import java.time.LocalDateTime;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 提供方实现。`@DubboService` 主要供 AstSight 静态画像读取；
 * 实际导出在 {@link DemoApplication} 里用 ServiceConfig 完成
 * （Dubbo 2.7 的 Spring 注解集成与 Spring Boot 3 不兼容）。
 */
@DubboService(group = "order", version = "1.0.0")
public class OrderServiceImpl implements OrderService {

    private final AtomicInteger sequence = new AtomicInteger();

    @Override
    public OrderView create(CreateOrderRequest request) {
        int quantity = request.getQuantity() == null ? 0 : request.getQuantity();
        String sku = request.getSku() == null ? "" : request.getSku();
        OrderStatus status = request.getStatus() == null
                ? OrderStatus.NEW
                : request.getStatus();
        long amount = quantity * 100L;
        return new OrderView(
                "O-" + sequence.incrementAndGet(),
                sku,
                quantity,
                amount,
                status,
                LocalDateTime.now());
    }

    @Override
    public OrderView get(String orderId) {
        return new OrderView(
                orderId,
                "SKU-1",
                1,
                100L,
                OrderStatus.NEW,
                LocalDateTime.of(2024, 1, 1, 0, 0));
    }

    @Override
    public String echo(String value) {
        return "echo:" + value;
    }

    @Override
    public String statusName(OrderStatus status) {
        return status == null ? "null" : status.name();
    }

    @Override
    public String ping() {
        return "pong";
    }
}
