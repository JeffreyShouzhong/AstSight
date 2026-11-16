package io.astsight.agent.http;

import io.astsight.agent.core.AgentRuntime;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;
import org.objectweb.asm.Type;

import java.lang.instrument.ClassFileTransformer;
import java.security.ProtectionDomain;

/**
 * 织入 HttpClient 5 classic 的 {@code InternalHttpClient#doExecute}。
 *
 * <p>5.0–5.4 的方法签名在参数数量上略有差异，这里按描述符定位
 * {@code HttpHost} / {@code ClassicHttpRequest} 槽位，其余参数原样保留。</p>
 */
final class HttpClient5Transformer implements ClassFileTransformer {

    static final String CLIENT =
            "org/apache/hc/client5/http/impl/classic/InternalHttpClient";
    private static final String HOST = "org/apache/hc/core5/http/HttpHost";
    private static final String REQUEST =
            "org/apache/hc/core5/http/ClassicHttpRequest";
    private static final String BRIDGE =
            "io/astsight/agent/http/ApacheHttpClientBridge";

    private final AgentRuntime runtime;

    HttpClient5Transformer(AgentRuntime runtime) {
        this.runtime = runtime;
    }

    @Override
    public byte[] transform(
            ClassLoader loader,
            String className,
            Class<?> classBeingRedefined,
            ProtectionDomain protectionDomain,
            byte[] classfileBuffer) {
        if (!CLIENT.equals(className)) {
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
                    if (!"doExecute".equals(name)
                            || !startsWithHostRequest(descriptor)) {
                        return delegate;
                    }
                    final int temp = tempSlot(descriptor);
                    return new MethodVisitor(Opcodes.ASM9, delegate) {
                        @Override
                        public void visitCode() {
                            super.visitCode();
                            visitVarInsn(Opcodes.ALOAD, 1);
                            visitVarInsn(Opcodes.ALOAD, 2);
                            visitMethodInsn(
                                    Opcodes.INVOKESTATIC,
                                    BRIDGE,
                                    "rewriteOrOriginal",
                                    "(Ljava/lang/Object;Ljava/lang/Object;)"
                                            + "[Ljava/lang/Object;",
                                    false);
                            visitVarInsn(Opcodes.ASTORE, temp);

                            visitVarInsn(Opcodes.ALOAD, temp);
                            visitInsn(Opcodes.ICONST_0);
                            visitInsn(Opcodes.AALOAD);
                            visitTypeInsn(Opcodes.CHECKCAST, HOST);
                            visitVarInsn(Opcodes.ASTORE, 1);

                            visitVarInsn(Opcodes.ALOAD, temp);
                            visitInsn(Opcodes.ICONST_1);
                            visitInsn(Opcodes.AALOAD);
                            visitTypeInsn(Opcodes.CHECKCAST, REQUEST);
                            visitVarInsn(Opcodes.ASTORE, 2);
                        }
                    };
                }
            };
            reader.accept(visitor, 0);
            return writer.toByteArray();
        } catch (Throwable failure) {
            runtime.recordError("httpclient5", failure);
            return null;
        }
    }

    private static boolean startsWithHostRequest(String descriptor) {
        Type[] arguments;
        try {
            arguments = Type.getArgumentTypes(descriptor);
        } catch (Throwable invalid) {
            return false;
        }
        return arguments.length >= 3
                && ("L" + HOST + ";").equals(arguments[0].getDescriptor())
                && ("L" + REQUEST + ";").equals(arguments[1].getDescriptor());
    }

    private static int tempSlot(String descriptor) {
        int slot = 1;
        for (Type argument : Type.getArgumentTypes(descriptor)) {
            slot += argument.getSize();
        }
        return slot;
    }
}
