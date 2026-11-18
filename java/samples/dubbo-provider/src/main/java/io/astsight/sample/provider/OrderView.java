package io.astsight.sample.provider;

import java.time.LocalDateTime;

public class OrderView {

    private String orderId;
    private String sku;
    private Integer quantity;
    private Long amount;
    private OrderStatus status;
    private LocalDateTime createdAt;

    public OrderView() {
    }

    public OrderView(
            String orderId,
            String sku,
            Integer quantity,
            Long amount,
            OrderStatus status,
            LocalDateTime createdAt) {
        this.orderId = orderId;
        this.sku = sku;
        this.quantity = quantity;
        this.amount = amount;
        this.status = status;
        this.createdAt = createdAt;
    }

    public String getOrderId() {
        return orderId;
    }

    public void setOrderId(String orderId) {
        this.orderId = orderId;
    }

    public String getSku() {
        return sku;
    }

    public void setSku(String sku) {
        this.sku = sku;
    }

    public Integer getQuantity() {
        return quantity;
    }

    public void setQuantity(Integer quantity) {
        this.quantity = quantity;
    }

    public Long getAmount() {
        return amount;
    }

    public void setAmount(Long amount) {
        this.amount = amount;
    }

    public OrderStatus getStatus() {
        return status;
    }

    public void setStatus(OrderStatus status) {
        this.status = status;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }

    public void setCreatedAt(LocalDateTime createdAt) {
        this.createdAt = createdAt;
    }
}
