package io.astsight.agent.http;

import io.astsight.agent.core.AgentModule;
import io.astsight.agent.core.AgentRuntime;

import java.lang.instrument.Instrumentation;

/** Spring RestTemplate：统一织入 {@code doExecute}，与底层 RequestFactory 无关。 */
public final class RestTemplateModule implements AgentModule {

    @Override
    public String id() {
        return "resttemplate";
    }

    @Override
    public void install(Instrumentation instrumentation, AgentRuntime runtime) {
        instrumentation.addTransformer(new RestTemplateTransformer(runtime), true);
        runtime.registerAdapter(id(), version());
    }

    private static String version() {
        try {
            Class<?> template = Class.forName("org.springframework.web.client.RestTemplate");
            Package pkg = template.getPackage();
            return pkg == null ? null : pkg.getImplementationVersion();
        } catch (Throwable ignored) {
            return null;
        }
    }
}
