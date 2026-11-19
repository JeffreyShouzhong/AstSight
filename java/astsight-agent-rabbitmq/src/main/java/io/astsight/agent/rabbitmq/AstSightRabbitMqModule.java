package io.astsight.agent.rabbitmq;

import io.astsight.agent.core.AgentModule;
import io.astsight.agent.core.AgentRuntime;

import java.lang.instrument.Instrumentation;

/**
 * RabbitMQ 生产者侧拦截：命中计划内 exchange/routingKey 时不真正 publish。
 *
 * <p>织入 {@code ChannelN} 的三个 {@code basicPublish} 重载；Spring AMQP
 * 最终也会走这里。</p>
 */
public final class AstSightRabbitMqModule implements AgentModule {

    @Override
    public String id() {
        return "rabbitmq-producer";
    }

    @Override
    public void install(Instrumentation instrumentation, AgentRuntime runtime) {
        instrumentation.addTransformer(new RabbitMqChannelTransformer(runtime), true);
        runtime.registerAdapter(id(), version());
        if (available()) {
            runtime.mqModuleInstalled("rabbitmq");
        }
    }

    private static boolean available() {
        try {
            Class.forName("com.rabbitmq.client.impl.ChannelN");
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static String version() {
        try {
            Class<?> channel = Class.forName("com.rabbitmq.client.impl.ChannelN");
            Package pkg = channel.getPackage();
            return pkg == null ? null : pkg.getImplementationVersion();
        } catch (Throwable ignored) {
            return null;
        }
    }
}
