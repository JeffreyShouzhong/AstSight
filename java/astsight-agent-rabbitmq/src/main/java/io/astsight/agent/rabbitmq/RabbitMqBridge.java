package io.astsight.agent.rabbitmq;

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

/**
 * RabbitMQ 消息通过反射读取，不依赖 rabbitmq-client 类型。
 *
 * <p>消息身份使用 {@code exchange/routingKey}，与用例里的 topic 保持一致；
 * 计划内消息返回 true，调用方跳过真实 publish。</p>
 */
public final class RabbitMqBridge {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private RabbitMqBridge() {
    }

    public static boolean suppress(
            String exchange, String routingKey, Object props, byte[] body) {
        AgentRuntime runtime = AstSightAgent.runtime();
        if (runtime == null) {
            return false;
        }
        String safeExchange = exchange == null ? "" : exchange;
        String safeRouting = routingKey == null ? "" : routingKey;
        String identity = safeExchange + "/" + safeRouting;
        Map<String, String> headers = new LinkedHashMap<String, String>();
        try {
            if (props != null) {
                Method getter = props.getClass().getMethod("getHeaders");
                Object raw = getter.invoke(props);
                if (raw instanceof Map) {
                    for (Object entryItem : ((Map<?, ?>) raw).entrySet()) {
                        Map.Entry<?, ?> entry = (Map.Entry<?, ?>) entryItem;
                        headers.put(
                                String.valueOf(entry.getKey()),
                                decode(entry.getValue()));
                    }
                }
            }
        } catch (Throwable ignored) {
            // header 读取失败不影响消息体断言。
        }
        JsonNode key = TextNode.valueOf(safeRouting);
        JsonNode payload = toNode(body);
        return runtime.publishMq(
                "rabbitmq", identity, key, headers, payload);
    }

    private static JsonNode toNode(byte[] body) {
        if (body == null) {
            return null;
        }
        String text = new String(body, StandardCharsets.UTF_8);
        String trimmed = text.trim();
        if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
            try {
                return MAPPER.readTree(trimmed);
            } catch (Exception ignored) {
                // fall through
            }
        }
        if (printable(text)) {
            return TextNode.valueOf(text);
        }
        return TextNode.valueOf(Base64.getEncoder().encodeToString(body));
    }

    private static String decode(Object value) {
        if (value == null) {
            return "";
        }
        if (value instanceof byte[]) {
            return new String((byte[]) value, StandardCharsets.UTF_8);
        }
        return String.valueOf(value);
    }

    private static boolean printable(String text) {
        for (int index = 0; index < text.length(); index++) {
            char value = text.charAt(index);
            if (value == '�'
                    || (value < 0x20
                    && value != '\n'
                    && value != '\r'
                    && value != '\t')) {
                return false;
            }
        }
        return true;
    }
}
