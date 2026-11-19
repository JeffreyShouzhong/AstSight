package io.astsight.agent.kafka;

import io.astsight.agent.core.AgentModule;
import io.astsight.agent.core.AgentRuntime;

import java.lang.instrument.Instrumentation;

/**
 * Kafka 生产者侧拦截：命中计划内 topic 时不真正发送，只上报消息内容。
 *
 * <p>只织入 {@code KafkaProducer.send(ProducerRecord, Callback)}；Kafka 的
 * 单参数 {@code send(record)} 在该类里会委托到双参数版本。</p>
 */
public final class AstSightKafkaModule implements AgentModule {

    @Override
    public String id() {
        return "kafka-producer";
    }

    @Override
    public void install(Instrumentation instrumentation, AgentRuntime runtime) {
        instrumentation.addTransformer(new KafkaProducerTransformer(runtime), true);
        runtime.registerAdapter(id(), version());
        if (available()) {
            runtime.mqModuleInstalled("kafka");
        }
    }

    private static boolean available() {
        try {
            Class.forName("org.apache.kafka.clients.producer.KafkaProducer");
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static String version() {
        try {
            Class<?> producer =
                    Class.forName("org.apache.kafka.clients.producer.KafkaProducer");
            Package pkg = producer.getPackage();
            return pkg == null ? null : pkg.getImplementationVersion();
        } catch (Throwable ignored) {
            return null;
        }
    }
}
