import org.apache.dubbo.common.URL;
import org.apache.dubbo.common.extension.ExtensionLoader;
import org.apache.dubbo.rpc.AsyncRpcResult;
import org.apache.dubbo.rpc.Filter;
import org.apache.dubbo.rpc.Invocation;
import org.apache.dubbo.rpc.Invoker;
import org.apache.dubbo.rpc.Result;
import org.apache.dubbo.rpc.RpcException;
import org.apache.dubbo.rpc.RpcInvocation;

import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Dubbo 消费端 Filter 的最小联调示例：不启动注册中心与 provider，直接
 * 经 Dubbo SPI 取激活 Filter，再对一个只计数的假 Invoker 发起调用。
 *
 * <p>参数：`stub`（默认）/ `unbound` / `unplanned`。成功打印 `result=`，
 * 失败打印 `error=`；真实调用发生时会打印 `REAL_CALL`。</p>
 */
interface OrderService {
    DubboFilterSample.Order create(String id);
}

public final class DubboFilterSample {

    public static final class Order {
        private String orderId;
        private int amount;

        public Order() {
        }

        public Order(String orderId, int amount) {
            this.orderId = orderId;
            this.amount = amount;
        }

        public String getOrderId() {
            return orderId;
        }

        public void setOrderId(String orderId) {
            this.orderId = orderId;
        }

        public int getAmount() {
            return amount;
        }

        public void setAmount(int amount) {
            this.amount = amount;
        }
    }

    private static final AtomicInteger REAL_CALLS = new AtomicInteger();

    public static void main(String[] args) throws Exception {
        String mode = args.length > 0 ? args[0] : "stub";
        // unplanned 用不同 group 生成计划外 dep，验证真实调用仍可放行。
        String group = "unplanned".equals(mode) ? "other" : "order";
        final URL url = URL.valueOf(
                "dubbo://127.0.0.1:20880/" + OrderService.class.getName()
                        + "?group=" + group + "&version=1.0.0");
        Invoker<OrderService> invoker = new Invoker<OrderService>() {
            @Override
            public Class<OrderService> getInterface() {
                return OrderService.class;
            }

            @Override
            public URL getUrl() {
                return url;
            }

            @Override
            public boolean isAvailable() {
                return true;
            }

            @Override
            public void destroy() {
            }

            @Override
            public Result invoke(Invocation invocation) throws RpcException {
                REAL_CALLS.incrementAndGet();
                System.out.println("REAL_CALL");
                return AsyncRpcResult.newDefaultAsyncResult(new Order("REAL", 0), invocation);
            }
        };

        RpcInvocation invocation = new RpcInvocation(
                "create",
                OrderService.class.getName(),
                "Ljava/lang/String;",
                new Class<?>[]{String.class},
                new Object[]{"A-1"});

        List<Filter> filters = ExtensionLoader.getExtensionLoader(Filter.class)
                .getActivateExtension(url, "reference.filter");
        Filter target = null;
        for (Filter filter : filters) {
            if ("io.astsight.agent.dubbo.AstSightDubboFilter"
                    .equals(filter.getClass().getName())) {
                target = filter;
            }
        }
        System.out.println("filter=" + (target == null ? "none" : target.getClass().getName()));
        if (target == null) {
            System.exit(2);
        }

        try {
            Result result = target.invoke(invoker, invocation);
            Object value = result.getValue();
            if (value instanceof Order) {
                Order order = (Order) value;
                System.out.println("result=" + order.getOrderId() + "," + order.getAmount());
            } else {
                System.out.println("result=" + value);
            }
        } catch (RpcException failure) {
            System.out.println("error=" + failure.getMessage());
        }
        System.out.println("real=" + REAL_CALLS.get());
    }
}
