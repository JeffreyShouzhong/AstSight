package io.astsight.agent.http;

import io.astsight.agent.core.AgentModule;
import io.astsight.agent.core.AgentRuntime;

import java.lang.instrument.Instrumentation;

/** HttpClient 4.3+ classic：织入 {@code InternalHttpClient#doExecute}。 */
public final class HttpClient4Module implements AgentModule {

    @Override
    public String id() {
        return "httpclient4";
    }

    @Override
    public void install(Instrumentation instrumentation, AgentRuntime runtime) {
        instrumentation.addTransformer(new HttpClient4Transformer(runtime), true);
        runtime.registerAdapter(id(), version());
    }

    private static String version() {
        try {
            Class<?> client = Class.forName("org.apache.http.client.HttpClient");
            Package pkg = client.getPackage();
            return pkg == null ? null : pkg.getImplementationVersion();
        } catch (Throwable ignored) {
            return null;
        }
    }
}
