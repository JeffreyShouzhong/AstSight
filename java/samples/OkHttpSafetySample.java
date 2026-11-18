import java.io.IOException;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.CertificatePinner;
import okhttp3.Interceptor;

/** HTTP agent 安全矩阵样例：连接复用、异步/重试、TLS/scheme 与签名边界。 */
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
        } else if ("async".equals(mode)) {
            async(client, url, count);
        } else if ("retry".equals(mode)) {
            retry(client, url);
        } else if ("pinning".equals(mode)) {
            pinning(client, url);
        } else if ("scheme_guard".equals(mode)) {
            schemeGuard(client, url);
        } else if ("signature".equals(mode)) {
            signature(client, url);
        } else if ("direct".equals(mode)) {
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
        single(client, request);
    }

    private static void single(OkHttpClient client, Request request)
            throws IOException {
        try (Response response = client.newCall(request).execute()) {
            System.out.println("status=" + response.code());
        }
    }

    private static void async(
            OkHttpClient client,
            String url,
            int count
    ) throws IOException, InterruptedException {
        CountDownLatch done = new CountDownLatch(count);
        AtomicInteger success = new AtomicInteger();
        for (int index = 0; index < count; index++) {
            Request request = new Request.Builder().url(url).build();
            client.newCall(request).enqueue(new okhttp3.Callback() {
                @Override
                public void onFailure(okhttp3.Call call, IOException failure) {
                    done.countDown();
                }

                @Override
                public void onResponse(okhttp3.Call call, Response response)
                        throws IOException {
                    try (Response ignored = response) {
                        if (response.code() == 200) {
                            success.incrementAndGet();
                        }
                    } finally {
                        done.countDown();
                    }
                }
            });
        }
        if (!done.await(15, TimeUnit.SECONDS)) {
            throw new IOException("async timeout");
        }
        System.out.println("success=" + success.get());
        client.dispatcher().executorService().shutdown();
        if (!client.dispatcher().executorService().awaitTermination(
                5,
                TimeUnit.SECONDS
        )) {
            throw new IOException("async executor shutdown timeout");
        }
    }

    private static void retry(OkHttpClient base, String url)
            throws IOException {
        OkHttpClient client = base.newBuilder()
                .addInterceptor(new Interceptor() {
                    @Override
                    public Response intercept(Chain chain) throws IOException {
                        Response first = chain.proceed(chain.request());
                        first.close();
                        return chain.proceed(chain.request());
                    }
                })
                .build();
        single(client, url);
    }

    private static void pinning(OkHttpClient base, String url)
            throws IOException {
        OkHttpClient client = base.newBuilder()
                .certificatePinner(
                        new CertificatePinner.Builder()
                                .add(
                                        "127.0.0.1",
                                        "sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
                                )
                                .build()
                )
                .build();
        single(client, url);
    }

    private static void schemeGuard(OkHttpClient base, String url)
            throws IOException {
        OkHttpClient client = base.newBuilder()
                .addInterceptor(new Interceptor() {
                    @Override
                    public Response intercept(Chain chain) throws IOException {
                        if (!"https".equals(chain.request().url().scheme())) {
                            throw new IOException("https_required");
                        }
                        return chain.proceed(chain.request());
                    }
                })
                .build();
        single(client, url);
    }

    private static void signature(OkHttpClient base, String url)
            throws IOException {
        OkHttpClient client = base.newBuilder()
                .addInterceptor(new Interceptor() {
                    @Override
                    public Response intercept(Chain chain) throws IOException {
                        Request request = chain.request();
                        String canonical = canonical(request);
                        return chain.proceed(
                                request.newBuilder()
                                        .header(
                                                "X-Test-Canonical",
                                                canonical
                                        )
                                        .build()
                        );
                    }
                })
                .build();
        Request request = new Request.Builder()
                .url(url)
                .header("Host", "downstream.example.test")
                .header("X-Signature-Precomputed", "signed-before-agent")
                .build();
        single(client, request);
    }

    private static String canonical(Request request) {
        String query = request.url().encodedQuery();
        return request.method()
                + "|" + request.url().scheme()
                + "|" + request.url().host()
                + "|" + request.url().port()
                + "|" + request.url().encodedPath()
                + "|" + (query == null ? "" : query);
    }
}
