package io.astsight.sidecar.dubbo;

/** 请求字段不合法；`code` 直接进入响应 `error`，不归到 RPC 异常。 */
final class RequestException extends Exception {

    final String code;

    RequestException(String code, String message) {
        super(message);
        this.code = code;
    }
}
