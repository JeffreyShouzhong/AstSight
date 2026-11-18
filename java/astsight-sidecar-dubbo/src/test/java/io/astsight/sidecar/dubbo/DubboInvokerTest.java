package io.astsight.sidecar.dubbo;

import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import org.apache.dubbo.config.ApplicationConfig;
import org.apache.dubbo.config.ProtocolConfig;
import org.apache.dubbo.config.RegistryConfig;
import org.apache.dubbo.config.ServiceConfig;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

import java.net.ServerSocket;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 真 provider + 真泛化调用。provider 和 sidecar 在同一个 JVM 里，但调用走
 * `dubbo://127.0.0.1:<port>`，仍然验证网络序列化与泛化参数还原。
 */
final class DubboInvokerTest {

    private static ServiceConfig<EchoService> serviceConfig;
    private static DubboInvoker invoker;
    private static int port;
    private static ApplicationConfig application;

    @BeforeAll
    static void startProvider() throws Exception {
        port = freePort();

        application = new ApplicationConfig("astsight-sidecar-test-provider");
        application.setQosEnable(Boolean.FALSE);

        ProtocolConfig protocol = new ProtocolConfig("dubbo", port);
        protocol.setSerialization("hessian2");

        serviceConfig = new ServiceConfig<EchoService>();
        serviceConfig.setApplication(application);
        serviceConfig.setProtocol(protocol);
        // N/A 表示不注册，测试只走直连 URL。
        serviceConfig.setRegistry(new RegistryConfig("N/A"));
        serviceConfig.setRegister(Boolean.FALSE);
        serviceConfig.setInterface(EchoService.class);
        serviceConfig.setGroup("order");
        serviceConfig.setVersion("1.0.0");
        serviceConfig.setRef(new EchoServiceImpl());
        serviceConfig.export();

        assertFalse(serviceConfig.isUnexported());
        assertNotNull(serviceConfig.getExportedUrls());
        port = serviceConfig.getExportedUrls().get(0).getPort();

        invoker = new DubboInvoker(application);
    }

    @AfterAll
    static void stopProvider() {
        if (invoker != null) {
            invoker.close();
        }
        if (serviceConfig != null) {
            serviceConfig.unexport();
        }
    }

    @Test
    void invokesScalarMethod() {
        InvokeOutcome outcome = invoker.invoke(
                request("echo", new String[]{"java.lang.String"}, "hi"));

        assertTrue(outcome.ok, outcome.message);
        assertEquals("echo:hi", outcome.value);
        assertEquals(InvokeOutcome.OK, outcome.rpcStatus);
    }

    @Test
    void invokesPrimitiveParameters() {
        InvokeOutcome outcome = invoker.invoke(
                request("add", new String[]{"int", "int"}, 2, 3));

        assertTrue(outcome.ok, outcome.message);
        assertEquals(Integer.valueOf(5), outcome.value);
    }

    @Test
    @SuppressWarnings("unchecked")
    void genericInvocationRealizesPojoArgumentAndResponse() {
        ObjectNode argument = DubboInvoker.MAPPER.createObjectNode();
        argument.put("name", "Ada");

        InvokeOutcome outcome = invoker.invoke(request(
                "describe",
                new String[]{EchoRequest.class.getName()},
                argument));

        assertTrue(outcome.ok, outcome.message);
        Map<String, Object> reply = (Map<String, Object>) outcome.value;
        assertEquals("hello Ada", reply.get("message"));
    }

    @Test
    @SuppressWarnings("unchecked")
    void genericInvocationRealizesTemporalDtoField() {
        ObjectNode argument = DubboInvoker.MAPPER.createObjectNode();
        argument.put("name", "Ada");
        argument.put("birthday", "2024-01-02");

        InvokeOutcome outcome = invoker.invoke(request(
                "birthday",
                new String[]{EchoRequest.class.getName()},
                argument));

        assertTrue(outcome.ok, outcome.message);
        assertEquals("2024-01-02", outcome.value);
    }

    @Test
    void attachmentsReachProvider() {
        InvokeOutcome outcome = invoker.invoke(request(
                "attachment",
                new String[]{"java.lang.String"},
                "tenant",
                attachment("tenant", "t-1")));

        assertTrue(outcome.ok, outcome.message);
        assertEquals("t-1", outcome.value);
    }

    @Test
    void argumentTypeMismatchIsBadRequest() {
        InvokeOutcome outcome = invoker.invoke(
                request("add", new String[]{"int", "int"}, "abc", 3));

        assertFalse(outcome.ok);
        assertEquals("bad_request", outcome.error);
        assertEquals(InvokeOutcome.BAD_REQUEST, outcome.rpcStatus);
    }

    @Test
    void timeoutIsNormalized() {
        InvokeOutcome outcome = invoker.invoke(request(
                "slow",
                new String[]{"long"},
                new Object[]{Long.valueOf(500)},
                null,
                100));

        assertFalse(outcome.ok);
        assertEquals(InvokeOutcome.TIMEOUT, outcome.rpcStatus);
        assertEquals("rpc_timeout", outcome.error);
        assertTrue(outcome.elapsedMs >= 100);
    }

    private static InvokeRequest request(
            String method, String[] paramTypes, Object... arguments) {
        return request(method, paramTypes, arguments, null, 3_000);
    }

    private static InvokeRequest request(
            String method,
            String[] paramTypes,
            Object argument,
            Map<String, String> attachments) {
        return request(
                method, paramTypes, new Object[]{argument}, attachments, 3_000);
    }

    private static InvokeRequest request(
            String method,
            String[] paramTypes,
            Object[] arguments,
            Map<String, String> attachments,
            int timeoutMs) {
        ObjectNode node = DubboInvoker.MAPPER.createObjectNode();
        node.put("id", "test");
        node.put("op", "invoke");
        node.put("interface", EchoService.class.getName());
        node.put("method", method);
        ArrayNode types = node.putArray("param_types");
        for (String type : paramTypes) {
            types.add(type);
        }
        ArrayNode args = node.putArray("arguments");
        for (Object argument : arguments) {
            args.add(DubboInvoker.MAPPER.valueToTree(argument));
        }
        node.put("url", "dubbo://127.0.0.1:" + port);
        node.put("group", "order");
        node.put("version", "1.0.0");
        node.put("timeout_ms", timeoutMs);
        if (attachments != null) {
            ObjectNode values = node.putObject("attachments");
            for (Map.Entry<String, String> entry : attachments.entrySet()) {
                values.put(entry.getKey(), entry.getValue());
            }
        }
        try {
            return InvokeRequest.parse(node);
        } catch (RequestException failure) {
            throw new AssertionError(failure);
        }
    }

    private static Map<String, String> attachment(String key, String value) {
        Map<String, String> values = new java.util.LinkedHashMap<String, String>();
        values.put(key, value);
        return values;
    }

    private static int freePort() throws Exception {
        ServerSocket socket = new ServerSocket(0);
        try {
            return socket.getLocalPort();
        } finally {
            socket.close();
        }
    }
}
