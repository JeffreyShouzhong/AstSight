package io.astsight.sample.consumer;

import org.apache.dubbo.config.ApplicationConfig;
import org.apache.dubbo.config.ReferenceConfig;
import org.apache.dubbo.rpc.RpcException;

/**
 * 真实 Dubbo 消费端最小样例：直连 URL、不启注册中心与 provider。
 *
 * <p>调用经 {@link ReferenceConfig} 生成的真实代理进入 Filter 链。命中桩时
 * Filter 短路返回，不建连、不发请求；未命中时 {@code lazy=true} 才会真正
 * 去连目标端口。输出：`result=`、`error=`、`loader=`（应用类加载器）、
 * `filter_loader=`（Filter 所在类加载器）。</p>
 *
 * <p>参数：`--host=127.0.0.1`、`--port=20880`。</p>
 */
public final class DubboConsumerSample {

    private static final String FILTER_CLASS =
            "io.astsight.agent.dubbo.AstSightDubboFilter";

    public static void main(String[] args) {
        String host = "127.0.0.1";
        int port = 20880;
        for (String arg : args) {
            int index = arg.indexOf('=');
            if (index < 0) {
                continue;
            }
            String key = arg.substring(0, index);
            String value = arg.substring(index + 1);
            if ("--host".equals(key)) {
                host = value;
            } else if ("--port".equals(key)) {
                port = Integer.parseInt(value);
            }
        }

        // 不启动 QoS 端口；lazy=true 让未命中 Mock 时才真正建连。
        System.setProperty("dubbo.application.qos-enable", "false");
        ApplicationConfig application =
                new ApplicationConfig("astsight-dubbo-consumer-sample");
        application.setQosEnable(false);

        ReferenceConfig<OrderApi> reference = new ReferenceConfig<OrderApi>();
        reference.setApplication(application);
        reference.setInterface(OrderApi.class);
        reference.setUrl("dubbo://" + host + ":" + port + "?lazy=true");
        reference.setGroup("order");
        reference.setVersion("1.0.0");
        reference.setCheck(false);
        reference.setLazy(Boolean.TRUE);
        reference.setTimeout(3000);
        reference.setRetries(0);

        try {
            OrderApi api = reference.get();
            System.out.println("loader=" + loaderName(DubboConsumerSample.class));
            OrderView view = api.create("A-1");
            System.out.println("result=" + view.getOrderId() + "," + view.getAmount());
            System.out.println("filter_loader=" + filterLoaderName());
        } catch (RpcException failure) {
            System.out.println("error=" + failure.getMessage());
        } finally {
            reference.destroy();
        }
    }

    private static String loaderName(Class<?> type) {
        ClassLoader loader = type.getClassLoader();
        return loader == null ? "bootstrap" : loader.getClass().getName();
    }

    /**
     * Filter 由 agent 定义进应用类加载器；这里用同一个加载器查一次，确认不是
     * 从父加载器（agent jar）里拿到的副本。
     */
    private static String filterLoaderName() {
        try {
            ClassLoader loader = Thread.currentThread().getContextClassLoader();
            Class<?> filter = Class.forName(FILTER_CLASS, false, loader);
            return loaderName(filter);
        } catch (Throwable failure) {
            return "missing:" + failure;
        }
    }
}
