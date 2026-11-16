package io.astsight.agent.core;

import java.lang.instrument.Instrumentation;

/**
 * 一个可独立加载的客户端栈适配器。实现通过 {@code META-INF/services} 注册，
 * 核心只依赖 JDK 类型，避免不同 HTTP 客户端版本的 API 冲突。
 */
public interface AgentModule {

    String id();

    void install(Instrumentation instrumentation, AgentRuntime runtime) throws Exception;
}
