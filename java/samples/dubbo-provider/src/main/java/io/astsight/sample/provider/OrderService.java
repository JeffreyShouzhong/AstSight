package io.astsight.sample.provider;

public interface OrderService {

    OrderView create(CreateOrderRequest request);

    OrderView get(String orderId);

    String echo(String value);

    String statusName(OrderStatus status);

    String ping();
}
