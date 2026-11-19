package io.astsight.agent.rocketmq;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.TextNode;
import io.astsight.agent.core.AgentRuntime;
import io.astsight.agent.core.AstSightAgent;

import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * RocketMQ Message 通过反射读取，不依赖 rocketmq-client 类型。
 *
 * <p>同步发送被拦截时返回 {@code Object[]{SendResult}}；SendResult 构造失败时
 * 返回 {@code Object[]{null}}，仍然抑制真实发送。异步发送会尽力回调
 * {@code SendCallback.onSuccess(result)}。</p>
 */
public final class RocketMqBridge {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final String SEND_RESULT =
            "org.apache.rocketmq.client.producer.SendResult";
    private static final String SEND_STATUS =
            "org.apache.rocketmq.client.producer.SendStatus";
    private static final String MESSAGE_QUEUE =
            "org.apache.rocketmq.common.message.MessageQueue";

    private RocketMqBridge() {
    }

    /** 返回 null 表示计划外；否则返回长度为 1 的数组，元素可为 null。 */
    public static Object[] sync(Object message) {
        AgentRuntime runtime = AstSightAgent.runtime();
        if (runtime == null || message == null) {
            return null;
        }
        String topic;
        try {
            topic = String.valueOf(method(message, "getTopic").invoke(message));
        } catch (Throwable failure) {
            runtime.recordError("rocketmq-producer", failure);
            return null;
        }
        boolean handled = runtime.publishMq(
                "rocketmq",
                topic,
                keyNode(message),
                headers(message),
                bodyNode(message));
        if (!handled) {
            return null;
        }
        return new Object[]{sendResult(message, topic)};
    }

    /** 异步发送：返回 true 表示已拦截并已回调。 */
    public static boolean async(Object message, Object callback) {
        AgentRuntime runtime = AstSightAgent.runtime();
        if (runtime == null || message == null) {
            return false;
        }
        String topic;
        try {
            topic = String.valueOf(method(message, "getTopic").invoke(message));
        } catch (Throwable failure) {
            runtime.recordError("rocketmq-producer", failure);
            return false;
        }
        boolean handled = runtime.publishMq(
                "rocketmq",
                topic,
                keyNode(message),
                headers(message),
                bodyNode(message));
        if (!handled) {
            return false;
        }
        invokeSuccess(runtime, callback, sendResult(message, topic));
        return true;
    }

    private static JsonNode keyNode(Object message) {
        try {
            Object keys = method(message, "getKeys").invoke(message);
            return keys == null ? null : TextNode.valueOf(String.valueOf(keys));
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static Map<String, String> headers(Object message) {
        Map<String, String> out = new LinkedHashMap<String, String>();
        try {
            Object raw = method(message, "getProperties").invoke(message);
            if (raw instanceof Map) {
                for (Object item : ((Map<?, ?>) raw).entrySet()) {
                    Map.Entry<?, ?> entry = (Map.Entry<?, ?>) item;
                    out.put(String.valueOf(entry.getKey()),
                            String.valueOf(entry.getValue()));
                }
            }
        } catch (Throwable ignored) {
            // properties 读取失败不影响消息体断言。
        }
        return out;
    }

    private static JsonNode bodyNode(Object message) {
        try {
            Object body = method(message, "getBody").invoke(message);
            if (!(body instanceof byte[])) {
                return body == null ? null : MAPPER.valueToTree(body);
            }
            byte[] raw = (byte[]) body;
            String text = new String(raw, StandardCharsets.UTF_8);
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
            return TextNode.valueOf(Base64.getEncoder().encodeToString(raw));
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static Object sendResult(Object message, String topic) {
        ClassLoader loader = message.getClass().getClassLoader();
        try {
            Class<?> resultClass = Class.forName(SEND_RESULT, false, loader);
            Class<?> statusClass = Class.forName(SEND_STATUS, false, loader);
            Class<?> queueClass = Class.forName(MESSAGE_QUEUE, false, loader);
            Object status = statusClass.getField("SEND_OK").get(null);
            Object queue = messageQueue(queueClass, topic);
            for (Constructor<?> constructor : resultClass.getConstructors()) {
                Class<?>[] types = constructor.getParameterTypes();
                if (types.length == 0 || types[0] != statusClass) {
                    continue;
                }
                Object[] args = arguments(types, status, queue);
                if (args == null) {
                    continue;
                }
                try {
                    return constructor.newInstance(args);
                } catch (Throwable ignored) {
                    // 尝试下一个版本构造器。
                }
            }
        } catch (Throwable ignored) {
            // 构造失败时仍然抑制发送；SUT 拿到 null SendResult。
        }
        return null;
    }

    private static Object messageQueue(Class<?> queueClass, String topic) {
        for (Constructor<?> constructor : queueClass.getConstructors()) {
            Class<?>[] types = constructor.getParameterTypes();
            Object[] args = new Object[types.length];
            boolean ok = true;
            for (int index = 0; index < types.length; index++) {
                if (types[index] == String.class) {
                    args[index] = index == 0 ? topic : "astsight";
                } else if (types[index] == int.class) {
                    args[index] = 0;
                } else {
                    ok = false;
                    break;
                }
            }
            if (!ok) {
                continue;
            }
            try {
                return constructor.newInstance(args);
            } catch (Throwable ignored) {
                // 尝试下一个构造器。
            }
        }
        return null;
    }

    private static Object[] arguments(
            Class<?>[] types, Object status, Object queue) {
        Object[] args = new Object[types.length];
        for (int index = 0; index < types.length; index++) {
            if (index == 0) {
                args[index] = status;
            } else if (types[index] == String.class) {
                args[index] = "astsight-msg";
            } else if (types[index] == long.class) {
                args[index] = 0L;
            } else if (types[index] == int.class) {
                args[index] = 0;
            } else if (types[index] == boolean.class) {
                args[index] = false;
            } else if (queue != null && types[index].isInstance(queue)) {
                args[index] = queue;
            } else {
                args[index] = null;
            }
        }
        return args;
    }

    private static void invokeSuccess(
            AgentRuntime runtime, Object callback, Object result) {
        if (callback == null) {
            return;
        }
        try {
            for (Method method : callback.getClass().getMethods()) {
                if ("onSuccess".equals(method.getName())
                        && method.getParameterTypes().length == 1) {
                    method.invoke(callback, result);
                    return;
                }
            }
        } catch (Throwable failure) {
            runtime.recordError("rocketmq-producer", failure);
        }
    }

    private static Method method(Object target, String name)
            throws NoSuchMethodException {
        return target.getClass().getMethod(name);
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
