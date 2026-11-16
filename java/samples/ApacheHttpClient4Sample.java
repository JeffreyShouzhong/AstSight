/** 版本矩阵用最小样例：直连 Apache HttpClient 4 发一个请求。 */
public final class ApacheHttpClient4Sample {

    private ApacheHttpClient4Sample() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            throw new IllegalArgumentException(
                    "usage: ApacheHttpClient4Sample <url>");
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
    }
}
