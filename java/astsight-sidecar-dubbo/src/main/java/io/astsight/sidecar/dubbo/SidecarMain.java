package io.astsight.sidecar.dubbo;

import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.io.Reader;
import java.nio.charset.StandardCharsets;

/**
 * Dubbo sidecar 入口。
 *
 * <pre>
 * java -jar astsight-dubbo-sidecar.jar
 * </pre>
 *
 * <p>协议：stdin/stdout 各一行一个 JSON 对象，见
 * {@link SidecarServer}。启动后第一条请求通常是 {@code ping}，用于版本与
 * 协议号自检；收到 {@code shutdown} 或 stdin EOF 后退出。
 */
public final class SidecarMain {

    private SidecarMain() {
    }

    public static void main(String[] args) throws Exception {
        // stdout 只走协议；Dubbo / Netty 等第三方库的 System.out 一律转 stderr。
        PrintStream protocol = new PrintStream(
                new FileOutputStream(FileDescriptor.out), true, "UTF-8");
        System.setOut(System.err);
        DubboInvoker invoker = new DubboInvoker();
        try {
            Reader reader = new InputStreamReader(System.in, StandardCharsets.UTF_8);
            new SidecarServer(invoker).run(reader, protocol);
        } finally {
            invoker.close();
            protocol.flush();
        }
    }

    static String version() {
        Package owner = SidecarMain.class.getPackage();
        String value = owner == null ? null : owner.getImplementationVersion();
        return value == null || value.isEmpty() ? "dev" : value;
    }
}
