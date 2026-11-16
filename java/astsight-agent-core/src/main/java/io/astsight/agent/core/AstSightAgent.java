package io.astsight.agent.core;

import java.lang.instrument.Instrumentation;
import java.util.ServiceLoader;

/** Java agent 入口；premain 失败只记录，不阻断 SUT 启动。 */
public final class AstSightAgent {

    private static volatile AgentRuntime runtime;

    private AstSightAgent() {
    }

    public static void premain(String args, Instrumentation instrumentation) {
        try {
            AgentOptions options = AgentOptions.parse(args);
            AgentRuntime created = new AgentRuntime(options);
            runtime = created;
            created.refreshRoutes();
            for (AgentModule module : ServiceLoader.load(
                    AgentModule.class, AstSightAgent.class.getClassLoader())) {
                try {
                    module.install(instrumentation, created);
                } catch (Throwable failure) {
                    created.markStackUnsupported(module.id());
                    System.err.println(
                            "[astsight-agent] adapter " + module.id() + " failed: " + failure);
                }
            }
            created.startBackground();
            System.err.println(
                    "[astsight-agent] started sut_id="
                            + options.sutId()
                            + " adapters="
                            + created.adapterIds());
        } catch (Throwable failure) {
            System.err.println("[astsight-agent] disabled: " + failure);
        }
    }

    public static AgentRuntime runtime() {
        return runtime;
    }
}
