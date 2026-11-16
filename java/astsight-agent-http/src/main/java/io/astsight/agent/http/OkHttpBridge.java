package io.astsight.agent.http;

import io.astsight.agent.core.AgentRewriteException;
import io.astsight.agent.core.AgentRuntime;
import io.astsight.agent.core.AstSightAgent;
import io.astsight.agent.core.RewriteResult;

import java.lang.reflect.Method;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;

/**
 * OkHttp 对象通过反射操作，避免 agent 类加载器在 Spring Boot fat jar 等场景下
 * 解析不到应用侧的 {@code okhttp3.Request}。核心与 adapter 都不依赖 OkHttp 类型。
 */
public final class OkHttpBridge {

    private static final MethodKey METHOD = new MethodKey("method");
    private static final MethodKey URL = new MethodKey("url");
    private static final MethodKey NEW_BUILDER = new MethodKey("newBuilder");
    private static final MethodKey BUILDER_URL = new MethodKey("url", String.class);
    private static final MethodKey BUILDER_HEADER =
            new MethodKey("header", String.class, String.class);
    private static final MethodKey BUILDER_BUILD = new MethodKey("build");
    private static final ConcurrentMap<Class<?>, Method[]> CACHE =
            new ConcurrentHashMap<Class<?>, Method[]>();

    private OkHttpBridge() {
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
            Class<?> requestType = request.getClass();
            Method[] requestMethods = CACHE.get(requestType);
            if (requestMethods == null) {
                requestMethods = new Method[]{
                        requestType.getMethod(METHOD.name),
                        requestType.getMethod(URL.name),
                        requestType.getMethod(NEW_BUILDER.name)
                };
                CACHE.put(requestType, requestMethods);
            }
            method = (String) requestMethods[0].invoke(request);
            Object httpUrl = requestMethods[1].invoke(request);
            rawUrl = String.valueOf(httpUrl);
            RewriteResult result = runtime.rewrite("okhttp", method, rawUrl);
            if (!result.isRewritten()) {
                if (runtime.isPlanned(method, rawUrl)) {
                    throw fail(runtime, "okhttp planned request was not rewritten");
                }
                return request;
            }
            required = true;

            Object builder = requestMethods[2].invoke(request);
            Class<?> builderType = builder.getClass();
            Method[] builderMethods = CACHE.get(builderType);
            if (builderMethods == null) {
                builderMethods = new Method[]{
                        builderType.getMethod(
                                BUILDER_URL.name, BUILDER_URL.parameterTypes),
                        builderType.getMethod(
                                BUILDER_HEADER.name, BUILDER_HEADER.parameterTypes),
                        builderType.getMethod(BUILDER_BUILD.name)
                };
                CACHE.put(builderType, builderMethods);
            }
            builderMethods[0].invoke(builder, result.url());
            builderMethods[1].invoke(
                    builder, "X-AstSight-Adapter", "okhttp");
            builderMethods[1].invoke(
                    builder, "X-AstSight-Mode", "agent_rewrite");
            builderMethods[1].invoke(
                    builder, "X-AstSight-Original-Host", result.originalHost());
            builderMethods[1].invoke(
                    builder, "X-AstSight-Original-Path", result.originalPath());
            builderMethods[1].invoke(
                    builder, "X-AstSight-Original-Query", result.originalQuery());
            return builderMethods[2].invoke(builder);
        } catch (AgentRewriteException failure) {
            throw failure;
        } catch (Throwable failure) {
            runtime.recordError("okhttp", failure);
            if (required
                    || (method != null
                    && rawUrl != null
                    && runtime.isPlanned(method, rawUrl))) {
                throw new AgentRewriteException(
                        "okhttp rewrite failed", failure);
            }
            return request;
        }
    }

    private static AgentRewriteException fail(
            AgentRuntime runtime, String message) {
        IllegalStateException failure = new IllegalStateException(message);
        runtime.recordError("okhttp", failure);
        return new AgentRewriteException(message, failure);
    }

    private static final class MethodKey {
        private final String name;
        private final Class<?>[] parameterTypes;

        private MethodKey(String name, Class<?>... parameterTypes) {
            this.name = name;
            this.parameterTypes = parameterTypes;
        }
    }
}
