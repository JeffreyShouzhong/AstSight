package io.astsight.agent.dubbo;

import io.astsight.agent.core.AgentModule;
import io.astsight.agent.core.AgentRuntime;

import java.lang.instrument.Instrumentation;

/**
 * Dubbo 扩展：把消费端 Filter 注入应用类加载器，并接上 admin 取桩通道。
 *
 * <p>Dubbo 版本在 Filter 首次实例化时才可知（agent premain 阶段 Dubbo 还没
 * 加载），版本与激活计数都通过心跳上报。</p>
 */
public final class AstSightDubboModule implements AgentModule {

    @Override
    public String id() {
        return "dubbo";
    }

    @Override
    public void install(Instrumentation instrumentation, AgentRuntime runtime) {
        DubboBridge.configure(runtime);
        runtime.dubboModuleInstalled();
        new DubboClassInjector(instrumentation, runtime).install();
    }
}
