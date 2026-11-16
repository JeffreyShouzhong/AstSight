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
 * 直接用 ASM 改写 {@code OkHttpClient#newCall(Request)} 的入口。
 *
 * <p>不使用 ByteBuddy Advice：Spring Boot fat jar 中 OkHttp 由
 * {@code LaunchedURLClassLoader} 加载，ByteBuddy 在解析方法签名时会尝试从 agent
 * 类加载器加载 {@code okhttp3.Request} 并失败。这里只按名称匹配方法与 JVM 描述符，
 * 注入的调用只引用 JDK 类型和 {@link OkHttpBridge}，不需要解析 OkHttp 类型。</p>
 */
final class OkHttpTransformer implements ClassFileTransformer {

    static final String CLIENT = "okhttp3/OkHttpClient";
    private static final String REQUEST_DESCRIPTOR_PREFIX = "(Lokhttp3/Request;)";
    private static final String BRIDGE = "io/astsight/agent/http/OkHttpBridge";

    private final AgentRuntime runtime;

    OkHttpTransformer(AgentRuntime runtime) {
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
                    if (!"newCall".equals(name)
                            || !descriptor.startsWith(REQUEST_DESCRIPTOR_PREFIX)) {
                        return delegate;
                    }
                    return new MethodVisitor(Opcodes.ASM9, delegate) {
                        @Override
                        public void visitCode() {
                            super.visitCode();
                            // 实例方法：local 0 = this，local 1 = Request。
                            // 无条件写回；bridge 在未改写时返回原对象。
                            visitVarInsn(Opcodes.ALOAD, 1);
                            visitMethodInsn(
                                    Opcodes.INVOKESTATIC,
                                    BRIDGE,
                                    "rewriteOrOriginal",
                                    "(Ljava/lang/Object;)Ljava/lang/Object;",
                                    false);
                            visitTypeInsn(Opcodes.CHECKCAST, "okhttp3/Request");
                            visitVarInsn(Opcodes.ASTORE, 1);
                        }
                    };
                }
            };
            reader.accept(visitor, 0);
            return writer.toByteArray();
        } catch (Throwable failure) {
            runtime.recordError("okhttp", failure);
            return null;
        }
    }
}
