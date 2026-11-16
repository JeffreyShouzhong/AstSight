package io.astsight.sample.dubbo;

/** 桩响应要能经 Jackson 构造，因此保留无参构造与 getter/setter。 */
public class OrderView {

    private String orderId;
    private int amount;

    public OrderView() {
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
