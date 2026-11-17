package io.astsight.sidecar.dubbo;

/** 测试用 provider 接口；sidecar 侧不依赖它，泛化调用只认全限定名。 */
public interface EchoService {

    String echo(String value);

    int add(int left, int right);

    EchoReply describe(EchoRequest request);

    String birthday(EchoRequest request);

    String attachment(String key);

    String slow(long millis);
}
