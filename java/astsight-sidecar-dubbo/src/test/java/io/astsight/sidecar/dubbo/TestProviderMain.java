package io.astsight.sidecar.dubbo;

import org.apache.dubbo.config.ApplicationConfig;
import org.apache.dubbo.config.ProtocolConfig;
import org.apache.dubbo.config.RegistryConfig;
import org.apache.dubbo.config.ServiceConfig;

import java.io.IOException;

/**
 * 供 Python e2e 启动的真 provider：导出 {@link EchoService} 并打印端口。
 *
 * <p>用法：{@code TestProviderMain <port>}；stdin 关闭或进程被杀后退出。
 */
public final class TestProviderMain {

    private TestProviderMain() {
    }

    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 20880;

        ApplicationConfig application = new ApplicationConfig("astsight-sidecar-test-provider");
        application.setQosEnable(Boolean.FALSE);

        ProtocolConfig protocol = new ProtocolConfig("dubbo", port);
        protocol.setSerialization("hessian2");

        ServiceConfig<EchoService> service = new ServiceConfig<EchoService>();
        service.setApplication(application);
        service.setProtocol(protocol);
        service.setRegistry(new RegistryConfig("N/A"));
        service.setRegister(Boolean.FALSE);
        service.setInterface(EchoService.class);
        service.setGroup("order");
        service.setVersion("1.0.0");
        service.setRef(new EchoServiceImpl());
        service.export();

        int actualPort = service.getExportedUrls().get(0).getPort();
        System.out.println("READY " + actualPort);
        System.out.flush();

        Runtime.getRuntime().addShutdownHook(new Thread(service::unexport));
        try {
            while (System.in.read() != -1) {
                // 等待父进程关闭 stdin。
            }
        } catch (IOException ignored) {
            // 父进程直接断开时也走正常关闭。
        }
        service.unexport();
    }
}
