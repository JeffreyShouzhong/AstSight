package io.astsight.agent.http;

import io.astsight.agent.core.AgentRewriteException;
import io.astsight.agent.core.AgentRuntime;
import io.astsight.agent.core.AstSightAgent;
import io.astsight.agent.core.RewriteResult;

import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.net.URI;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;

/**
 * Apache HttpClient 4/5 classic 请求改写。
 *
 * <p>织入点是两代 {@code InternalHttpClient#doExecute}。bridge 返回
 * {@code {target, request}}：改写成功时把 target 换成 Mock 端点（doExecute
 * 会把 target 直接交给 RoutePlanner，置空会报 “Target host is not specified”），
 * 同时把 request URI 换成 Mock URL；未改写时原样返回。请求对象与 target 全程
 * 反射操作，不依赖应用侧 HttpClient 类型。</p>
 *
 * <p>Feign / RestTemplate 走 Apache 传输时会在上层先被改写为 Mock 地址；本
 * bridge 看到 {@code /_dep/} 或 Mock endpoint 会直接放行，因此可以安全地同时
 * 安装。</p>
 */
public final class ApacheHttpClientBridge {

    private static final ConcurrentMap<Class<?>, Api> CACHE =
            new ConcurrentHashMap<Class<?>, Api>();

    private ApacheHttpClientBridge() {
    }

    public static Object[] rewriteOrOriginal(Object target, Object request) {
        Object[] original = new Object[]{target, request};
        AgentRuntime runtime = AstSightAgent.runtime();
        if (runtime == null || request == null) {
            return original;
        }
        String adapter = adapter(request.getClass());
        String method = null;
        String rawUrl = null;
        boolean required = false;
        try {
            Api api = api(request.getClass());
            method = api.method(request);
            rawUrl = absolute(target, api.uri(request));
            if (rawUrl == null) {
                return original;
            }
            RewriteResult result = runtime.rewrite(adapter, method, rawUrl);
            if (!result.isRewritten()) {
                if (runtime.isPlanned(method, rawUrl)) {
                    throw fail(
                            runtime,
                            adapter,
                            adapter + " planned request was not rewritten");
                }
                return original;
            }
            required = true;

            URI rewritten = URI.create(result.url());
            api.setUri(request, rewritten);
            api.addHeader(request, "X-AstSight-Adapter", adapter);
            api.addHeader(request, "X-AstSight-Mode", "agent_rewrite");
            api.addHeader(
                    request, "X-AstSight-Original-Host", result.originalHost());
            api.addHeader(
                    request, "X-AstSight-Original-Path", result.originalPath());
            api.addHeader(
                    request, "X-AstSight-Original-Query", result.originalQuery());
            // doExecute 会把 target 参数直接交给 RoutePlanner，不能置空；
            // 必须换成 Mock 端点，否则会报 "Target host is not specified"。
            return new Object[]{
                    mockTarget(rewritten, api.hc5, request.getClass().getClassLoader()),
                    request};
        } catch (AgentRewriteException failure) {
            throw failure;
        } catch (Throwable failure) {
            runtime.recordError(adapter, failure);
            if (required
                    || (method != null
                    && rawUrl != null
                    && runtime.isPlanned(method, rawUrl))) {
                throw new AgentRewriteException(
                        adapter + " rewrite failed", failure);
            }
            return original;
        }
    }

    private static String adapter(Class<?> requestType) {
        return requestType.getName().startsWith("org.apache.hc.")
                ? "httpclient5"
                : "httpclient4";
    }

    /** 用应用侧 HttpHost 类型构造 Mock 端点，避免 target 为 null。 */
    private static Object mockTarget(
            URI uri, boolean hc5, ClassLoader loader) throws Exception {
        String host = uri.getHost();
        if (host == null || host.isEmpty()) {
            throw new IllegalStateException(
                    "rewritten URL has no host: " + uri);
        }
        Class<?> type = Class.forName(
                hc5 ? "org.apache.hc.core5.http.HttpHost"
                        : "org.apache.http.HttpHost",
                false,
                loader);
        if (hc5) {
            // HC5 的构造参数顺序与 HC4 不同，直接用 URI 工厂更稳。
            Method create = type.getMethod("create", URI.class);
            return create.invoke(null, uri);
        }
        Constructor<?> constructor =
                type.getConstructor(String.class, int.class, String.class);
        return constructor.newInstance(
                host, uri.getPort(), uri.getScheme());
    }

    /**
     * 把 request 中的 URI 还原成绝对地址。显式 target 优先（与 HttpClient 的
     * {@code determineTarget} 语义一致），request URI 只提供 path/query。
     */
    private static String absolute(Object target, String uriText) {
        if (uriText == null) {
            return null;
        }
        String uri = uriText.trim();
        if (uri.isEmpty()) {
            uri = "/";
        }
        if ("*".equals(uri)) {
            return null;
        }
        String path = uri;
        String query = "";
        boolean absoluteUri = false;
        try {
            URI parsed = new URI(uri);
            absoluteUri = parsed.isAbsolute();
            String rawPath = parsed.getRawPath();
            if (rawPath == null || rawPath.isEmpty()) {
                rawPath = "/";
            } else if (!rawPath.startsWith("/")) {
                rawPath = "/" + rawPath;
            }
            path = rawPath;
            if (parsed.getRawQuery() != null) {
                query = "?" + parsed.getRawQuery();
            }
        } catch (Exception ignored) {
            // 保留原始字符串，交给后面的 URI 解析决定是否可改写。
        }
        if (target == null) {
            return absoluteUri ? uri : null;
        }
        String scheme;
        String host;
        int port;
        try {
            Class<?> targetType = target.getClass();
            scheme = String.valueOf(
                    targetType.getMethod("getSchemeName").invoke(target));
            host = String.valueOf(
                    targetType.getMethod("getHostName").invoke(target));
            port = ((Number) targetType.getMethod("getPort").invoke(target))
                    .intValue();
        } catch (Throwable unreadable) {
            return absoluteUri ? uri : null;
        }
        if (scheme == null || scheme.isEmpty() || "null".equals(scheme)
                || host == null || host.isEmpty() || "null".equals(host)) {
            return absoluteUri ? uri : null;
        }
        String authority = host;
        if (host.indexOf(':') >= 0 && !host.startsWith("[")) {
            authority = "[" + host + "]";
        }
        if (port > 0) {
            authority += ":" + port;
        }
        return scheme + "://" + authority + path + query;
    }

    private static AgentRewriteException fail(
            AgentRuntime runtime, String adapter, String message) {
        IllegalStateException failure = new IllegalStateException(message);
        runtime.recordError(adapter, failure);
        return new AgentRewriteException(message, failure);
    }

    private static Api api(Class<?> requestType) throws Exception {
        Api cached = CACHE.get(requestType);
        if (cached != null) {
            return cached;
        }
        Api created = Api.of(requestType);
        CACHE.put(requestType, created);
        return created;
    }

    /** 一个 HttpClient 版本的反射视图；首次请求时构建并缓存。 */
    private static final class Api {
        private final boolean hc5;
        private final Method requestLine;
        private final Method method;
        private final Method uri;
        private final Method setUri;
        private final Method addHeader;

        private Api(
                boolean hc5,
                Method requestLine,
                Method method,
                Method uri,
                Method setUri,
                Method addHeader) {
            this.hc5 = hc5;
            this.requestLine = requestLine;
            this.method = method;
            this.uri = uri;
            this.setUri = setUri;
            this.addHeader = addHeader;
        }

        static Api of(Class<?> type) throws Exception {
            ClassLoader loader = type.getClassLoader();
            String name = type.getName();
            if (name.startsWith("org.apache.hc.")) {
                Class<?> request =
                        Class.forName("org.apache.hc.core5.http.HttpRequest", false, loader);
                Class<?> message =
                        Class.forName("org.apache.hc.core5.http.HttpMessage", false, loader);
                return new Api(
                        true,
                        null,
                        type.getMethod("getMethod"),
                        type.getMethod("getUri"),
                        optional(request, "setUri", URI.class),
                        optional(message, "addHeader", String.class, Object.class));
            }
            Class<?> line =
                    Class.forName("org.apache.http.RequestLine", false, loader);
            Class<?> message =
                    Class.forName("org.apache.http.HttpMessage", false, loader);
            Method requestLine = type.getMethod("getRequestLine");
            return new Api(
                    false,
                    requestLine,
                    line.getMethod("getMethod"),
                    line.getMethod("getUri"),
                    optional(type, "setURI", URI.class),
                    optional(message, "addHeader", String.class, String.class));
        }

        private static Method optional(
                Class<?> type, String name, Class<?>... parameterTypes) {
            try {
                return type.getMethod(name, parameterTypes);
            } catch (NoSuchMethodException absent) {
                return null;
            }
        }

        String method(Object request) throws Exception {
            Object value = hc5
                    ? method.invoke(request)
                    : method.invoke(requestLine.invoke(request));
            return value == null ? null : String.valueOf(value);
        }

        String uri(Object request) throws Exception {
            Object value = hc5
                    ? uri.invoke(request)
                    : uri.invoke(requestLine.invoke(request));
            if (value instanceof URI) {
                return value.toString();
            }
            return value == null ? null : String.valueOf(value);
        }

        void setUri(Object request, URI value) throws Exception {
            if (setUri == null) {
                throw new IllegalStateException(
                        "request type does not expose a URI setter: "
                                + request.getClass().getName());
            }
            setUri.invoke(request, value);
        }

        void addHeader(Object request, String name, String value) throws Exception {
            if (addHeader != null) {
                addHeader.invoke(request, name, value == null ? "" : value);
            }
        }
    }
}
