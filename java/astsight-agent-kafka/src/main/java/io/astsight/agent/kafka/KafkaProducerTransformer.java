package io.astsight.agent.kafka;

import io.astsight.agent.core.AgentRuntime;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Label;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

import java.lang.instrument.ClassFileTransformer;
import java.security.ProtectionDomain;

/**
 * 只改写 {@code KafkaProducer.send(ProducerRecord, Callback)}。
 *
 * <p>方法入口调用 {@link KafkaBridge#suppressOrNull(Object, Object)}：返回非空
 * Future 表示计划内消息已拦截，直接 ARETURN；返回 null 继续执行 Kafka 原始
 * 发送逻辑。不依赖 kafka-clients 类型，避免 agent 类加载器在 fat jar 场景下
 * 解析不到应用侧类。</p>
 */
final class KafkaProducerTransformer implements ClassFileTransformer {

    static final String PRODUCER =
            "org/apache/kafka/clients/producer/KafkaProducer";
    private static final String BRIDGE =
            "io/astsight/agent/kafka/KafkaBridge";
    private static final String SEND_DESCRIPTOR =
            "(Lorg/apache/kafka/clients/producer/ProducerRecord;"
            + "Lorg/apache/kafka/clients/producer/Callback;)"
            + "Ljava/util/concurrent/Future;";

    private final AgentRuntime runtime;

    KafkaProducerTransformer(AgentRuntime runtime) {
        this.runtime = runtime;
    }

    @Override
    public byte[] transform(
            ClassLoader loader,
            String className,
            Class<?> classBeingRedefined,
            ProtectionDomain protectionDomain,
            byte[] classfileBuffer) {
        if (!PRODUCER.equals(className)) {
            return null;
        }
        try {
            ClassReader reader = new ClassReader(classfileBuffer);
            ClassWriter writer = new ClassWriter(reader, ClassWriter.COMPUTE_MAXS);
            ClassVisitor visitor = new ClassVisitor(Opcodes.ASM9, writer) {
                @Override
                public MethodVisitor visitMethod(
                        int access,
                        String name,
                        String descriptor,
                        String signature,
                        String[] exceptions) {
                    MethodVisitor delegate =
                            super.visitMethod(access, name, descriptor, signature, exceptions);
                    if (!"send".equals(name) || !SEND_DESCRIPTOR.equals(descriptor)) {
                        return delegate;
                    }
                    return new MethodVisitor(Opcodes.ASM9, delegate) {
                        @Override
                        public void visitCode() {
                            super.visitCode();
                            Label original = new Label();
                            // local 0=this, 1=ProducerRecord, 2=Callback。
                            visitVarInsn(Opcodes.ALOAD, 1);
                            visitVarInsn(Opcodes.ALOAD, 2);
                            visitMethodInsn(
                                    Opcodes.INVOKESTATIC,
                                    BRIDGE,
                                    "suppressOrNull",
                                    "(Ljava/lang/Object;Ljava/lang/Object;)"
                                            + "Ljava/util/concurrent/Future;",
                                    false);
                            visitInsn(Opcodes.DUP);
                            visitJumpInsn(Opcodes.IFNULL, original);
                            visitInsn(Opcodes.ARETURN);
                            visitLabel(original);
                            visitFrame(
                                    Opcodes.F_SAME1,
                                    0,
                                    null,
                                    1,
                                    new Object[]{"java/util/concurrent/Future"});
                            visitInsn(Opcodes.POP);
                        }
                    };
                }
            };
            reader.accept(visitor, 0);
            return writer.toByteArray();
        } catch (Throwable failure) {
            runtime.recordError("kafka-producer", failure);
            return null;
        }
    }
}
