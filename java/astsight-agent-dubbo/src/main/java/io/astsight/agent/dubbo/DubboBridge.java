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
import java.lang.reflect.ParameterizedType;
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
        ClassLoader loader = filterClass.getClassLoader();
        Object value = toValue(reply.body, info, loader);
        // oneway 没有返回值；桩仍被消费并留证据。
        if (info.oneway) {
            value = null;
        }
        Object result = resultObject(loader, invocation, value, info.oneway);
        if (info.async || info.future) {
            applyAsyncContext(loader, invocation, value, info.future);
        }
        return result;
    }

    // --------------------------------------------------------------- 调用描述

    static final class CallInfo {
        String depId;
        String service;
        String method;
        Class<?>[] parameterTypes;
        /** `dep_id` 里的参数签名：泛化调用取真实接口的参数类型名。 */
        String[] parameterSignature;
        Object[] arguments;
        /** 已展开 CompletableFuture 的返回类型；泛化调用为 Object。 */
        Type returnType;
        boolean generic;
        boolean async;
        boolean future;
        boolean oneway;
    }

    static String depId(
            String service,
            String method,
            String[] parameterTypes,
            String group,
            String version) {
        StringBuilder value = new StringBuilder("dubbo:")
                .append(service == null ? "" : service)
                .append('#')
                .append(method == null ? "" : method)
                .append('(');
        if (parameterTypes != null) {
            for (int index = 0; index < parameterTypes.length; index++) {
                if (index > 0) {
                    value.append(',');
                }
                value.append(parameterTypes[index] == null ? "" : parameterTypes[index]);
            }
        }
        value.append(')');
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

    /** 参数签名用于区分重载；泛化调用的真实签名在 {@code parameterTypes} 参数里。 */
    private static String[] parameterNames(Class<?>[] types) {
        if (types == null) {
            return new String[0];
        }
        String[] names = new String[types.length];
        for (int index = 0; index < types.length; index++) {
            names[index] = types[index] == null ? "" : types[index].getName();
        }
        return names;
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
            info.parameterSignature = parameterNames(info.parameterTypes);
            info.arguments = arguments == null ? new Object[0] : arguments;
            info.service = iface.getName();
            info.method = methodName;
            info.generic = "org.apache.dubbo.rpc.service.GenericService".equals(info.service)
                    || "com.alibaba.dubbo.rpc.service.GenericService".equals(info.service)
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
                // 泛化调用第 3 个参数是真实参数类型名数组。
                if (info.arguments.length > 2 && info.arguments[2] instanceof String[]) {
                    info.parameterSignature = (String[]) info.arguments[2];
                } else {
                    info.parameterSignature = new String[0];
                }
                info.returnType = Object.class;
                // $invokeAsync 声明即返回 CompletableFuture，不依赖 attachment。
                info.future = "$invokeAsync".equals(methodName);
            } else {
                Method target = findMethod(iface, methodName, info.parameterTypes);
                if (target == null) {
                    System.err.println("[astsight-agent] dubbo method not found: " + iface + "#" + methodName
                            + " params=" + java.util.Arrays.toString(info.parameterTypes));
                    return null;
                }
                Type declared = target.getGenericReturnType();
                Type inner = unwrapFuture(declared);
                info.future = inner != declared;
                info.returnType = inner;
            }
            ClassLoader loader = invocation.getClass().getClassLoader();
            detectInvokeMode(info, invocation, url, loader);
            info.oneway = isOneway(invocation, url, info.method, loader);
            info.depId = depId(
                    info.service, info.method, info.parameterSignature, group, version);
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

    /** 声明返回 CompletableFuture&lt;T&gt; 时，桩体按 T 构造，值交给 future。 */
    private static Type unwrapFuture(Type declared) {
        if (declared instanceof ParameterizedType) {
            ParameterizedType parameterized = (ParameterizedType) declared;
            if (parameterized.getRawType() == CompletableFuture.class) {
                Type[] arguments = parameterized.getActualTypeArguments();
                return arguments.length == 1 ? arguments[0] : Object.class;
            }
        } else if (declared == CompletableFuture.class) {
            return Object.class;
        }
        return declared;
    }

    /**
     * 调用模式。Filter 在 {@code AbstractInvoker} 之前执行，所以不能只读
     * {@code RpcInvocation#getInvokeMode}（那时通常还是 null）；优先用
     * Dubbo 自己的 {@code RpcUtils#getInvokeMode(URL, Invocation)}，
     * 它同时看 attachment 和声明返回类型。
     */
    private static void detectInvokeMode(
            CallInfo info, Object invocation, Object url, ClassLoader loader) {
        String mode = enumName(rpcUtils(loader, url, invocation, "getInvokeMode"));
        if ("FUTURE".equals(mode)) {
            info.future = true;
        } else if ("ASYNC".equals(mode)) {
            info.async = true;
        } else if (mode == null) {
            // RpcUtils 不可用时的最低回退：只看 attachment。
            String async = attachmentString(invocation, info.method + ".async");
            if (async == null) {
                async = attachmentString(invocation, "async");
            }
            if (async != null) {
                info.async = Boolean.parseBoolean(async);
            }
        }
    }

    private static boolean isOneway(
            Object invocation, Object url, String method, ClassLoader loader) {
        Object flag = rpcUtils(loader, url, invocation, "isOneway");
        if (flag instanceof Boolean) {
            return (Boolean) flag;
        }
        String value = attachmentString(invocation, method + ".return");
        if (value == null) {
            value = attachmentString(invocation, "return");
        }
        return value != null && !Boolean.parseBoolean(value);
    }

    private static Object rpcUtils(
            ClassLoader loader, Object url, Object invocation, String name) {
        try {
            Class<?> utils = Class.forName(
                    "org.apache.dubbo.rpc.support.RpcUtils", false, loader);
            Class<?> urlType = Class.forName(
                    "org.apache.dubbo.common.URL", false, loader);
            Class<?> invocationType = Class.forName(
                    "org.apache.dubbo.rpc.Invocation", false, loader);
            Method method = utils.getMethod(name, urlType, invocationType);
            return method.invoke(null, url, invocation);
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static String attachmentString(Object invocation, String key) {
        Object value = attachment(invocation, key);
        return value == null ? null : String.valueOf(value);
    }

    private static String enumName(Object value) {
        return value instanceof Enum ? ((Enum<?>) value).name() : null;
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
            ClassLoader loader, Object invocation, Object value, boolean oneway) {
        try {
            Class<?> invocationType = Class.forName(
                    "org.apache.dubbo.rpc.Invocation", false, loader);
            Class<?> resultType = Class.forName(
                    "org.apache.dubbo.rpc.AsyncRpcResult", false, loader);
            if (oneway) {
                try {
                    Method empty = resultType.getMethod(
                            "newDefaultAsyncResult", invocationType);
                    return empty.invoke(null, invocation);
                } catch (NoSuchMethodException ignored) {
                    // 旧版本没有单参工厂，退回带 null 值的工厂。
                }
            }
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

    /**
     * 异步/声明 future 的调用：正常链路里由 {@code AbstractInvoker} 设置
     * {@code RpcContext} 的 future 和 invokeMode；Filter 短路后这一步不会发生，
     * 由这里补上，否则调用方拿不到结果或直接拿到 null。
     */
    private static void applyAsyncContext(
            ClassLoader loader, Object invocation, Object value, boolean futureMode) {
        try {
            CompletableFuture<Object> future = new CompletableFuture<Object>();
            future.complete(value);
            Class<?> contextType = Class.forName(
                    "org.apache.dubbo.rpc.RpcContext", false, loader);
            Object context = contextType.getMethod("getContext").invoke(null);
            contextType.getMethod("setFuture", CompletableFuture.class)
                    .invoke(context, future);
            if (futureMode) {
                Class<?> modeType = Class.forName(
                        "org.apache.dubbo.rpc.InvokeMode", false, loader);
                setInvokeMode(invocation, modeType, "FUTURE");
            }
        } catch (Throwable failure) {
            if (runtime != null) {
                runtime.dubboError("async context failed " + failure);
            }
            throw new IllegalStateException("mock_response_invalid");
        }
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static void setInvokeMode(Object invocation, Class<?> modeType, String name)
            throws Exception {
        Object mode = Enum.valueOf((Class) modeType, name);
        invocation.getClass().getMethod("setInvokeMode", modeType)
                .invoke(invocation, mode);
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
