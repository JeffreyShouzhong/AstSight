package io.astsight.agent.http;

import io.astsight.agent.core.AgentModule;
import io.astsight.agent.core.AgentRuntime;

import java.lang.instrument.Instrumentation;

/** OkHttp 3/4/5：织入 {@code OkHttpClient#newCall(Request)}。 */
public final class OkHttpModule implements AgentModule {

    @Override
    public String id() {
        return "okhttp";
    }

    @Override
    public void install(Instrumentation instrumentation, AgentRuntime runtime) {
        instrumentation.addTransformer(new OkHttpTransformer(runtime), true);
        runtime.registerAdapter(id(), version());
    }

    private static String version() {
        try {
            Class<?> client = Class.forName("okhttp3.OkHttpClient");
            Package pkg = client.getPackage();
            return pkg == null ? null : pkg.getImplementationVersion();
        } catch (Throwable ignored) {
            return null;
        }
    }
}
