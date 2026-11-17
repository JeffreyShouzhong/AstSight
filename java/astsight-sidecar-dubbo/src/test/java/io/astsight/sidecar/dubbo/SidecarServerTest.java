package io.astsight.sidecar.dubbo;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import org.junit.jupiter.api.Test;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.io.StringReader;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** JSONL 协议层：不启动 Dubbo，调用方用假实现。 */
final class SidecarServerTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void pingReportsProtocolAndVersions() throws Exception {
        List<JsonNode> responses = run(
                "{\"id\":\"1\",\"op\":\"ping\"}\n",
                request -> InvokeOutcome.failure("unused", "OK", null));

        assertEquals(1, responses.size());
        JsonNode response = responses.get(0);
        assertTrue(response.path("ok").asBoolean());
        assertEquals(SidecarServer.PROTOCOL, response.path("protocol").asInt());
        assertEquals("1", response.path("id").asText());
        assertFalse(response.path("sidecar_version").asText().isEmpty());
    }

    @Test
    void invokeReturnsValueAndElapsed() throws Exception {
        List<JsonNode> responses = run(
                "{\"id\":\"2\",\"op\":\"invoke\",\"interface\":\"com.acme.OrderApi\","
                        + "\"method\":\"get\",\"param_types\":[\"java.lang.String\"],"
                        + "\"arguments\":[\"O-1\"],\"url\":\"dubbo://127.0.0.1:20880\"}\n",
                request -> {
                    assertEquals("com.acme.OrderApi", request.service);
                    assertEquals("get", request.method);
                    assertEquals("dubbo://127.0.0.1:20880", request.url);
                    return InvokeOutcome.success("ok");
                });

        JsonNode response = responses.get(0);
        assertTrue(response.path("ok").asBoolean());
        assertEquals("OK", response.path("rpc_status").asText());
        assertEquals("ok", response.path("value").asText());
        assertTrue(response.has("elapsed_ms"));
    }

    @Test
    void nullReturnValueIsJsonNull() throws Exception {
        List<JsonNode> responses = run(
                "{\"id\":\"2b\",\"op\":\"invoke\",\"interface\":\"com.acme.OrderApi\","
                        + "\"method\":\"ping\",\"param_types\":[],\"arguments\":[],"
                        + "\"url\":\"dubbo://127.0.0.1:20880\"}\n",
                request -> InvokeOutcome.success(null));

        JsonNode response = responses.get(0);
        assertTrue(response.path("ok").asBoolean());
        assertTrue(response.has("value"));
        assertTrue(response.get("value").isNull());
    }

    @Test
    void rpcFailureKeepsStructuredStatus() throws Exception {
        List<JsonNode> responses = run(
                "{\"id\":\"3\",\"op\":\"invoke\",\"interface\":\"com.acme.OrderApi\","
                        + "\"method\":\"get\",\"param_types\":[],\"arguments\":[],"
                        + "\"url\":\"dubbo://127.0.0.1:20880\"}\n",
                request -> {
                    InvokeOutcome outcome = InvokeOutcome.failure(
                            "rpc_timeout", InvokeOutcome.TIMEOUT, "waiting for response");
                    outcome.exceptionType = "org.apache.dubbo.rpc.RpcException";
                    return outcome;
                });

        JsonNode response = responses.get(0);
        assertFalse(response.path("ok").asBoolean());
        assertEquals("rpc_timeout", response.path("error").asText());
        assertEquals("TIMEOUT", response.path("rpc_status").asText());
        assertEquals(
                "org.apache.dubbo.rpc.RpcException",
                response.path("exception_type").asText());
    }

    @Test
    void missingInterfaceIsBadRequestAndDoesNotCallInvoker() throws Exception {
        List<JsonNode> responses = run(
                "{\"id\":\"4\",\"op\":\"invoke\",\"method\":\"get\","
                        + "\"arguments\":[],\"url\":\"dubbo://127.0.0.1:20880\"}\n",
                request -> {
                    throw new AssertionError("不应调用执行器");
                });

        JsonNode response = responses.get(0);
        assertFalse(response.path("ok").asBoolean());
        assertEquals("bad_request", response.path("error").asText());
    }

    @Test
    void argumentCountMustMatchParamTypes() throws Exception {
        List<JsonNode> responses = run(
                "{\"id\":\"5\",\"op\":\"invoke\",\"interface\":\"com.acme.OrderApi\","
                        + "\"method\":\"get\",\"param_types\":[\"java.lang.String\"],"
                        + "\"arguments\":[],\"url\":\"dubbo://127.0.0.1:20880\"}\n",
                request -> {
                    throw new AssertionError("不应调用执行器");
                });

        assertEquals("bad_request", responses.get(0).path("error").asText());
    }

    @Test
    void registryIsExplicitlyUnsupported() throws Exception {
        List<JsonNode> responses = run(
                "{\"id\":\"6\",\"op\":\"invoke\",\"interface\":\"com.acme.OrderApi\","
                        + "\"method\":\"get\",\"arguments\":[],"
                        + "\"registry\":\"nacos://127.0.0.1:8848\"}\n",
                request -> {
                    throw new AssertionError("不应调用执行器");
                });

        assertEquals(
                "registry_not_supported", responses.get(0).path("error").asText());
    }

    @Test
    void nonHessianSerializationIsRejected() throws Exception {
        List<JsonNode> responses = run(
                "{\"id\":\"7\",\"op\":\"invoke\",\"interface\":\"com.acme.OrderApi\","
                        + "\"method\":\"get\",\"arguments\":[],"
                        + "\"url\":\"dubbo://127.0.0.1:20880\",\"serialization\":\"kryo\"}\n",
                request -> {
                    throw new AssertionError("不应调用执行器");
                });

        assertEquals(
                "unsupported_serialization", responses.get(0).path("error").asText());
    }

    @Test
    void badJsonGetsIdWhenPresent() throws Exception {
        List<JsonNode> responses = run("{not json\n", request -> null);

        assertEquals("bad_json", responses.get(0).path("error").asText());
        assertTrue(responses.get(0).path("id").isMissingNode());
    }

    @Test
    void shutdownStopsReadingFurtherLines() throws Exception {
        List<JsonNode> responses = run(
                "{\"id\":\"8\",\"op\":\"shutdown\"}\n"
                        + "{\"id\":\"9\",\"op\":\"ping\"}\n",
                request -> null);

        assertEquals(1, responses.size());
        assertTrue(responses.get(0).path("ok").asBoolean());
    }

    @Test
    void scalarArgumentsFollowDeclaredTypes() {
        assertEquals("x", InvokeRequest.convertArgument(
                MAPPER.valueToTree("x"), "java.lang.String"));
        assertEquals(Integer.valueOf(7), InvokeRequest.convertArgument(
                MAPPER.valueToTree(7), "int"));
        assertEquals(Boolean.TRUE, InvokeRequest.convertArgument(
                MAPPER.valueToTree(true), "boolean"));
        assertNull(InvokeRequest.convertArgument(MAPPER.nullNode(), "java.lang.String"));
    }

    @Test
    void temporalAndCommonJdkArgumentsAreConverted() {
        assertEquals(
                LocalDate.of(2024, 1, 2),
                InvokeRequest.convertArgument(
                        MAPPER.valueToTree("2024-01-02"), "java.time.LocalDate"));
        assertEquals(
                Instant.parse("2024-01-02T03:04:05Z"),
                InvokeRequest.convertArgument(
                        MAPPER.valueToTree("2024-01-02T03:04:05Z"), "java.time.Instant"));
        assertEquals(
                Date.from(Instant.parse("2024-01-02T00:00:00Z")),
                InvokeRequest.convertArgument(
                        MAPPER.valueToTree("2024-01-02T00:00:00"), "java.util.Date"));
        assertEquals(
                UUID.fromString("00000000-0000-0000-0000-000000000001"),
                InvokeRequest.convertArgument(
                        MAPPER.valueToTree("00000000-0000-0000-0000-000000000001"),
                        "java.util.UUID"));
        assertEquals(
                URI.create("https://example.com"),
                InvokeRequest.convertArgument(
                        MAPPER.valueToTree("https://example.com"), "java.net.URI"));
    }

    private static List<JsonNode> run(String input, DubboCaller caller) throws Exception {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        PrintStream out = new PrintStream(bytes, true, "UTF-8");
        boolean stopped = new SidecarServer(caller).run(
                new StringReader(input), out);
        List<JsonNode> responses = new ArrayList<JsonNode>();
        String text = new String(bytes.toByteArray(), StandardCharsets.UTF_8);
        for (String line : text.split("\\R")) {
            if (!line.trim().isEmpty()) {
                responses.add(MAPPER.readTree(line));
            }
        }
        assertTrue(stopped || !input.contains("shutdown"));
        return responses;
    }
}
