package io.astsight.agent.http;

import io.astsight.agent.core.AgentRuntime;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

import java.lang.instrument.ClassFileTransformer;
import java.security.ProtectionDomain;

/**
 * 织入 {@code feign.SynchronousMethodHandler#targetRequest(RequestTemplate)} 的返回值。
 *
 * <p>该方法在所有 {@code RequestInterceptor} 与 target 拼接之后返回最终
 * {@code feign.Request}，因此在返回前改写 URL 不会破坏 Feign 自身的模板语义。
 * 与 OkHttp 一样只按名称和描述符匹配，注入代码只引用 {@link FeignBridge}。</p>
 */
final class FeignTransformer implements ClassFileTransformer {

    static final String HANDLER = "feign/SynchronousMethodHandler";
    private static final String DESCRIPTOR =
            "(Lfeign/RequestTemplate;)Lfeign/Request;";
    private static final String BRIDGE = "io/astsight/agent/http/FeignBridge";

    private final AgentRuntime runtime;

    FeignTransformer(AgentRuntime runtime) {
        this.runtime = runtime;
    }

    @Override
    public byte[] transform(
            ClassLoader loader,
            String className,
            Class<?> classBeingRedefined,
            ProtectionDomain protectionDomain,
            byte[] classfileBuffer) {
        if (!HANDLER.equals(className)) {
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
                    if (!"targetRequest".equals(name)
                            || !DESCRIPTOR.equals(descriptor)) {
                        return delegate;
                    }
                    return new MethodVisitor(Opcodes.ASM9, delegate) {
                        @Override
                        public void visitInsn(int opcode) {
                            if (opcode == Opcodes.ARETURN) {
                                // 栈顶是刚构建好的 Request；bridge 未改写时原样返回。
                                visitMethodInsn(
                                        Opcodes.INVOKESTATIC,
                                        BRIDGE,
                                        "rewriteOrOriginal",
                                        "(Ljava/lang/Object;)Ljava/lang/Object;",
                                        false);
                                visitTypeInsn(Opcodes.CHECKCAST, "feign/Request");
                            }
                            super.visitInsn(opcode);
                        }
                    };
                }
            };
            reader.accept(visitor, 0);
            return writer.toByteArray();
        } catch (Throwable failure) {
            runtime.recordError("feign", failure);
            return null;
        }
    }
}
