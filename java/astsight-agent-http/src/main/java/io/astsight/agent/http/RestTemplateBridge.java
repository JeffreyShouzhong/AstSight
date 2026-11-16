package io.astsight.agent.http;

import io.astsight.agent.core.AgentRewriteException;
import io.astsight.agent.core.AgentRuntime;
import io.astsight.agent.core.AstSightAgent;
import io.astsight.agent.core.RewriteResult;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.net.URI;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;

/**
 * RestTemplate 出站改写：替换 {@code doExecute} 的 URI，并用动态代理包一层
 * {@code RequestCallback} 写入证据头。
 *
 * <p>bridge 不直接引用 Spring 类型（agent 类加载器通常看不到应用依赖），
 * 代理接口通过应用侧 {@code RestTemplate} 的类加载器反射加载。</p>
 */
public final class RestTemplateBridge {

    private static final String CALLBACK = "org.springframework.web.client.RequestCallback";
    private static final ConcurrentMap<Class<?>, Method[]> HEADER_CACHE =
            new ConcurrentHashMap<Class<?>, Method[]>();

    private RestTemplateBridge() {
    }

    /**
     * @return {@code [URI, RequestCallback]}，由织入代码写回 doExecute 的局部变量。
     */
    public static Object[] rewriteCall(
            Object restTemplate,
            Object uri,
            Object httpMethod,
            Object callback) {
        Object[] unchanged = new Object[]{uri, callback};
        AgentRuntime runtime = AstSightAgent.runtime();
        if (runtime == null || uri == null) {
            return unchanged;
        }
        String method = httpMethod == null ? "" : String.valueOf(httpMethod);
        String rawUrl = String.valueOf(uri);
        try {
            RewriteResult result = runtime.rewrite("resttemplate", method, rawUrl);
            if (!result.isRewritten()) {
                if (runtime.isPlanned(method, rawUrl)) {
                    throw fail(runtime, "resttemplate planned request was not rewritten");
                }
                return unchanged;
            }
            Object newUri = URI.create(result.url());
            Object wrapped = wrapCallback(restTemplate, callback, result);
            return new Object[]{newUri, wrapped};
        } catch (AgentRewriteException failure) {
            throw failure;
        } catch (Throwable failure) {
            runtime.recordError("resttemplate", failure);
            if (runtime.isPlanned(method, rawUrl)) {
                throw new AgentRewriteException(
                        "resttemplate rewrite failed", failure);
            }
            return unchanged;
        }
    }

    private static Object wrapCallback(
            Object restTemplate, Object callback, RewriteResult result)
            throws Exception {
        ClassLoader loader = restTemplate == null
                ? null
                : restTemplate.getClass().getClassLoader();
        if (loader == null) {
            loader = Thread.currentThread().getContextClassLoader();
        }
        if (loader == null) {
            loader = ClassLoader.getSystemClassLoader();
        }
        Class<?> callbackType = Class.forName(CALLBACK, false, loader);
        return Proxy.newProxyInstance(
                loader,
                new Class<?>[]{callbackType},
                new EvidenceCallback(callback, result));
    }

    private static void applyEvidence(Object request, RewriteResult result)
            throws Exception {
        Method[] methods = HEADER_CACHE.get(request.getClass());
        if (methods == null) {
            Method headers = request.getClass().getMethod("getHeaders");
            Object value = headers.invoke(request);
            Method set = value.getClass().getMethod(
                    "set", String.class, String.class);
            methods = new Method[]{headers, set};
            HEADER_CACHE.put(request.getClass(), methods);
        }
        Object headers = methods[0].invoke(request);
        set(methods[1], headers, "X-AstSight-Adapter", "resttemplate");
        set(methods[1], headers, "X-AstSight-Mode", "agent_rewrite");
        set(methods[1], headers, "X-AstSight-Original-Host", result.originalHost());
        set(methods[1], headers, "X-AstSight-Original-Path", result.originalPath());
        set(methods[1], headers, "X-AstSight-Original-Query", result.originalQuery());
    }

    private static void set(
            Method setter, Object headers, String name, String value)
            throws Exception {
        setter.invoke(headers, name, value == null ? "" : value);
    }

    private static AgentRewriteException fail(
            AgentRuntime runtime, String message) {
        IllegalStateException failure = new IllegalStateException(message);
        runtime.recordError("resttemplate", failure);
        return new AgentRewriteException(message, failure);
    }

    private static final class EvidenceCallback implements InvocationHandler {
        private final Object delegate;
        private final RewriteResult result;

        private EvidenceCallback(Object delegate, RewriteResult result) {
            this.delegate = delegate;
            this.result = result;
        }

        @Override
        public Object invoke(Object proxy, Method method, Object[] args)
                throws Throwable {
            if ("doWithRequest".equals(method.getName())
                    && args != null
                    && args.length == 1
                    && args[0] != null) {
                applyEvidence(args[0], result);
                return delegate == null ? null : invokeDelegate(method, args);
            }
            if (delegate != null) {
                return invokeDelegate(method, args);
            }
            String name = method.getName();
            if ("toString".equals(name)) {
                return "AstSightRequestCallback";
            }
            if ("hashCode".equals(name)) {
                return System.identityHashCode(proxy);
            }
            if ("equals".equals(name) && args != null && args.length == 1) {
                return proxy == args[0];
            }
            return null;
        }

        private Object invokeDelegate(Method method, Object[] args)
                throws Throwable {
            try {
                return method.invoke(delegate, args);
            } catch (InvocationTargetException wrapped) {
                throw wrapped.getCause();
            }
        }
    }
}
