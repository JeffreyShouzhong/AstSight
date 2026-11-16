package io.astsight.agent.dubbo;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

/** Dubbo dep_id 与 Mock/会话绑定使用同一套字符串。 */
final class DubboBridgeTest {

    @Test
    void depIdIncludesGroupAndVersionInStableOrder() {
        assertEquals(
                "dubbo:com.acme.OrderService#create?group=order&version=1.0.0",
                DubboBridge.depId(
                        "com.acme.OrderService", "create", "order", "1.0.0"));
    }

    @Test
    void depIdOmitsEmptyParameters() {
        assertEquals(
                "dubbo:com.acme.OrderService#create",
                DubboBridge.depId("com.acme.OrderService", "create", null, ""));
        assertEquals(
                "dubbo:com.acme.OrderService#create?group=order",
                DubboBridge.depId("com.acme.OrderService", "create", "order", null));
        assertEquals(
                "dubbo:com.acme.OrderService#create?version=2",
                DubboBridge.depId("com.acme.OrderService", "create", "", "2"));
    }
}
