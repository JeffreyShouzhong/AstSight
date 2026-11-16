package io.astsight.agent.http;

import io.astsight.agent.core.AgentRewriteException;
import io.astsight.agent.core.AgentRuntime;
import io.astsight.agent.core.AstSightAgent;
import io.astsight.agent.core.RewriteResult;

import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.nio.charset.Charset;
import java.util.Collection;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;

/**
 * Feign 请求通过反射重建，避免 agent 类加载器解析应用侧的 {@code feign.Request}。
 *
 * <p>入口是 {@code SynchronousMethodHandler#targetRequest}，此时 URL 已完成
 * target 拼接与 interceptor 处理。重建时按 Feign 10/11/12/13 的
 * {@code Request.create} 重载动态选择签名：能拿到 byte[] 就用 byte[] 版本，
 * 否则沿用原 {@code Request.Body} 对象。</p>
 */
public final class FeignBridge {

    private static final ConcurrentMap<Class<?>, Api> CACHE =
            new ConcurrentHashMap<Class<?>, Api>();

    private FeignBridge() {
    }

    public static Object rewriteOrOriginal(Object request) {
        AgentRuntime runtime = AstSightAgent.runtime();
        if (runtime == null || request == null) {
            return request;
        }
        String method = null;
        String rawUrl = null;
        boolean required = false;
        try {
            Api api = api(request.getClass());
            method = api.method(request);
            rawUrl = api.url(request);
            RewriteResult result = runtime.rewrite("feign", method, rawUrl);
            if (!result.isRewritten()) {
                if (runtime.isPlanned(method, rawUrl)) {
                    throw fail(runtime, "feign planned request was not rewritten");
                }
                return request;
            }
            required = true;

            Map<String, Collection<String>> headers =
                    new LinkedHashMap<String, Collection<String>>(
                            api.headers(request));
            put(headers, "X-AstSight-Adapter", "feign");
            put(headers, "X-AstSight-Mode", "agent_rewrite");
            put(headers, "X-AstSight-Original-Host", result.originalHost());
            put(headers, "X-AstSight-Original-Path", result.originalPath());
            put(headers, "X-AstSight-Original-Query", result.originalQuery());
            Object rewritten = api.create(
                    method,
                    result.url(),
                    headers,
                    api.body(request),
                    api.charset(request),
                    api.template(request));
            if (rewritten == null) {
                throw new IllegalStateException("feign Request.create unavailable");
            }
            return rewritten;
        } catch (AgentRewriteException failure) {
            throw failure;
        } catch (Throwable failure) {
            runtime.recordError("feign", failure);
            if (required
                    || (method != null
                    && rawUrl != null
                    && runtime.isPlanned(method, rawUrl))) {
                throw new AgentRewriteException("feign rewrite failed", failure);
            }
            return request;
        }
    }

    private static void put(
            Map<String, Collection<String>> headers, String name, String value) {
        headers.put(name, Collections.singletonList(value == null ? "" : value));
    }

    private static AgentRewriteException fail(
            AgentRuntime runtime, String message) {
        IllegalStateException failure = new IllegalStateException(message);
        runtime.recordError("feign", failure);
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

    /** 一个 Feign 版本的反射视图；在首次请求时构建并缓存。 */
    private static final class Api {
        private final Method url;
        private final Method method;
        private final Method headers;
        private final Method body;
        private final Method charset;
        private final Method template;
        private final Method create;

        private Api(
                Method url,
                Method method,
                Method headers,
                Method body,
                Method charset,
                Method template,
                Method create) {
            this.url = url;
            this.method = method;
            this.headers = headers;
            this.body = body;
            this.charset = charset;
            this.template = template;
            this.create = create;
        }

        static Api of(Class<?> type) throws Exception {
            Method url = type.getMethod("url");
            Method headers = type.getMethod("headers");
            Method method = stringMethod(type, "method");
            if (method == null) {
                method = type.getMethod("httpMethod");
            }
            Method body = optional(type, "body");
            Method charset = optional(type, "charset");
            Method template = optional(type, "requestTemplate");

            Method create = null;
            Method fallback = null;
            for (Method candidate : type.getMethods()) {
                if (!"create".equals(candidate.getName())
                        || !Modifier.isStatic(candidate.getModifiers())) {
                    continue;
                }
                Class<?>[] params = candidate.getParameterTypes();
                if (params.length < 4 || params.length > 6) {
                    continue;
                }
                if (!acceptsMethod(params[0]) || params[1] != String.class) {
                    continue;
                }
                if (!Map.class.isAssignableFrom(params[2])) {
                    continue;
                }
                boolean candidateBytes = params[3] == byte[].class;
                if (candidateBytes && body != null
                        && body.getReturnType() == byte[].class) {
                    create = candidate;
                    break;
                }
                if (!candidateBytes && fallback == null) {
                    fallback = candidate;
                }
            }
            if (create == null) {
                create = fallback;
            }
            if (create == null) {
                throw new IllegalStateException(
                        "feign Request.create(String|HttpMethod, String, Map, ...) not found");
            }
            return new Api(
                    url, method, headers, body, charset, template, create);
        }

        private static boolean acceptsMethod(Class<?> type) {
            return type == String.class || type.isEnum();
        }

        private static Method stringMethod(Class<?> type, String name)
                throws Exception {
            Method candidate = optional(type, name);
            return candidate != null && candidate.getReturnType() == String.class
                    ? candidate
                    : null;
        }

        private static Method optional(Class<?> type, String name) {
            try {
                return type.getMethod(name);
            } catch (NoSuchMethodException absent) {
                return null;
            }
        }

        String url(Object request) throws Exception {
            return String.valueOf(url.invoke(request));
        }

        String method(Object request) throws Exception {
            Object value = method.invoke(request);
            return value instanceof String ? (String) value : String.valueOf(value);
        }

        Map<String, Collection<String>> headers(Object request) throws Exception {
            @SuppressWarnings("unchecked")
            Map<String, Collection<String>> value =
                    (Map<String, Collection<String>>) headers.invoke(request);
            return value == null
                    ? new LinkedHashMap<String, Collection<String>>()
                    : value;
        }

        Object body(Object request) throws Exception {
            return body == null ? null : body.invoke(request);
        }

        Charset charset(Object request) {
            if (charset != null) {
                try {
                    Object value = charset.invoke(request);
                    if (value instanceof Charset) {
                        return (Charset) value;
                    }
                } catch (Throwable ignored) {
                    // 某些版本 charset() 只在内部使用，取不到时退回 UTF-8。
                }
            }
            return Charset.forName("UTF-8");
        }

        Object template(Object request) {
            if (template == null) {
                return null;
            }
            try {
                return template.invoke(request);
            } catch (Throwable ignored) {
                return null;
            }
        }

        Object create(
                String method,
                String url,
                Map<String, Collection<String>> headers,
                Object body,
                Charset charset,
                Object template) throws Exception {
            Class<?>[] params = create.getParameterTypes();
            Object[] args = new Object[params.length];
            for (int i = 0; i < params.length; i++) {
                Class<?> type = params[i];
                if (i == 0) {
                    args[i] = methodValue(type, method);
                } else if (i == 1) {
                    args[i] = url;
                } else if (i == 2) {
                    args[i] = headers;
                } else if (i == 3) {
                    args[i] = body;
                } else if (type == Charset.class) {
                    args[i] = charset;
                } else {
                    args[i] = template;
                }
            }
            return create.invoke(null, args);
        }

        @SuppressWarnings({"unchecked", "rawtypes"})
        private static Object methodValue(Class<?> type, String method) {
            if (type == String.class) {
                return method;
            }
            return Enum.valueOf((Class<? extends Enum>) type, method.toUpperCase());
        }
    }
}
