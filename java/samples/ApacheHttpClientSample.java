/**
 * 直连 Apache HttpClient 4 与 5 的最小样例：同一个 JVM 内依次发两个请求，
 * 用于验证两个 adapter 共存。
 */
public final class ApacheHttpClientSample {

    private ApacheHttpClientSample() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            throw new IllegalArgumentException(
                    "usage: ApacheHttpClientSample <hc4-url> <hc5-url>");
        }

        try (org.apache.http.impl.client.CloseableHttpClient client =
                     org.apache.http.impl.client.HttpClients.createDefault();
             org.apache.http.client.methods.CloseableHttpResponse response =
                     client.execute(
                             new org.apache.http.client.methods.HttpGet(args[0]))) {
            System.out.println(
                    "hc4 status=" + response.getStatusLine().getStatusCode());
            System.out.println(
                    org.apache.http.util.EntityUtils.toString(response.getEntity()));
        }

        try (org.apache.hc.client5.http.impl.classic.CloseableHttpClient client =
                     org.apache.hc.client5.http.impl.classic.HttpClients.createDefault();
             org.apache.hc.client5.http.impl.classic.CloseableHttpResponse response =
                     client.execute(
                             new org.apache.hc.client5.http.classic.methods.HttpGet(args[1]))) {
            System.out.println("hc5 status=" + response.getCode());
            System.out.println(
                    org.apache.hc.core5.http.io.entity.EntityUtils.toString(
                            response.getEntity()));
        }
    }
}
