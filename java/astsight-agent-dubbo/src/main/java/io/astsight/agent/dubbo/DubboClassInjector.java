package io.astsight.agent.dubbo;

import io.astsight.agent.core.AgentRuntime;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.security.CodeSource;
import java.security.ProtectionDomain;
import java.security.cert.Certificate;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import java.util.WeakHashMap;

/**
 * 把 {@link AstSightDubboFilter} 定义进被测应用的类加载器。
 *
 * <p>agent jar 在 system classloader，而 Dubbo 在应用类加载器（普通
 * classpath 场景两者相同，Spring Boot fat jar 场景不同）。Filter 必须由能看到
 * Dubbo 接口的加载器定义，Dubbo SPI 才能实例化它。这里不增强任何 Dubbo 类：
 * 只监听 Dubbo 扩展加载入口，把 agent jar 里的 Filter 字节定义到同一个
 * 类加载器。</p>
 */
final class DubboClassInjector {

    private static final String FILTER_CLASS = "io.astsight.agent.dubbo.AstSightDubboFilter";
    private static final String FILTER_RESOURCE =
            "/io/astsight/agent/dubbo/AstSightDubboFilter.class";

    /**
     * 触发注入的类：必须是 Dubbo 扩展扫描前会加载、且 Filter 自身不引用的类，
     * 否则会在同一次类加载里递归加载 Filter 的接口。
     */
    private static final Set<String> TRIGGERS = Collections.singleton(
            "org/apache/dubbo/common/extension/ExtensionLoader");

    private final Instrumentation instrumentation;
    private final AgentRuntime runtime;
    private final byte[] filterBytes;
    private final Map<ClassLoader, Boolean> injected =
            Collections.synchronizedMap(new WeakHashMap<ClassLoader, Boolean>());

    DubboClassInjector(Instrumentation instrumentation, AgentRuntime runtime) {
        this.instrumentation = instrumentation;
        this.runtime = runtime;
        this.filterBytes = readFilterBytes();
    }

    void install() {
        if (filterBytes == null) {
            runtime.dubboError("filter class bytes missing");
            return;
        }
        openJavaLang(null);
        instrumentation.addTransformer(new ClassFileTransformer() {
            @Override
            public byte[] transform(
                    ClassLoader loader,
                    String className,
                    Class<?> classBeingRedefined,
                    ProtectionDomain protectionDomain,
                    byte[] classfileBuffer) {
                if (loader != null && TRIGGERS.contains(className)) {
                    try {
                        define(loader);
                    } catch (Throwable failure) {
                        runtime.dubboError("inject failed: " + failure);
                    }
                }
                return null;
            }
        }, false);
    }

    /**
     * JDK 9+ 需要把 {@code java.lang} 打开给相关模块，才能反射调用
     * {@code ClassLoader#defineClass}；JDK 8 没有模块系统，直接可设访问。
     * 这里全部用反射，保持 release=8 可编译。
     *
     * <p>除 agent 自己所在模块外，还要打开给应用类加载器对应的 unnamed
     * module：Spring Boot fat jar 里应用是独立 unnamed module，只开给 agent
     * 自己时，Filter 注入和 Dubbo 2.7.x 自带 javassist 编译都会失败。</p>
     *
     * @param loader 应用类加载器；{@code null} 表示只打开给 agent 自己
     */
    private void openJavaLang(ClassLoader loader) {
        try {
            Class<?> moduleType = Class.forName("java.lang.Module");
            Method getModule = Class.class.getMethod("getModule");
            Method redefine = Instrumentation.class.getMethod(
                    "redefineModule",
                    moduleType,
                    Set.class,
                    Map.class,
                    Map.class,
                    Set.class,
                    Map.class);
            Object base = getModule.invoke(Object.class);
            Set<Object> targets = new LinkedHashSet<Object>();
            targets.add(getModule.invoke(DubboClassInjector.class));
            if (loader != null) {
                Object appModule = ClassLoader.class
                        .getMethod("getUnnamedModule")
                        .invoke(loader);
                if (appModule != null) {
                    targets.add(appModule);
                }
            }
            Map<String, Set<Object>> opens = Collections.<String, Set<Object>>singletonMap(
                    "java.lang", targets);
            redefine.invoke(
                    instrumentation,
                    base,
                    Collections.<Object>emptySet(),
                    Collections.<String, Set<Object>>emptyMap(),
                    opens,
                    Collections.<Object>emptySet(),
                    Collections.<Object, Object>emptyMap());
        } catch (Throwable ignored) {
            // JDK 8 没有 Module；继续走 setAccessible。
        }
    }

    private void define(ClassLoader loader) throws Exception {
        synchronized (injected) {
            if (injected.containsKey(loader)) {
                return;
            }
            // 先占位，避免定义过程中再次触发同一加载器。
            injected.put(loader, Boolean.TRUE);
        }
        openJavaLang(loader);
        try {
            Method method = ClassLoader.class.getDeclaredMethod(
                    "defineClass",
                    String.class,
                    byte[].class,
                    int.class,
                    int.class,
                    ProtectionDomain.class);
            method.setAccessible(true);
            method.invoke(
                    loader,
                    FILTER_CLASS,
                    filterBytes,
                    0,
                    filterBytes.length,
                    new ProtectionDomain(
                            new CodeSource(null, (Certificate[]) null), null));
        } catch (InvocationTargetException failure) {
            Throwable cause = failure.getCause();
            if (cause instanceof LinkageError
                    && String.valueOf(cause.getMessage()).contains("duplicate")) {
                return;
            }
            throw failure;
        }
    }

    private static byte[] readFilterBytes() {
        InputStream stream = DubboClassInjector.class.getResourceAsStream(FILTER_RESOURCE);
        if (stream == null) {
            return null;
        }
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int count;
            while ((count = stream.read(buffer)) != -1) {
                out.write(buffer, 0, count);
            }
            return out.toByteArray();
        } catch (Exception failure) {
            return null;
        } finally {
            try {
                stream.close();
            } catch (Exception ignored) {
                // 读取失败已由返回值表达。
            }
        }
    }
}
