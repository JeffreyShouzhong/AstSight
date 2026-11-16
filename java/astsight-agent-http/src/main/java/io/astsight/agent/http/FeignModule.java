package io.astsight.agent.http;

import io.astsight.agent.core.AgentModule;
import io.astsight.agent.core.AgentRuntime;

import java.lang.instrument.Instrumentation;

/** Feign 同步客户端（默认 / OkHttp / Apache HC 等传输都经过同一入口）。 */
public final class FeignModule implements AgentModule {

    @Override
    public String id() {
        return "feign";
    }

    @Override
    public void install(Instrumentation instrumentation, AgentRuntime runtime) {
        instrumentation.addTransformer(new FeignTransformer(runtime), true);
        runtime.registerAdapter(id(), version());
    }

    private static String version() {
        try {
            Class<?> request = Class.forName("feign.Request");
            Package pkg = request.getPackage();
            return pkg == null ? null : pkg.getImplementationVersion();
        } catch (Throwable ignored) {
            return null;
        }
    }
}
