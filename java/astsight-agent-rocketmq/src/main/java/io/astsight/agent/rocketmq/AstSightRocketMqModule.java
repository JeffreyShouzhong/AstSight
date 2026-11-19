package io.astsight.agent.rocketmq;

import io.astsight.agent.core.AgentModule;
import io.astsight.agent.core.AgentRuntime;

import java.lang.instrument.Instrumentation;

/**
 * RocketMQ 生产者侧拦截：命中计划内 topic 时不真正发送。
 *
 * <p>织入 {@code DefaultMQProducer} 的同步/异步 send 重载；异步回调会收到一个
 * 合成的 SendResult（构造失败时为 null）。</p>
 */
public final class AstSightRocketMqModule implements AgentModule {

    @Override
    public String id() {
        return "rocketmq-producer";
    }

    @Override
    public void install(Instrumentation instrumentation, AgentRuntime runtime) {
        instrumentation.addTransformer(
                new RocketMqProducerTransformer(runtime), true);
        runtime.registerAdapter(id(), version());
        if (available()) {
            runtime.mqModuleInstalled("rocketmq");
        }
    }

    private static boolean available() {
        try {
            Class.forName("org.apache.rocketmq.client.producer.DefaultMQProducer");
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static String version() {
        try {
            Class<?> producer = Class.forName(
                    "org.apache.rocketmq.client.producer.DefaultMQProducer");
            Package pkg = producer.getPackage();
            return pkg == null ? null : pkg.getImplementationVersion();
        } catch (Throwable ignored) {
            return null;
        }
    }
}
