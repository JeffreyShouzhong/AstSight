package io.astsight.sample.triple;

import org.apache.dubbo.common.constants.CommonConstants;
import org.apache.dubbo.config.ApplicationConfig;
import org.apache.dubbo.config.ProtocolConfig;
import org.apache.dubbo.config.RegistryConfig;
import org.apache.dubbo.config.ServiceConfig;
import org.apache.dubbo.config.bootstrap.DubboBootstrap;

/** 直连 Triple provider；不启动注册中心。 */
public final class TripleServerMain {

    private TripleServerMain() {
    }

    public static void main(String[] args) {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 50053;
        ServiceConfig<InventoryService> service = new ServiceConfig<>();
        service.setInterface(InventoryService.class);
        service.setRef(new InventoryServiceImpl());
        service.setGroup("inventory");
        service.setVersion("1.0.0");

        DubboBootstrap bootstrap = DubboBootstrap.getInstance();
        bootstrap.application(new ApplicationConfig("astsight-triple-sample"))
                .registry(new RegistryConfig("N/A"))
                .protocol(new ProtocolConfig(CommonConstants.TRIPLE, port))
                .service(service)
                .start()
                .await();
    }
}
