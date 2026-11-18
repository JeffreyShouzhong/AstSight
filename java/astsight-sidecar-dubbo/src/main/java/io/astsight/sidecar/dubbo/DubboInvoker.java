package io.astsight.sidecar.dubbo;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.JsonNode;

import org.apache.dubbo.config.ApplicationConfig;
import org.apache.dubbo.config.ReferenceConfig;
import org.apache.dubbo.rpc.RpcContext;
import org.apache.dubbo.rpc.RpcException;
import org.apache.dubbo.rpc.service.GenericService;

import java.util.HashMap;
import java.util.Map;

/**
 * 泛化调用执行器。每条引用按
 * {@code interface + group + version + url + timeout} 缓存，调用结束后不销毁，
 * 由 {@link #close()} 统一回收。
 */
final class DubboInvoker implements DubboCaller, AutoCloseable {

    static final ObjectMapper MAPPER = new ObjectMapper();
    private static final int MAX_MESSAGE_CHARS = 2_000;

    private final ApplicationConfig application;
    private final Map<String, ReferenceConfig<GenericService>> references =
            new HashMap<String, ReferenceConfig<GenericService>>();

    DubboInvoker() {
        this("astsight-dubbo-sidecar");
    }

    DubboInvoker(String applicationName) {
        this(newApplication(applicationName));
    }

    DubboInvoker(ApplicationConfig application) {
        this.application = application;
        // sidecar 是短生命周期进程，不监听 QoS 端口，避免并发用例端口冲突。
        application.setQosEnable(Boolean.FALSE);
    }

    private static ApplicationConfig newApplication(String name) {
        return new ApplicationConfig(name);
    }

    @Override
    public InvokeOutcome invoke(InvokeRequest request) {
        long started = System.nanoTime();
        InvokeOutcome outcome;
        try {
            GenericService service = reference(request).get();
            RpcContext context = RpcContext.getContext();
            try {
                applyAttachments(context, request);
                Object value = service.$invoke(
                        request.method, request.paramTypes, convertArguments(request));
                outcome = InvokeOutcome.success(value);
            } finally {
                context.clearAttachments();
            }
        } catch (InvokeRequest.BadArgument failure) {
            outcome = InvokeOutcome.failure(
                    "bad_request", InvokeOutcome.BAD_REQUEST, message(failure));
        } catch (RpcException failure) {
            outcome = rpcFailure(failure);
        } catch (Throwable failure) {
            outcome = unexpectedFailure(failure);
        }
        outcome.elapsedMs = (System.nanoTime() - started) / 1_000_000L;
        return outcome;
    }

    private ReferenceConfig<GenericService> reference(InvokeRequest request) {
        String key = request.service
                + '\u0000' + safe(request.group)
                + '\u0000' + safe(request.version)
                + '\u0000' + request.url
                + '\u0000' + request.timeoutMs;
        synchronized (references) {
            ReferenceConfig<GenericService> existing = references.get(key);
            if (existing != null) {
                return existing;
            }
            ReferenceConfig<GenericService> reference = new ReferenceConfig<GenericService>();
            reference.setApplication(application);
            reference.setInterface(request.service);
            reference.setGeneric("true");
            reference.setProtocol(request.url.startsWith("tri://") ? "tri" : "dubbo");
            reference.setUrl(request.url);
            reference.setCheck(Boolean.FALSE);
            reference.setLazy(Boolean.TRUE);
            reference.setInit(Boolean.FALSE);
            // 测试必须可重放：禁止 Dubbo 默认的失败重试放大调用次数。
            reference.setRetries(Integer.valueOf(0));
            reference.setTimeout(Integer.valueOf(request.timeoutMs));
            if (request.group != null) {
                reference.setGroup(request.group);
            }
            if (request.version != null) {
                reference.setVersion(request.version);
            }
            references.put(key, reference);
            return reference;
        }
    }

    private static Object[] convertArguments(InvokeRequest request) {
        Object[] values = new Object[request.paramTypes.length];
        for (int index = 0; index < values.length; index++) {
            JsonNode node = request.arguments.get(index);
            values[index] = InvokeRequest.convertArgument(node, request.paramTypes[index]);
        }
        return values;
    }

    private static void applyAttachments(RpcContext context, InvokeRequest request) {
        for (Map.Entry<String, String> attachment : request.attachments.entrySet()) {
            context.setAttachment(attachment.getKey(), attachment.getValue());
        }
    }

    private static InvokeOutcome rpcFailure(RpcException failure) {
        InvokeOutcome outcome = new InvokeOutcome();
        if (failure.isTimeout()) {
            outcome.error = "rpc_timeout";
            outcome.rpcStatus = InvokeOutcome.TIMEOUT;
        } else if (failure.isSerialization()) {
            outcome.error = "serialization_error";
            outcome.rpcStatus = InvokeOutcome.SERIALIZATION;
        } else if (failure.isNetwork()) {
            outcome.error = "network_error";
            outcome.rpcStatus = InvokeOutcome.NETWORK;
        } else if (failure.isForbidden()) {
            outcome.error = "forbidden";
            outcome.rpcStatus = InvokeOutcome.FORBIDDEN;
        } else if (failure.getCode() == RpcException.NO_INVOKER_AVAILABLE_AFTER_FILTER) {
            outcome.error = "no_provider";
            outcome.rpcStatus = InvokeOutcome.NO_PROVIDER;
        } else {
            outcome.error = "rpc_exception";
            outcome.rpcStatus = InvokeOutcome.RPC_EXCEPTION;
        }
        outcome.exceptionType = exceptionType(failure);
        outcome.message = message(failure);
        return outcome;
    }

    private static InvokeOutcome unexpectedFailure(Throwable failure) {
        String text = message(failure);
        if (text != null && text.contains("No provider available")) {
            InvokeOutcome outcome = InvokeOutcome.failure(
                    "no_provider", InvokeOutcome.NO_PROVIDER, text);
            outcome.exceptionType = exceptionType(failure);
            return outcome;
        }
        InvokeOutcome outcome = InvokeOutcome.failure(
                "rpc_exception", InvokeOutcome.RPC_EXCEPTION, text);
        outcome.exceptionType = exceptionType(failure);
        return outcome;
    }

    private static String exceptionType(Throwable failure) {
        Throwable cause = failure.getCause();
        return (cause == null ? failure.getClass() : cause.getClass()).getName();
    }

    private static String message(Throwable failure) {
        String value = failure.getMessage();
        if (value == null || value.isEmpty()) {
            value = failure.getClass().getName();
        }
        return value.length() <= MAX_MESSAGE_CHARS
                ? value
                : value.substring(0, MAX_MESSAGE_CHARS);
    }

    private static String safe(String value) {
        return value == null ? "" : value;
    }

    @Override
    public void close() {
        synchronized (references) {
            for (ReferenceConfig<GenericService> reference : references.values()) {
                try {
                    reference.destroy();
                } catch (Throwable ignored) {
                    // 关闭阶段不因单条引用失败阻塞进程退出。
                }
            }
            references.clear();
        }
    }
}
