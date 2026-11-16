/** 版本矩阵用最小样例：直连 Apache HttpClient 5 classic 发一个请求。 */
public final class ApacheHttpClient5Sample {

    private ApacheHttpClient5Sample() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            throw new IllegalArgumentException(
                    "usage: ApacheHttpClient5Sample <url>");
        }
        try (org.apache.hc.client5.http.impl.classic.CloseableHttpClient client =
                     org.apache.hc.client5.http.impl.classic.HttpClients.createDefault();
             org.apache.hc.client5.http.impl.classic.CloseableHttpResponse response =
                     client.execute(
                             new org.apache.hc.client5.http.classic.methods.HttpGet(args[0]))) {
            System.out.println("hc5 status=" + response.getCode());
            System.out.println(
                    org.apache.hc.core5.http.io.entity.EntityUtils.toString(
                            response.getEntity()));
        }
    }
}
