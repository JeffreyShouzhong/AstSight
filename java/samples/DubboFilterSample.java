import org.apache.dubbo.common.URL;
import org.apache.dubbo.common.extension.ExtensionLoader;
import org.apache.dubbo.rpc.AsyncRpcResult;
import org.apache.dubbo.rpc.Filter;
import org.apache.dubbo.rpc.Invocation;
import org.apache.dubbo.rpc.Invoker;
import org.apache.dubbo.rpc.Result;
import org.apache.dubbo.rpc.RpcContext;
import org.apache.dubbo.rpc.RpcException;
import org.apache.dubbo.rpc.RpcInvocation;
import org.apache.dubbo.rpc.service.GenericService;

import java.lang.reflect.Method;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Dubbo 消费端 Filter 的最小联调示例：不启动注册中心与 provider，直接
 * 经 Dubbo SPI 取激活 Filter，再对一个只计数的假 Invoker 发起调用。
 *
 * <p>参数：`stub`（默认）/ `unbound` / `unplanned` / `async` / `future` /
 * `oneway` / `generic` / `genericAsync`。成功打印 `result=`、`async=`、
 * `future=`、`oneway=` 或 `generic=`，失败打印 `error=`；真实调用发生时会
 * 打印 `REAL_CALL`。</p>
 */
interface OrderService {
    DubboFilterSample.Order create(String id);

    CompletableFuture<DubboFilterSample.Order> createAsync(String id);

    void ping(String id);
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
        try {
            if ("async".equals(mode)) {
                runAsync();
            } else if ("future".equals(mode)) {
                runFuture();
            } else if ("oneway".equals(mode)) {
                runOneway();
            } else if ("generic".equals(mode) || "genericAsync".equals(mode)) {
                runGeneric("genericAsync".equals(mode));
            } else {
                runSync(mode);
            }
        } catch (RpcException failure) {
            System.out.println("error=" + failure.getMessage());
        }
        System.out.println("real=" + REAL_CALLS.get());
    }

    private static void runSync(String mode) throws Exception {
        // unplanned 用不同 group 生成计划外 dep，验证真实调用仍可放行。
        String group = "unplanned".equals(mode) ? "other" : "order";
        URL url = urlFor(OrderService.class, group);
        RpcInvocation invocation = new RpcInvocation(
                "create",
                OrderService.class.getName(),
                "Ljava/lang/String;",
                new Class<?>[]{String.class},
                new Object[]{"A-1"});
        Result result = invoke(url, OrderService.class, invocation);
        System.out.println("result=" + describe(result.getValue()));
    }

    private static void runAsync() throws Exception {
        URL url = urlFor(OrderService.class, "order");
        RpcInvocation invocation = new RpcInvocation(
                "create",
                OrderService.class.getName(),
                "Ljava/lang/String;",
                new Class<?>[]{String.class},
                new Object[]{"A-1"});
        invocation.setAttachment("async", "true");
        Result result = invoke(url, OrderService.class, invocation);
        // 老式 async：调用方从 RpcContext 拿 future。
        Future<?> future = RpcContext.getContext().getFuture();
        System.out.println("async=" + describe(future == null ? null : future.get()));
        System.out.println("async-result=" + describe(result.getValue()));
    }

    private static void runFuture() throws Exception {
        URL url = urlFor(OrderService.class, "order");
        Method method = OrderService.class.getMethod("createAsync", String.class);
        RpcInvocation invocation = new RpcInvocation(
                method,
                OrderService.class.getName(),
                OrderService.class.getName(),
                new Object[]{"A-1"});
        Result result = invoke(url, OrderService.class, invocation);
        // 声明返回 CompletableFuture 时，代理层直接返回 future。
        Object recreated = recreate(result);
        if (recreated instanceof CompletableFuture) {
            CompletableFuture<?> future = (CompletableFuture<?>) recreated;
            System.out.println("future=" + describe(future.get()));
        } else {
            System.out.println("future=<not-future:" + recreated + ">");
        }
    }

    private static void runOneway() throws Exception {
        URL url = urlFor(OrderService.class, "order");
        RpcInvocation invocation = new RpcInvocation(
                "ping",
                OrderService.class.getName(),
                "Ljava/lang/String;",
                new Class<?>[]{String.class},
                new Object[]{"A-1"});
        invocation.setAttachment("return", "false");
        Result result = invoke(url, OrderService.class, invocation);
        Object value = result.getValue();
        System.out.println("oneway=" + (value == null ? "empty" : String.valueOf(value)));
    }

    private static void runGeneric(boolean async) throws Exception {
        URL url = urlFor(GenericService.class, "order");
        String method = async ? "$invokeAsync" : "$invoke";
        RpcInvocation invocation = new RpcInvocation(
                method,
                GenericService.class.getName(),
                "Ljava/lang/String;Ljava/lang/String;[Ljava/lang/String;[Ljava/lang/Object;",
                new Class<?>[]{String.class, String.class, String[].class, Object[].class},
                new Object[]{
                        OrderService.class.getName(),
                        "create",
                        new String[]{"java.lang.String"},
                        new Object[]{"A-1"}});
        Result result = invoke(url, GenericService.class, invocation);
        Object value = result.getValue();
        if (async) {
            Object recreated = recreate(result);
            value = recreated instanceof CompletableFuture
                    ? ((CompletableFuture<?>) recreated).get()
                    : recreated;
        }
        Object orderId = value instanceof Map ? ((Map<?, ?>) value).get("orderId") : value;
        System.out.println("generic=" + orderId);
    }

    private static Object recreate(Result result) {
        try {
            return result.recreate();
        } catch (RuntimeException failure) {
            throw failure;
        } catch (Throwable failure) {
            throw new IllegalStateException(failure);
        }
    }

    private static Result invoke(
            URL url, Class<?> iface, RpcInvocation invocation) {
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
        return target.invoke(new CountingInvoker(iface, url), invocation);
    }

    private static URL urlFor(Class<?> iface, String group) {
        return URL.valueOf(
                "dubbo://127.0.0.1:20880/" + iface.getName()
                        + "?group=" + group + "&version=1.0.0");
    }

    private static String describe(Object value) {
        if (value instanceof Order) {
            Order order = (Order) value;
            return order.getOrderId() + "," + order.getAmount();
        }
        return String.valueOf(value);
    }

    private static final class CountingInvoker implements Invoker<Object> {
        private final Class<?> iface;
        private final URL url;

        private CountingInvoker(Class<?> iface, URL url) {
            this.iface = iface;
            this.url = url;
        }

        @SuppressWarnings("unchecked")
        @Override
        public Class<Object> getInterface() {
            return (Class<Object>) iface;
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
    }
}
