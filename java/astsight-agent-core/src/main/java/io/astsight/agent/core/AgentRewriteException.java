package io.astsight.agent.core;

/**
 * 计划内下游命中但改写失败时抛出。adapter 必须让本次调用失败，而不是
 * 返回原始请求把流量放行到真实下游。
 */
public final class AgentRewriteException extends RuntimeException {

    public AgentRewriteException(String message) {
        super(message);
    }

    public AgentRewriteException(String message, Throwable cause) {
        super(message, cause);
    }
}
