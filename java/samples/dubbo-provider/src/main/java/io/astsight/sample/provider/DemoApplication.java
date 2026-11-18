package io.astsight.sample.provider;

import org.apache.dubbo.config.ApplicationConfig;
import org.apache.dubbo.config.ProtocolConfig;
import org.apache.dubbo.config.RegistryConfig;
import org.apache.dubbo.config.ServiceConfig;
import org.springframework.boot.CommandLineRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.EnvironmentAware;
import org.springframework.core.env.Environment;

import java.util.concurrent.CountDownLatch;

/**
 * 常驻 Dubbo provider 样例：不启注册中心、不启 Web 端口，直接导出
 * {@link OrderService}，供 AstSight sidecar 泛化调用。
 *
 * <p>端口来自 `application.yml` 的 `dubbo.protocol.port`，可用
 * `--dubbo.protocol.port=20881` 覆盖。启动完成后打印 `READY &lt;port&gt;`。</p>
 */
@SpringBootApplication
public class DemoApplication implements CommandLineRunner, EnvironmentAware {

    private Environment environment;
    private ServiceConfig<OrderService> service;

    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }

    @Override
    public void setEnvironment(Environment environment) {
        this.environment = environment;
    }

    @Override
    public void run(String... args) {
        int port = environment.getProperty(
                "dubbo.protocol.port", Integer.class, 20880);

        ApplicationConfig application =
                new ApplicationConfig("astsight-dubbo-provider");
        application.setQosEnable(Boolean.FALSE);

        ProtocolConfig protocol = new ProtocolConfig("dubbo", port);
        protocol.setSerialization("hessian2");

        service = new ServiceConfig<OrderService>();
        service.setApplication(application);
        service.setProtocol(protocol);
        service.setRegistry(new RegistryConfig("N/A"));
        service.setRegister(Boolean.FALSE);
        service.setInterface(OrderService.class);
        service.setGroup("order");
        service.setVersion("1.0.0");
        service.setRef(new OrderServiceImpl());
        service.export();

        System.out.println("READY " + port);
        System.out.flush();

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            if (service != null) {
                service.unexport();
            }
        }));

        // 非 Web 的 Spring Boot 应用在 runner 返回后会退出；阻塞住主线程，
        // 让 provider 常驻，直到收到终止信号。
        try {
            new CountDownLatch(1).await();
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }
}
