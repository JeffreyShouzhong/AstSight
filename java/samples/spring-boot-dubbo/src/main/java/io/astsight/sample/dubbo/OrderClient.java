package io.astsight.sample.dubbo;

import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import org.apache.dubbo.config.ApplicationConfig;
import org.apache.dubbo.config.ReferenceConfig;
import org.apache.dubbo.config.annotation.DubboReference;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 消费端样例：`@DubboReference` 字段是画像的读取对象；运行时由
 * {@link ReferenceConfig} 直连 URL 填同一个字段。
 *
 * <p>为什么不直接让 Dubbo 处理注解：Dubbo 2.7.x 的注解处理依赖 Spring 4/5
 * 的集成（`javax`），与 Spring Boot 3 不兼容；本样例也不启注册中心与
 * provider。所以注解只承担“源码形态”，代理由编程式引用创建，两者指向同一
 * 接口、同一 group/version，dep_id 一致。</p>
 */
@Component
public class OrderClient {

    @DubboReference(interfaceClass = OrderApi.class, group = "order", version = "1.0.0")
    private OrderApi orderApi;

    private final String url;
    private ReferenceConfig<OrderApi> reference;

    public OrderClient(@Value("${dubbo.url}") String url) {
        this.url = url;
    }

    @PostConstruct
    void init() {
        // 不启动 QoS 端口；lazy=true 让未命中 Mock 时才真正建连。
        System.setProperty("dubbo.application.qos-enable", "false");
        ApplicationConfig application =
                new ApplicationConfig("astsight-spring-dubbo-sample");
        application.setQosEnable(false);

        reference = new ReferenceConfig<OrderApi>();
        reference.setApplication(application);
        reference.setInterface(OrderApi.class);
        reference.setUrl(url);
        reference.setGroup("order");
        reference.setVersion("1.0.0");
        reference.setCheck(false);
        reference.setLazy(Boolean.TRUE);
        reference.setTimeout(3000);
        reference.setRetries(0);
        orderApi = reference.get();
    }

    public OrderView create(String id) {
        return orderApi.create(id);
    }

    @PreDestroy
    void destroy() {
        if (reference != null) {
            reference.destroy();
        }
    }
}
