package io.astsight.agent.dubbo;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

/** Dubbo dep_id 与 Mock/会话绑定使用同一套字符串。 */
final class DubboBridgeTest {

    @Test
    void depIdIncludesParameterSignature() {
        assertEquals(
                "dubbo:com.acme.OrderService#create(java.lang.String)"
                        + "?group=order&version=1.0.0",
                DubboBridge.depId(
                        "com.acme.OrderService",
                        "create",
                        new String[]{"java.lang.String"},
                        "order",
                        "1.0.0"));
        assertEquals(
                "dubbo:com.acme.OrderService#create()",
                DubboBridge.depId(
                        "com.acme.OrderService", "create", new String[0], null, null));
        assertEquals(
                "dubbo:com.acme.OrderService#create(java.lang.String,int[])",
                DubboBridge.depId(
                        "com.acme.OrderService",
                        "create",
                        new String[]{"java.lang.String", "int[]"},
                        null,
                        null));
    }

    @Test
    void depIdDistinguishesOverloads() {
        assertEquals(
                "dubbo:com.acme.OrderService#create(java.lang.String)",
                DubboBridge.depId(
                        "com.acme.OrderService",
                        "create",
                        new String[]{"java.lang.String"},
                        null,
                        null));
        assertEquals(
                "dubbo:com.acme.OrderService#create(java.lang.String,int)",
                DubboBridge.depId(
                        "com.acme.OrderService",
                        "create",
                        new String[]{"java.lang.String", "int"},
                        null,
                        null));
    }

    @Test
    void depIdOmitsEmptyParameters() {
        assertEquals(
                "dubbo:com.acme.OrderService#create()",
                DubboBridge.depId("com.acme.OrderService", "create", null, null, ""));
        assertEquals(
                "dubbo:com.acme.OrderService#create()?group=order",
                DubboBridge.depId(
                        "com.acme.OrderService", "create", new String[0], "order", null));
        assertEquals(
                "dubbo:com.acme.OrderService#create()?version=2",
                DubboBridge.depId(
                        "com.acme.OrderService", "create", new String[0], "", "2"));
    }
}
