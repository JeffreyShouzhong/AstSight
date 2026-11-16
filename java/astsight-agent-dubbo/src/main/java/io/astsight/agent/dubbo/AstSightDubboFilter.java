package io.astsight.agent.dubbo;

import org.apache.dubbo.common.extension.Activate;
import org.apache.dubbo.rpc.Filter;
import org.apache.dubbo.rpc.Invocation;
import org.apache.dubbo.rpc.Invoker;
import org.apache.dubbo.rpc.Result;
import org.apache.dubbo.rpc.RpcException;

/**
 * 消费端 Dubbo Filter：命中会话绑定时直接返回桩对象，不发起真实调用。
 *
 * <p>这个类会被注入到被测应用的类加载器里（见 {@link DubboClassInjector}），
 * 因此只做最薄的一层适配：真正的取桩、JSON 转换与计数都在
 * {@link DubboBridge} 中完成，避免把 Jackson 等依赖牵进应用类加载器。</p>
 *
 * <p>取桩失败时抛 {@link RpcException}，绝不回落到真实 provider；未登记的
 * 下游（非计划内）才调用下一个 Invoker，放行真实调用。</p>
 */
@Activate(group = "consumer", order = -10000)
public final class AstSightDubboFilter implements Filter {

    public AstSightDubboFilter() {
        DubboBridge.filterInstantiated(getClass());
    }

    @Override
    public Result invoke(Invoker<?> invoker, Invocation invocation) throws RpcException {
        try {
            Object result = DubboBridge.intercept(invoker, invocation, getClass());
            if (result != null) {
                return (Result) result;
            }
        } catch (RpcException already) {
            throw already;
        } catch (Throwable failure) {
            String message = failure.getMessage();
            throw new RpcException(
                    "astSight:" + (message == null || message.isEmpty()
                            ? failure.getClass().getName()
                            : message));
        }
        return invoker.invoke(invocation);
    }
}
