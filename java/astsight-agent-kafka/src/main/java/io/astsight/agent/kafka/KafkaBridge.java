package io.astsight.agent.kafka;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.TextNode;
import io.astsight.agent.core.AgentRuntime;
import io.astsight.agent.core.AstSightAgent;

import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;

/**
 * 通过反射读取 {@code ProducerRecord}，不依赖 kafka-clients 类型。
 *
 * <p>计划内 topic：上报消息内容并返回已完成的 Future，真实发送被跳过；
 * 计划外 topic：返回 null，继续 Kafka 原始发送逻辑。</p>
 */
public final class KafkaBridge {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private KafkaBridge() {
    }

    public static java.util.concurrent.Future<?> suppressOrNull(
            Object record, Object callback) {
        AgentRuntime runtime = AstSightAgent.runtime();
        if (runtime == null || record == null) {
            return null;
        }
        String topic;
        try {
            topic = String.valueOf(method(record.getClass(), "topic").invoke(record));
        } catch (Throwable failure) {
            runtime.recordError("kafka-producer", failure);
            return null;
        }
        JsonNode key = null;
        JsonNode body = null;
        Map<String, String> headers = new LinkedHashMap<String, String>();
        try {
            key = toNode(method(record.getClass(), "key").invoke(record));
            body = toNode(method(record.getClass(), "value").invoke(record));
            Object rawHeaders = method(record.getClass(), "headers").invoke(record);
            collectHeaders(rawHeaders, headers);
        } catch (Throwable failure) {
            runtime.recordError("kafka-producer", failure);
        }
        boolean handled = runtime.publishMq("kafka", topic, key, headers, body);
        if (!handled) {
            return null;
        }
        invokeCallback(runtime, callback);
        CompletableFuture<Object> future = new CompletableFuture<Object>();
        // 消息被拦截，没有 broker 元数据；get() 返回 null。
        future.complete(null);
        return future;
    }

    private static void invokeCallback(AgentRuntime runtime, Object callback) {
        if (callback == null) {
            return;
        }
        try {
            for (Method candidate : callback.getClass().getMethods()) {
                if ("onCompletion".equals(candidate.getName())
                        && candidate.getParameterTypes().length == 2) {
                    candidate.invoke(callback, null, null);
                    return;
                }
            }
        } catch (Throwable failure) {
            runtime.recordError("kafka-producer", failure);
        }
    }

    private static void collectHeaders(Object rawHeaders, Map<String, String> target) {
        if (rawHeaders == null) {
            return;
        }
        try {
            Object iterator = method(rawHeaders.getClass(), "iterator").invoke(rawHeaders);
            if (!(iterator instanceof java.util.Iterator)) {
                return;
            }
            java.util.Iterator<?> items = (java.util.Iterator<?>) iterator;
            while (items.hasNext()) {
                Object header = items.next();
                if (header == null) {
                    continue;
                }
                Object name = method(header.getClass(), "key").invoke(header);
                Object value = method(header.getClass(), "value").invoke(header);
                target.put(
                        String.valueOf(name),
                        decodeText(value));
            }
        } catch (Throwable ignored) {
            // header 读取失败不影响消息体断言。
        }
    }

    private static JsonNode toNode(Object value) throws Exception {
        if (value == null) {
            return null;
        }
        if (value instanceof byte[]) {
            byte[] raw = (byte[]) value;
            String text = new String(raw, StandardCharsets.UTF_8);
            return textOrJson(text, raw);
        }
        if (value instanceof CharSequence) {
            String text = value.toString();
            JsonNode parsed = tryJson(text);
            return parsed == null ? TextNode.valueOf(text) : parsed;
        }
        return MAPPER.valueToTree(value);
    }

    private static JsonNode textOrJson(String text, byte[] raw) {
        JsonNode parsed = tryJson(text);
        if (parsed != null) {
            return parsed;
        }
        if (isPrintable(text)) {
            return TextNode.valueOf(text);
        }
        return TextNode.valueOf(Base64.getEncoder().encodeToString(raw));
    }

    private static JsonNode tryJson(String text) {
        String trimmed = text == null ? "" : text.trim();
        if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) {
            return null;
        }
        try {
            return MAPPER.readTree(trimmed);
        } catch (Exception ignored) {
            return null;
        }
    }

    private static String decodeText(Object value) {
        if (value == null) {
            return "";
        }
        if (value instanceof byte[]) {
            return new String((byte[]) value, StandardCharsets.UTF_8);
        }
        return String.valueOf(value);
    }

    private static boolean isPrintable(String text) {
        for (int index = 0; index < text.length(); index++) {
            char value = text.charAt(index);
            if (value == '�' || (value < 0x20 && value != '\n' && value != '\r' && value != '\t')) {
                return false;
            }
        }
        return true;
    }

    private static Method method(Class<?> type, String name) throws NoSuchMethodException {
        return type.getMethod(name);
    }
}
