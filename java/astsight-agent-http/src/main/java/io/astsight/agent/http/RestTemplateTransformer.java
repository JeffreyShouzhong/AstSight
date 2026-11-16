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
 * 织入 {@code RestTemplate#doExecute(URI, ...)}：替换 URI 与 RequestCallback。
 *
 * <p>Spring 5 只有 4 参重载，Spring 6.2 起新增带 uriTemplate 的 5 参重载
 * （4 参委托给 5 参）。这里按描述符定位参数槽位，两个重载都织入；第二次
 * 进入时 URL 已是 Mock 地址，{@code AgentRuntime} 会直接放行。</p>
 *
 * <p>所有 RestTemplate 工厂（JDK / Apache HC / OkHttp / Jetty 等）最终都走
 * {@code doExecute}，因此不需要分别为每种 RequestFactory 做适配。</p>
 */
final class RestTemplateTransformer implements ClassFileTransformer {

    static final String REST_TEMPLATE = "org/springframework/web/client/RestTemplate";
    private static final String URI = "java/net/URI";
    private static final String HTTP_METHOD = "org/springframework/http/HttpMethod";
    private static final String CALLBACK =
            "org/springframework/web/client/RequestCallback";
    private static final String BRIDGE =
            "io/astsight/agent/http/RestTemplateBridge";

    private final AgentRuntime runtime;

    RestTemplateTransformer(AgentRuntime runtime) {
        this.runtime = runtime;
    }

    @Override
    public byte[] transform(
            ClassLoader loader,
            String className,
            Class<?> classBeingRedefined,
            ProtectionDomain protectionDomain,
            byte[] classfileBuffer) {
        if (!REST_TEMPLATE.equals(className)) {
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
                    if (!"doExecute".equals(name)) {
                        return delegate;
                    }
                    Slots slots = Slots.parse(descriptor);
                    if (slots == null) {
                        return delegate;
                    }
                    final int temp = slots.slots;
                    return new MethodVisitor(Opcodes.ASM9, delegate) {
                        @Override
                        public void visitCode() {
                            super.visitCode();
                            visitVarInsn(Opcodes.ALOAD, 0);
                            visitVarInsn(Opcodes.ALOAD, slots.uri);
                            visitVarInsn(Opcodes.ALOAD, slots.method);
                            visitVarInsn(Opcodes.ALOAD, slots.callback);
                            visitMethodInsn(
                                    Opcodes.INVOKESTATIC,
                                    BRIDGE,
                                    "rewriteCall",
                                    "(Ljava/lang/Object;Ljava/lang/Object;"
                                            + "Ljava/lang/Object;Ljava/lang/Object;)"
                                            + "[Ljava/lang/Object;",
                                    false);
                            visitVarInsn(Opcodes.ASTORE, temp);

                            visitVarInsn(Opcodes.ALOAD, temp);
                            visitInsn(Opcodes.ICONST_0);
                            visitInsn(Opcodes.AALOAD);
                            visitTypeInsn(Opcodes.CHECKCAST, URI);
                            visitVarInsn(Opcodes.ASTORE, slots.uri);

                            visitVarInsn(Opcodes.ALOAD, temp);
                            visitInsn(Opcodes.ICONST_1);
                            visitInsn(Opcodes.AALOAD);
                            visitTypeInsn(Opcodes.CHECKCAST, CALLBACK);
                            visitVarInsn(Opcodes.ASTORE, slots.callback);
                        }
                    };
                }
            };
            reader.accept(visitor, 0);
            return writer.toByteArray();
        } catch (Throwable failure) {
            runtime.recordError("resttemplate", failure);
            return null;
        }
    }

    /** 从描述符解析 {@code doExecute} 的局部变量槽位。 */
    private static final class Slots {
        private final int slots;
        private final int uri;
        private final int method;
        private final int callback;

        private Slots(int slots, int uri, int method, int callback) {
            this.slots = slots;
            this.uri = uri;
            this.method = method;
            this.callback = callback;
        }

        static Slots parse(String descriptor) {
            Type[] arguments;
            try {
                arguments = Type.getArgumentTypes(descriptor);
            } catch (Throwable invalid) {
                return null;
            }
            if (arguments.length < 4) {
                return null;
            }
            int uri = -1;
            int method = -1;
            int callback = -1;
            int slot = 1;
            int total = 1;
            for (int i = 0; i < arguments.length; i++) {
                Type argument = arguments[i];
                String name = argument.getDescriptor();
                if (("L" + URI + ";").equals(name) && uri == -1) {
                    uri = slot;
                } else if (("L" + HTTP_METHOD + ";").equals(name)) {
                    method = slot;
                } else if (("L" + CALLBACK + ";").equals(name)) {
                    callback = slot;
                }
                slot += argument.getSize();
                total = slot;
            }
            if (uri == -1 || method == -1 || callback == -1) {
                return null;
            }
            return new Slots(total, uri, method, callback);
        }
    }
}
