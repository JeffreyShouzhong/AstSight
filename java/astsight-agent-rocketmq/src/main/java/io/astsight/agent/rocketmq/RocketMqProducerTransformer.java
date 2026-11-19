package io.astsight.agent.rocketmq;

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
 * 改写 {@code DefaultMQProducer} 的 send 重载。
 *
 * <p>同步方法入口调用 {@link RocketMqBridge#sync(Object)}：返回 null 表示计划外，
 * 继续原发送；返回 Object[] 表示已拦截，第 0 项是合成的 SendResult（可能为 null）。
 * 异步方法调用 {@link RocketMqBridge#async(Object, Object)}，命中后直接 RETURN。</p>
 */
final class RocketMqProducerTransformer implements ClassFileTransformer {

    static final String PRODUCER =
            "org/apache/rocketmq/client/producer/DefaultMQProducer";
    private static final String BRIDGE =
            "io/astsight/agent/rocketmq/RocketMqBridge";
    private static final String MESSAGE =
            "Lorg/apache/rocketmq/common/message/Message;";
    private static final String CALLBACK =
            "Lorg/apache/rocketmq/client/producer/SendCallback;";
    private static final String RESULT =
            "Lorg/apache/rocketmq/client/producer/SendResult;";

    private final AgentRuntime runtime;

    RocketMqProducerTransformer(AgentRuntime runtime) {
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
                    if (!"send".equals(name)
                            || descriptor == null
                            || !descriptor.startsWith("(" + MESSAGE)) {
                        return delegate;
                    }
                    if (descriptor.endsWith(")" + RESULT)) {
                        return syncVisitor(delegate);
                    }
                    int callbackIndex = callbackIndex(descriptor);
                    if (descriptor.endsWith(")V") && callbackIndex > 0) {
                        return asyncVisitor(delegate, callbackIndex);
                    }
                    return delegate;
                }
            };
            reader.accept(visitor, 0);
            return writer.toByteArray();
        } catch (Throwable failure) {
            runtime.recordError("rocketmq-producer", failure);
            return null;
        }
    }

    private MethodVisitor syncVisitor(MethodVisitor delegate) {
        return new MethodVisitor(Opcodes.ASM9, delegate) {
            @Override
            public void visitCode() {
                super.visitCode();
                Label original = new Label();
                visitVarInsn(Opcodes.ALOAD, 1);
                visitMethodInsn(
                        Opcodes.INVOKESTATIC,
                        BRIDGE,
                        "sync",
                        "(Ljava/lang/Object;)[Ljava/lang/Object;",
                        false);
                visitInsn(Opcodes.DUP);
                visitJumpInsn(Opcodes.IFNULL, original);
                visitTypeInsn(Opcodes.CHECKCAST, "[Ljava/lang/Object;");
                visitInsn(Opcodes.ICONST_0);
                visitInsn(Opcodes.AALOAD);
                visitTypeInsn(Opcodes.CHECKCAST, RESULT.substring(1, RESULT.length() - 1));
                visitInsn(Opcodes.ARETURN);
                visitLabel(original);
                visitFrame(
                        Opcodes.F_SAME1,
                        0,
                        null,
                        1,
                        new Object[]{"[Ljava/lang/Object;"});
                visitInsn(Opcodes.POP);
            }
        };
    }

    private MethodVisitor asyncVisitor(MethodVisitor delegate, int callbackIndex) {
        return new MethodVisitor(Opcodes.ASM9, delegate) {
            @Override
            public void visitCode() {
                super.visitCode();
                Label original = new Label();
                visitVarInsn(Opcodes.ALOAD, 1);
                visitVarInsn(Opcodes.ALOAD, callbackIndex);
                visitMethodInsn(
                        Opcodes.INVOKESTATIC,
                        BRIDGE,
                        "async",
                        "(Ljava/lang/Object;Ljava/lang/Object;)Z",
                        false);
                visitJumpInsn(Opcodes.IFEQ, original);
                visitInsn(Opcodes.RETURN);
                visitLabel(original);
                visitFrame(Opcodes.F_SAME, 0, null, 0, null);
            }
        };
    }

    /** 在描述符参数中找出 SendCallback 的局部变量下标；找不到返回 -1。 */
    private static int callbackIndex(String descriptor) {
        int cursor = 1;
        int index = 1;
        while (cursor < descriptor.length() && descriptor.charAt(cursor) != ')') {
            char type = descriptor.charAt(cursor);
            if (type == '[') {
                cursor++;
                while (cursor < descriptor.length()
                        && descriptor.charAt(cursor) == '[') {
                    cursor++;
                }
                if (descriptor.charAt(cursor) == 'L') {
                    cursor = descriptor.indexOf(';', cursor) + 1;
                } else {
                    cursor++;
                }
                index++;
                continue;
            }
            if (type == 'L') {
                int end = descriptor.indexOf(';', cursor);
                String value = descriptor.substring(cursor, end + 1);
                if (CALLBACK.equals(value)) {
                    return index;
                }
                cursor = end + 1;
                index++;
                continue;
            }
            cursor++;
            index += (type == 'J' || type == 'D') ? 2 : 1;
        }
        return -1;
    }
}
