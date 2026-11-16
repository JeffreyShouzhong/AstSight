package io.astsight.agent.dubbo;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import io.astsight.agent.core.AgentRuntime;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.reflect.Type;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CompletableFuture;

/**
 * Filter 与 agent 运行时之间的桥。这个类由父类加载器加载，只依赖 JDK、
 * Jackson 与 agent core，不引用任何 Dubbo 类型；Dubbo 对象一律用反射访问。
 *
 * <p>调用语义：
 * <ul>
 *   <li>非计划内下游：放行真实调用（返回 {@code null}）；</li>
 *   <li>计划内下游：向 Mock admin 取桩，任何失败都抛异常，绝不真实调用；</li>
 *   <li>未配置运行时（classpath 形态未接 agent）：失败关闭。</li>
 * </ul>
 */
public final class DubboBridge {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static volatile AgentRuntime runtime;

    private DubboBridge() {
    }

    static void configure(AgentRuntime value) {
        runtime = value;
    }

    static void filterInstantiated(Class<?> filterClass) {
        AgentRuntime current = runtime;
        if (current == null) {
            return;
        }
        current.dubboFilterInstantiated(version(filterClass.getClassLoader()));
    }

    /**
     * @return 命中桩时返回 Dubbo {@code Result} 对象；非计划内下游返回 {@code null}
     * @throws IllegalStateException 计划内下游取桩失败；消息为失败子码
     */
    static Object intercept(Object invoker, Object invocation, Class<?> filterClass) {
        AgentRuntime current = runtime;
        if (current == null) {
            throw new IllegalStateException("agent_not_active");
        }
        current.dubboFilterHit();

        CallInfo info = describe(invoker, invocation);
        if (info == null) {
            throw new IllegalStateException("mock_identity_mismatch");
        }
        // 路由快照已就绪且该 dep 不在计划内：放行真实调用，不产生 Mock 证据。
        if (current.dubboRoutesReady() && !current.isDubboPlanned(info.depId)) {
            current.dubboPassthrough();
            return null;
        }

        StubReply reply = requestStub(current, info);
        if (reply.passthrough) {
            current.dubboPassthrough();
            return null;
        }
        Object value = toValue(reply.body, info, filterClass.getClassLoader());
        return resultObject(filterClass.getClassLoader(), invocation, value);
    }

    // --------------------------------------------------------------- 调用描述

    static final class CallInfo {
        String depId;
        String service;
        String method;
        Class<?>[] parameterTypes;
        Object[] arguments;
        Type returnType;
        boolean generic;
    }

    static String depId(String service, String method, String group, String version) {
        StringBuilder value = new StringBuilder("dubbo:")
                .append(service == null ? "" : service)
                .append('#')
                .append(method == null ? "" : method);
        boolean hasGroup = group != null && !group.isEmpty();
        boolean hasVersion = version != null && !version.isEmpty();
        if (hasGroup || hasVersion) {
            value.append('?');
            if (hasGroup) {
                value.append("group=").append(group);
                if (hasVersion) {
                    value.append('&');
                }
            }
            if (hasVersion) {
                value.append("version=").append(version);
            }
        }
        return value.toString();
    }

    private static CallInfo describe(Object invoker, Object invocation) {
        try {
            Class<?> iface = (Class<?>) invoke(invoker, "getInterface", new Class<?>[0]);
            Object url = invoke(invoker, "getUrl", new Class<?>[0]);
            String group = url == null ? null : (String) invoke(
                    url, "getParameter", new Class<?>[]{String.class}, "group");
            String version = url == null ? null : (String) invoke(
                    url, "getParameter", new Class<?>[]{String.class}, "version");
            String methodName = (String) invoke(
                    invocation, "getMethodName", new Class<?>[0]);
            Class<?>[] parameterTypes = (Class<?>[]) invoke(
                    invocation, "getParameterTypes", new Class<?>[0]);
            Object[] arguments = (Object[]) invoke(
                    invocation, "getArguments", new Class<?>[0]);
            if (iface == null || methodName == null) {
                System.err.println("[astsight-agent] dubbo describe missing iface=" + iface + " method=" + methodName);
                return null;
            }

            CallInfo info = new CallInfo();
            info.parameterTypes = parameterTypes == null ? new Class<?>[0] : parameterTypes;
            info.arguments = arguments == null ? new Object[0] : arguments;
            info.service = iface.getName();
            info.method = methodName;
            info.generic = "org.apache.dubbo.rpc.service.GenericService".equals(info.service)
                    || "$invoke".equals(methodName)
                    || "$invokeAsync".equals(methodName);
            if (info.generic) {
                // 泛化调用把真实接口/方法名放在参数和 attachment 里。
                Object serviceArg = attachment(invocation, "interface");
                Object methodArg = null;
                if (info.arguments.length > 0 && info.arguments[0] instanceof String) {
                    serviceArg = info.arguments[0];
                }
                if (info.arguments.length > 1 && info.arguments[1] instanceof String) {
                    methodArg = info.arguments[1];
                }
                if (serviceArg instanceof String) {
                    info.service = (String) serviceArg;
                }
                if (methodArg instanceof String) {
                    info.method = (String) methodArg;
                }
                info.returnType = Object.class;
            } else {
                Method target = findMethod(iface, methodName, info.parameterTypes);
                if (target == null) {
                    System.err.println("[astsight-agent] dubbo method not found: " + iface + "#" + methodName
                            + " params=" + java.util.Arrays.toString(info.parameterTypes));
                    return null;
                }
                info.returnType = target.getGenericReturnType();
            }
            info.depId = depId(info.service, info.method, group, version);
            return info;
        } catch (Throwable failure) {
            System.err.println("[astsight-agent] dubbo describe failed: " + failure);
            return null;
        }
    }

    private static Method findMethod(Class<?> iface, String name, Class<?>[] parameterTypes) {
        for (Method candidate : iface.getMethods()) {
            if (candidate.getName().equals(name)
                    && Arrays.equals(candidate.getParameterTypes(), parameterTypes)) {
                return candidate;
            }
        }
        return null;
    }

    private static Object attachment(Object invocation, String key) {
        try {
            Object attachments = invoke(invocation, "getAttachments", new Class<?>[0]);
            if (attachments instanceof java.util.Map) {
                return ((java.util.Map<?, ?>) attachments).get(key);
            }
        } catch (Throwable ignored) {
            // 旧版本没有 attachments 时按未设置处理。
        }
        return null;
    }

    private static boolean isTrue(Object value) {
        return value != null && "true".equalsIgnoreCase(String.valueOf(value));
    }

    // ------------------------------------------------------------- admin 取桩

    private static final class StubReply {
        boolean passthrough;
        JsonNode body;
    }

    private static StubReply requestStub(AgentRuntime current, CallInfo info) {
        StubReply reply = new StubReply();
        byte[] payload = payload(current, info);
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(
                    current.adminUrl() + "/_admin/dubbo/invoke").openConnection();
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(2000);
            // 桩的 delay_ms 最大 60s，读超时要覆盖它。
            connection.setReadTimeout(65000);
            connection.setRequestProperty("Authorization", "Bearer " + current.token());
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Content-Type", "application/json");
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(payload.length);
            OutputStream out = connection.getOutputStream();
            try {
                out.write(payload);
            } finally {
                out.close();
            }
            int status = connection.getResponseCode();
            String text = read(status >= 200 && status < 300
                    ? connection.getInputStream() : connection.getErrorStream());
            JsonNode root = text == null || text.isEmpty() ? null : MAPPER.readTree(text);
            String error = root == null ? "" : root.path("error").asText("");
            if (status == 200 && root != null) {
                reply.body = root.get("body");
                return reply;
            }
            if (status == 404 && "mock_dep_unplanned".equals(error)) {
                reply.passthrough = true;
                return reply;
            }
            String code = error.isEmpty() ? "mock_admin_error_" + status : error;
            current.dubboError(code + " " + info.depId);
            throw new IllegalStateException(code);
        } catch (IOException failure) {
            // Mock 不可达：计划内必须失败关闭；已确认计划外才放行。
            if (current.dubboRoutesReady() && !current.isDubboPlanned(info.depId)) {
                reply.passthrough = true;
                return reply;
            }
            current.dubboError("admin_unreachable " + info.depId + " " + failure);
            throw new IllegalStateException("agent_not_active");
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private static byte[] payload(AgentRuntime current, CallInfo info) {
        ObjectNode node = MAPPER.createObjectNode();
        node.put("sut_id", current.sutId());
        node.put("dep_id", info.depId);
        node.put("service", info.service);
        node.put("method", info.method);
        node.put("generic", info.generic);
        ArrayNode types = node.putArray("parameter_types");
        for (Class<?> type : info.parameterTypes) {
            types.add(type == null ? "" : type.getName());
        }
        return node.toString().getBytes(StandardCharsets.UTF_8);
    }

    private static String read(InputStream stream) throws IOException {
        if (stream == null) {
            return "";
        }
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[4096];
            int count;
            while ((count = stream.read(buffer)) != -1) {
                out.write(buffer, 0, count);
            }
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        } finally {
            stream.close();
        }
    }

    // -------------------------------------------------------------- 响应构造

    private static Object toValue(JsonNode body, CallInfo info, ClassLoader loader) {
        if (info.generic || info.returnType == null
                || info.returnType == Void.TYPE || info.returnType == Void.class) {
            return body == null || body.isNull() ? null : MAPPER.convertValue(body, Object.class);
        }
        try {
            return MAPPER.convertValue(
                    body, MAPPER.getTypeFactory().constructType(info.returnType));
        } catch (Throwable failure) {
            if (runtime != null) {
                runtime.dubboError("response invalid " + info.depId + " " + failure);
            }
            throw new IllegalStateException("mock_response_invalid");
        }
    }

    private static Object resultObject(
            ClassLoader loader, Object invocation, Object value) {
        try {
            Class<?> invocationType = Class.forName(
                    "org.apache.dubbo.rpc.Invocation", false, loader);
            Class<?> resultType = Class.forName(
                    "org.apache.dubbo.rpc.AsyncRpcResult", false, loader);
            try {
                Method factory = resultType.getMethod(
                        "newDefaultAsyncResult", Object.class, invocationType);
                return factory.invoke(null, value, invocation);
            } catch (NoSuchMethodException missing) {
                Constructor<?> constructor = resultType.getConstructor(
                        CompletableFuture.class, invocationType);
                return constructor.newInstance(
                        CompletableFuture.completedFuture(value), invocation);
            }
        } catch (Throwable failure) {
            throw new IllegalStateException("mock_response_invalid");
        }
    }

    private static String version(ClassLoader loader) {
        try {
            Class<?> type = Class.forName("org.apache.dubbo.common.Version", false, loader);
            Object value = type.getMethod("getVersion").invoke(null);
            return value == null ? null : String.valueOf(value);
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static Object invoke(
            Object target, String name, Class<?>[] parameterTypes, Object... arguments)
            throws Exception {
        List<Class<?>> types = new ArrayList<Class<?>>();
        for (Class<?> type : parameterTypes) {
            types.add(type);
        }
        Method method = target.getClass().getMethod(name, types.toArray(new Class<?>[0]));
        // 被测代码常用非 public 的匿名类实现 Invoker/Invocation；必须放开
        // 声明类访问检查，否则反射调用会抛 IllegalAccessException。
        if (!method.isAccessible()) {
            method.setAccessible(true);
        }
        return method.invoke(target, arguments);
    }
}
