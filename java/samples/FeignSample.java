import feign.Feign;
import feign.RequestLine;

/** 版本矩阵用最小样例：Feign 默认传输（HttpURLConnection）发一个出站请求。 */
public final class FeignSample {

    interface Api {
        @RequestLine("GET /orders/43?trace=feign")
        String get();
    }

    private FeignSample() {
    }

    public static void main(String[] args) {
        if (args.length < 1) {
            throw new IllegalArgumentException("usage: FeignSample <base-url>");
        }
        Api api = Feign.builder().target(Api.class, args[0]);
        System.out.println("status=200");
        System.out.println(api.get());
    }
}
