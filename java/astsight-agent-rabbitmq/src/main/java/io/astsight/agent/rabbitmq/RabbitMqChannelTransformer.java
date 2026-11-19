package io.astsight.agent.rabbitmq;

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
 * 只改写 {@code ChannelN.basicPublish} 的三个重载。
 *
 * <p>方法入口调用 {@link RabbitMqBridge#suppress(String, String, Object, byte[])}；
 * 返回 true 时直接 RETURN，真实 publish 被跳过。不依赖 RabbitMQ 类型。</p>
 */
final class RabbitMqChannelTransformer implements ClassFileTransformer {

    static final String CHANNEL = "com/rabbitmq/client/impl/ChannelN";
    private static final String BRIDGE =
            "io/astsight/agent/rabbitmq/RabbitMqBridge";
    private static final String PREFIX =
            "(Ljava/lang/String;Ljava/lang/String;";
    private static final String SUFFIX = "Lcom/rabbitmq/client/AMQP$BasicProperties;[B)V";

    private final AgentRuntime runtime;

    RabbitMqChannelTransformer(AgentRuntime runtime) {
        this.runtime = runtime;
    }

    @Override
    public byte[] transform(
            ClassLoader loader,
            String className,
            Class<?> classBeingRedefined,
            ProtectionDomain protectionDomain,
            byte[] classfileBuffer) {
        if (!CHANNEL.equals(className)) {
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
                    int propsIndex = propsIndex(descriptor);
                    if (!"basicPublish".equals(name) || propsIndex < 0) {
                        return delegate;
                    }
                    return new MethodVisitor(Opcodes.ASM9, delegate) {
                        @Override
                        public void visitCode() {
                            super.visitCode();
                            Label original = new Label();
                            visitVarInsn(Opcodes.ALOAD, 1);
                            visitVarInsn(Opcodes.ALOAD, 2);
                            visitVarInsn(Opcodes.ALOAD, propsIndex);
                            visitVarInsn(Opcodes.ALOAD, propsIndex + 1);
                            visitMethodInsn(
                                    Opcodes.INVOKESTATIC,
                                    BRIDGE,
                                    "suppress",
                                    "(Ljava/lang/String;Ljava/lang/String;"
                                            + "Ljava/lang/Object;[B)Z",
                                    false);
                            visitJumpInsn(Opcodes.IFEQ, original);
                            visitInsn(Opcodes.RETURN);
                            visitLabel(original);
                            visitFrame(Opcodes.F_SAME, 0, null, 0, null);
                        }
                    };
                }
            };
            reader.accept(visitor, 0);
            return writer.toByteArray();
        } catch (Throwable failure) {
            runtime.recordError("rabbitmq-producer", failure);
            return null;
        }
    }

    /** 返回 BasicProperties 参数下标；非 basicPublish 签名返回 -1。 */
    private static int propsIndex(String descriptor) {
        if (descriptor == null || !descriptor.startsWith(PREFIX)
                || !descriptor.endsWith(SUFFIX)) {
            return -1;
        }
        String middle = descriptor.substring(
                PREFIX.length(), descriptor.length() - SUFFIX.length());
        // 4 参: ""；5 参: "Z"；6 参: "ZZ"。
        if (middle.isEmpty()) {
            return 3;
        }
        if ("Z".equals(middle)) {
            return 4;
        }
        if ("ZZ".equals(middle)) {
            return 5;
        }
        return -1;
    }
}
