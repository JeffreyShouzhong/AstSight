package io.astsight.agent.http;

import io.astsight.agent.core.AgentModule;
import io.astsight.agent.core.AgentRuntime;

import java.lang.instrument.Instrumentation;

/** HttpClient 5 classic：织入 {@code InternalHttpClient#doExecute}。 */
public final class HttpClient5Module implements AgentModule {

    @Override
    public String id() {
        return "httpclient5";
    }

    @Override
    public void install(Instrumentation instrumentation, AgentRuntime runtime) {
        instrumentation.addTransformer(new HttpClient5Transformer(runtime), true);
        runtime.registerAdapter(id(), version());
    }

    private static String version() {
        try {
            Class<?> client =
                    Class.forName("org.apache.hc.client5.http.classic.HttpClient");
            Package pkg = client.getPackage();
            return pkg == null ? null : pkg.getImplementationVersion();
        } catch (Throwable ignored) {
            return null;
        }
    }
}
