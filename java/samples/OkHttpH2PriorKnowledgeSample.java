import okhttp3.OkHttpClient;
import okhttp3.Protocol;
import okhttp3.Request;
import okhttp3.Response;

import java.util.Collections;
import java.util.concurrent.TimeUnit;

/**
 * HTTP/2 prior-knowledge（h2c）负例：OkHttp 只允许明文 HTTP/2，直接发
 * connection preface，不做 HTTP/1.1 升级。Mock 只支持 HTTP/1.1，因此改写后的
 * 请求必然失败；本示例把失败打印出来并以非 0 退出，供 e2e 断言“显式失败、
 * 零透传”。
 */
public final class OkHttpH2PriorKnowledgeSample {

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            throw new IllegalArgumentException("usage: OkHttpH2PriorKnowledgeSample <url>");
        }
        OkHttpClient client = new OkHttpClient.Builder()
                .protocols(Collections.singletonList(Protocol.H2_PRIOR_KNOWLEDGE))
                .connectTimeout(3, TimeUnit.SECONDS)
                .readTimeout(3, TimeUnit.SECONDS)
                .writeTimeout(3, TimeUnit.SECONDS)
                .build();
        Request request = new Request.Builder().url(args[0]).get().build();
        try (Response response = client.newCall(request).execute()) {
            System.out.println("status=" + response.code());
            System.out.println(response.body().string());
        } catch (Exception failure) {
            System.out.println("failed=" + failure.getClass().getName());
            throw failure;
        }
    }
}
