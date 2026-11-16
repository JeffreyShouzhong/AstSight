package io.astsight.agent.core;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * {@code -javaagent:astsight-agent.jar=admin=...,token=...,sut_id=...}
 *
 * <p>也可以写 {@code token_env=ASTSIGHT_AGENT_TOKEN}，由环境变量提供 token，
 * 避免 token 出现在进程命令行与 {@code JAVA_TOOL_OPTIONS} 日志中。</p>
 */
public final class AgentOptions {

    private final String adminUrl;
    private final String token;
    private final String sutId;
    private final long heartbeatIntervalMs;
    private final long routeRefreshIntervalMs;

    private AgentOptions(
            String adminUrl,
            String token,
            String sutId,
            long heartbeatIntervalMs,
            long routeRefreshIntervalMs) {
        this.adminUrl = adminUrl;
        this.token = token;
        this.sutId = sutId;
        this.heartbeatIntervalMs = heartbeatIntervalMs;
        this.routeRefreshIntervalMs = routeRefreshIntervalMs;
    }

    public static AgentOptions parse(String args) {
        Map<String, String> values = new LinkedHashMap<String, String>();
        if (args != null) {
            for (String item : args.split(",")) {
                String part = item.trim();
                if (part.isEmpty()) {
                    continue;
                }
                int split = part.indexOf('=');
                if (split <= 0) {
                    continue;
                }
                values.put(part.substring(0, split).trim(), part.substring(split + 1).trim());
            }
        }
        String admin = require(values, "admin");
        String token = values.get("token");
        if (token == null || token.isEmpty()) {
            String tokenEnv = values.get("token_env");
            if (tokenEnv != null && !tokenEnv.isEmpty()) {
                token = System.getenv(tokenEnv);
            }
        }
        String sutId = require(values, "sut_id");
        if (token == null || token.isEmpty()) {
            throw new IllegalArgumentException(
                    "missing agent option: token (or token_env)");
        }
        long heartbeat = parseLong(values.get("heartbeat_interval_ms"), 5000L, 500L);
        long refresh = parseLong(values.get("route_refresh_interval_ms"), 5000L, 500L);
        if (admin.endsWith("/")) {
            admin = admin.substring(0, admin.length() - 1);
        }
        return new AgentOptions(admin, token, sutId, heartbeat, refresh);
    }

    private static String require(Map<String, String> values, String key) {
        String value = values.get(key);
        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException("missing agent option: " + key);
        }
        return value;
    }

    private static long parseLong(String raw, long fallback, long minimum) {
        if (raw == null || raw.isEmpty()) {
            return fallback;
        }
        try {
            long value = Long.parseLong(raw);
            return value < minimum ? fallback : value;
        } catch (NumberFormatException ignored) {
            return fallback;
        }
    }

    public String adminUrl() {
        return adminUrl;
    }

    public String token() {
        return token;
    }

    public String sutId() {
        return sutId;
    }

    public long heartbeatIntervalMs() {
        return heartbeatIntervalMs;
    }

    public long routeRefreshIntervalMs() {
        return routeRefreshIntervalMs;
    }
}
