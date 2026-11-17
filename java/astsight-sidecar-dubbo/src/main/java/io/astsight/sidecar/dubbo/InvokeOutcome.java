package io.astsight.sidecar.dubbo;

/** 一次泛化调用的结果；RPC 失败也走返回值，不向调用方抛异常。 */
final class InvokeOutcome {

    static final String OK = "OK";
    static final String BAD_REQUEST = "BAD_REQUEST";
    static final String TIMEOUT = "TIMEOUT";
    static final String SERIALIZATION = "SERIALIZATION";
    static final String NETWORK = "NETWORK";
    static final String FORBIDDEN = "FORBIDDEN";
    static final String NO_PROVIDER = "NO_PROVIDER";
    static final String RPC_EXCEPTION = "RPC_EXCEPTION";
    static final String INTERNAL_ERROR = "INTERNAL_ERROR";

    boolean ok;
    Object value;
    String error;
    String rpcStatus = OK;
    String exceptionType;
    String message;
    long elapsedMs;

    static InvokeOutcome success(Object value) {
        InvokeOutcome outcome = new InvokeOutcome();
        outcome.ok = true;
        outcome.value = value;
        return outcome;
    }

    static InvokeOutcome failure(String error, String status, String message) {
        InvokeOutcome outcome = new InvokeOutcome();
        outcome.error = error;
        outcome.rpcStatus = status;
        outcome.message = message;
        return outcome;
    }
}
