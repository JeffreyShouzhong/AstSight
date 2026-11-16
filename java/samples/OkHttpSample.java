import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/** P0 spike 的最小 SUT：用 OkHttp 发一个出站请求并打印响应。 */
public final class OkHttpSample {

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            throw new IllegalArgumentException("usage: OkHttpSample <url>");
        }
        OkHttpClient client = new OkHttpClient();
        Request request = new Request.Builder().url(args[0]).get().build();
        Response response = client.newCall(request).execute();
        try {
            System.out.println("status=" + response.code());
            System.out.println(response.body().string());
        } finally {
            response.close();
        }
        if (args.length > 1) {
            // 可选停留，便于观察 agent 周期心跳；不影响请求本身。
            Thread.sleep(Long.parseLong(args[1]));
        }
    }
}
