package io.astsight.agent.core;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** 只读 admin 客户端：拉取路由表、上报心跳；不写会话、不改计划。 */
final class AdminClient {

    private final String baseUrl;
    private final String token;

    AdminClient(String baseUrl, String token) {
        this.baseUrl = baseUrl;
        this.token = token;
    }

    String fetchRoutes() throws IOException {
        return request("GET", "/_admin/agent/routes", null);
    }

    void sendHeartbeat(String json) throws IOException {
        request("POST", "/_admin/agent/heartbeat", json);
    }

    String publishMq(String json) throws IOException {
        return request("POST", "/_admin/mq/publish", json);
    }

    private String request(String method, String path, String body) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(baseUrl + path).openConnection();
        try {
            connection.setRequestMethod(method);
            connection.setConnectTimeout(2000);
            connection.setReadTimeout(3000);
            connection.setRequestProperty("Authorization", "Bearer " + token);
            connection.setRequestProperty("Accept", "application/json");
            if (body != null) {
                byte[] payload = body.getBytes(StandardCharsets.UTF_8);
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json");
                connection.setFixedLengthStreamingMode(payload.length);
                OutputStream out = connection.getOutputStream();
                try {
                    out.write(payload);
                } finally {
                    out.close();
                }
            }
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                throw new IOException("admin " + path + " status=" + status);
            }
            InputStream in = connection.getInputStream();
            try {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buffer = new byte[4096];
                int read;
                while ((read = in.read(buffer)) != -1) {
                    out.write(buffer, 0, read);
                }
                return new String(out.toByteArray(), StandardCharsets.UTF_8);
            } finally {
                in.close();
            }
        } finally {
            connection.disconnect();
        }
    }
}
