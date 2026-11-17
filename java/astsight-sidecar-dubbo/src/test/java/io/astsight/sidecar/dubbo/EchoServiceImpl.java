package io.astsight.sidecar.dubbo;

import org.apache.dubbo.rpc.RpcContext;

public final class EchoServiceImpl implements EchoService {

    @Override
    public String echo(String value) {
        return "echo:" + value;
    }

    @Override
    public int add(int left, int right) {
        return left + right;
    }

    @Override
    public EchoReply describe(EchoRequest request) {
        return new EchoReply(
                "hello " + request.getName(),
                RpcContext.getContext().getAttachment("tenant"));
    }

    @Override
    public String birthday(EchoRequest request) {
        return String.valueOf(request.getBirthday());
    }

    @Override
    public String attachment(String key) {
        return RpcContext.getContext().getAttachment(key);
    }

    @Override
    public String slow(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            return "interrupted";
        }
        return "slept";
    }
}
