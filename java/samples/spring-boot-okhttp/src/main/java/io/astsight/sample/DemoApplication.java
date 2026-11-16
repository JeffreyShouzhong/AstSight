package io.astsight.sample;

import feign.Feign;
import feign.Param;
import feign.RequestLine;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.RestTemplate;

/** Spring Boot fat jar 场景：同一进程内覆盖 OkHttp / Feign / RestTemplate。 */
@SpringBootApplication
@RestController
public class DemoApplication {

    private final OkHttpClient client = new OkHttpClient();
    private final RestTemplate restTemplate = new RestTemplate();
    private final FeignApi feignApi =
            Feign.builder().target(FeignApi.class, "https://api.example.com");

    public static void main(String[] args) {
        SpringApplication.run(DemoApplication.class, args);
    }

    @GetMapping("/call")
    public String call() throws Exception {
        Request request = new Request.Builder()
                .url("https://api.example.com/orders/7?trace=boot")
                .build();
        Response response = client.newCall(request).execute();
        try {
            return response.code() + ":" + response.body().string();
        } finally {
            response.close();
        }
    }

    @GetMapping("/feign/{id}")
    public String feign(@PathVariable int id) {
        return feignApi.order(id);
    }

    @GetMapping("/rest/{id}")
    public String rest(@PathVariable int id) {
        return restTemplate.getForObject(
                "https://api.example.com/orders/{id}?trace=rest",
                String.class,
                id);
    }

    /** Feign 阻塞客户端；transport 可由应用换成 OkHttp/Apache HC，入口不变。 */
    public interface FeignApi {

        @RequestLine("GET /orders/{id}?trace=feign")
        String order(@Param("id") int id);
    }
}
