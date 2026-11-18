package io.astsight.sidecar.dubbo;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.PrintStream;
import java.io.Reader;
import java.util.Properties;

/**
 * stdin/stdout JSONL 协议。一行一个请求，一行一个响应；stdout 只写协议，
 * 第三方库日志一律重定向到 stderr（见 {@link SidecarMain}）。
 */
final class SidecarServer {

    static final int PROTOCOL = 1;

    private final ObjectMapper mapper = new ObjectMapper();
    private final DubboCaller caller;

    SidecarServer(DubboCaller caller) {
        this.caller = caller;
    }

    /** @return 是否收到 {@code shutdown} */
    boolean run(Reader reader, PrintStream out) throws IOException {
        BufferedReader lines = new BufferedReader(reader);
        String line;
        while ((line = lines.readLine()) != null) {
            if (line.trim().isEmpty()) {
                continue;
            }
            boolean stop = false;
            ObjectNode response;
            String id = null;
            try {
                JsonNode request = mapper.readTree(line);
                if (request == null || !request.isObject()) {
                    throw new RequestException("bad_request", "请求必须是 JSON 对象");
                }
                id = text(request, "id");
                String op = text(request, "op");
                if (op == null || op.isEmpty()) {
                    throw new RequestException("bad_request", "缺少 op");
                }
                if ("ping".equals(op)) {
                    response = ping(id);
                } else if ("shutdown".equals(op)) {
                    response = base(id);
                    response.put("ok", true);
                    stop = true;
                } else if ("invoke".equals(op)) {
                    response = invoke(id, request);
                } else {
                    throw new RequestException("bad_request", "未知 op: " + op);
                }
            } catch (JsonProcessingException failure) {
                response = error(id, "bad_json", "请求不是合法 JSON：" + failure.getOriginalMessage());
            } catch (RequestException failure) {
                response = error(id, failure.code, message(failure));
            } catch (Throwable failure) {
                response = error(
                        id,
                        "internal_error",
                        failure.getClass().getName() + ": " + message(failure));
            }
            out.println(mapper.writeValueAsString(response));
            out.flush();
            if (stop) {
                return true;
            }
        }
        return false;
    }

    private ObjectNode invoke(String id, JsonNode request) throws RequestException {
        InvokeRequest parsed = InvokeRequest.parse(request);
        InvokeOutcome outcome = caller.invoke(parsed);
        ObjectNode response = base(id);
        response.put("ok", outcome.ok);
        response.put("rpc_status", outcome.rpcStatus == null ? "OK" : outcome.rpcStatus);
        response.put("elapsed_ms", outcome.elapsedMs);
        if (outcome.ok) {
            response.set("value", mapper.valueToTree(outcome.value));
        } else if (outcome.error != null) {
            response.put("error", outcome.error);
        }
        if (outcome.exceptionType != null) {
            response.put("exception_type", outcome.exceptionType);
        }
        if (outcome.message != null) {
            response.put("message", outcome.message);
        }
        return response;
    }

    private ObjectNode ping(String id) {
        ObjectNode response = base(id);
        response.put("ok", true);
        response.put("protocol", PROTOCOL);
        response.put("sidecar_version", SidecarMain.version());
        response.put("dubbo_version", dubboVersion());
        response.put("java_version", System.getProperty("java.version", ""));
        return response;
    }

    private ObjectNode base(String id) {
        ObjectNode response = mapper.createObjectNode();
        if (id != null) {
            response.put("id", id);
        }
        return response;
    }

    private ObjectNode error(
            String id, String code, String message) {
        ObjectNode response = base(id);
        response.put("ok", false);
        response.put("error", code);
        response.put("rpc_status", "BAD_REQUEST");
        response.put("message", message);
        return response;
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.get(field);
        return value == null || value.isNull() ? null : value.asText();
    }

    private static String message(Throwable failure) {
        String value = failure.getMessage();
        return value == null ? failure.getClass().getName() : value;
    }

    private static String dubboVersion() {
        // shaded jar 会覆盖 Dubbo 自己的 MANIFEST Implementation-Version，
        // Version.getVersion() 会误读成 sidecar 版本；优先读构建期过滤的
        // 资源文件。
        try (InputStream stream = SidecarServer.class.getResourceAsStream(
                "/dubbo-version.properties")) {
            if (stream != null) {
                Properties properties = new Properties();
                properties.load(stream);
                String version = properties.getProperty("version", "").trim();
                if (!version.isEmpty() && !version.contains("${")) {
                    return version;
                }
            }
        } catch (Throwable ignored) {
            // 回退到 Dubbo 自己的版本读取。
        }
        try {
            return org.apache.dubbo.common.Version.getVersion();
        } catch (Throwable ignored) {
            return "";
        }
    }
}
