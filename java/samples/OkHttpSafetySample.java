import java.io.IOException;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/** HTTP agent 安全矩阵样例：连接复用的多次调用与重定向失败关闭。 */
public final class OkHttpSafetySample {

    private OkHttpSafetySample() {
    }

    public static void main(String[] args) throws Exception {
        String url = args[0];
        String mode = args.length > 1 ? args[1] : "multi";
        int count = args.length > 2 ? Integer.parseInt(args[2]) : 3;
        OkHttpClient client = new OkHttpClient();
        if ("multi".equals(mode)) {
            multi(client, url, count);
        } else if ("redirect".equals(mode)) {
            single(client, url);
        } else {
            throw new IllegalArgumentException("unknown mode: " + mode);
        }
    }

    private static void multi(
            OkHttpClient client,
            String url,
            int count
    ) throws IOException {
        int success = 0;
        for (int index = 0; index < count; index++) {
            Request request = new Request.Builder().url(url).build();
            try (Response response = client.newCall(request).execute()) {
                if (response.code() == 200) {
                    success++;
                }
            }
        }
        System.out.println("success=" + success);
    }

    private static void single(OkHttpClient client, String url)
            throws IOException {
        Request request = new Request.Builder().url(url).build();
        try (Response response = client.newCall(request).execute()) {
            System.out.println("status=" + response.code());
        }
    }
}
