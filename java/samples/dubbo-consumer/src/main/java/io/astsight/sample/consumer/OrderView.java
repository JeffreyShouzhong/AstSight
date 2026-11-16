package io.astsight.sample.consumer;

/** 返回 DTO：agent 按接口声明的返回类型用 Jackson 构造桩对象。 */
public class OrderView {

    private String orderId;
    private int amount;

    public OrderView() {
    }

    public OrderView(String orderId, int amount) {
        this.orderId = orderId;
        this.amount = amount;
    }

    public String getOrderId() {
        return orderId;
    }

    public void setOrderId(String orderId) {
        this.orderId = orderId;
    }

    public int getAmount() {
        return amount;
    }

    public void setAmount(int amount) {
        this.amount = amount;
    }
}
