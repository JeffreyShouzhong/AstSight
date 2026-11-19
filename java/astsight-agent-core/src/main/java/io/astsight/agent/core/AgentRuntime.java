package io.astsight.agent.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.io.UnsupportedEncodingException;
import java.lang.management.ManagementFactory;
import java.net.URI;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Agent 运行时：持有路由快照、改写计数与心跳。Agent 不持有用例会话，
 * 会话/分支校验仍在 Mock 服务端完成。
 */
public final class AgentRuntime {

    private static final Set<String> EXCLUDED_PREFIXES = ConcurrentHashMap.newKeySet();

    static {
        EXCLUDED_PREFIXES.add("/_dep/");
        EXCLUDED_PREFIXES.add("/_agent/");
    }

    private final AgentOptions options;
    private final AdminClient admin;
    private final AtomicBoolean backgroundStarted = new AtomicBoolean(false);
    private final AtomicLong hooked = new AtomicLong();
    private final AtomicLong rewrites = new AtomicLong();
    private final AtomicLong errors = new AtomicLong();
    private final AtomicLong passthrough = new AtomicLong();
    private final Map<String, Long> passthroughHosts = new ConcurrentHashMap<String, Long>();
    private final Map<String, AdapterState> adapters = new ConcurrentHashMap<String, AdapterState>();
    private final Set<String> unsupported = ConcurrentHashMap.newKeySet();
    private final AtomicLong dubboFilterInstantiated = new AtomicLong();
    private final AtomicLong dubboFilterHits = new AtomicLong();
    private final AtomicLong dubboPassthrough = new AtomicLong();
    private final AtomicLong dubboErrors = new AtomicLong();
    private final AtomicLong mqPublished = new AtomicLong();
    private final AtomicLong mqSuppressed = new AtomicLong();
    private final AtomicLong mqErrors = new AtomicLong();
    private final Set<String> mqClients = ConcurrentHashMap.newKeySet();
    private volatile boolean mqInstalled;
    private volatile boolean dubboInstalled;
    private volatile String dubboVersion;
    private volatile RouteTableSnapshot routes;
    private volatile String lastError;

    AgentRuntime(AgentOptions options) {
        this.options = options;
        this.admin = new AdminClient(options.adminUrl(), options.token());
    }

    void refreshRoutes() {
        try {
            String payload = admin.fetchRoutes();
            routes = RouteTableSnapshot.parse(payload, options.adminUrl());
        } catch (Throwable failure) {
            note("route refresh failed: " + failure);
        }
    }

    public void registerAdapter(String id, String version) {
        registerAdapter(id, version, "http");
    }

    /**
     * 注册 adapter；`kind` 区分 HTTP 与 MQ，心跳按类别分别上报，
     * 避免 MQ producer 名称混进 `http_adapters`。
     */
    public void registerAdapter(String id, String version, String kind) {
        adapters.put(id, new AdapterState(id, version, true, kind));
    }

    void markStackUnsupported(String id) {
        unsupported.add(id);
        AdapterState current = adapters.get(id);
        if (current != null) {
            adapters.put(
                    id,
                    new AdapterState(
                            current.id, current.version, false, current.kind));
        }
    }

    public RewriteResult rewrite(String adapterId, String method, String rawUrl) {
        hooked.incrementAndGet();
        try {
            RouteTableSnapshot snapshot = routes;
            if (snapshot == null || rawUrl == null) {
                return RewriteResult.unchanged();
            }
            URI uri;
            try {
                uri = new URI(rawUrl);
            } catch (Exception invalid) {
                return RewriteResult.unchanged();
            }
            String scheme = uri.getScheme();
            if (scheme == null) {
                return RewriteResult.unchanged();
            }
            scheme = scheme.toLowerCase(Locale.ROOT);
            if (!"http".equals(scheme) && !"https".equals(scheme)) {
                return RewriteResult.unchanged();
            }
            String host = uri.getHost();
            if (host == null || host.isEmpty()) {
                return RewriteResult.unchanged();
            }
            String path = uri.getRawPath();
            if (path == null || path.isEmpty()) {
                path = "/";
            }
            for (String prefix : EXCLUDED_PREFIXES) {
                if (path.startsWith(prefix)) {
                    return RewriteResult.unchanged();
                }
            }
            int port = uri.getPort();
            if (isMockEndpoint(snapshot.endpoint(), host, port, scheme)) {
                return RewriteResult.unchanged();
            }
            String authority = host + (port == -1 ? "" : ":" + port);
            String query = uri.getRawQuery();
            RouteTableSnapshot.Match match = snapshot.resolve(method, authority, path);
            if (match.kind() == RouteTableSnapshot.Kind.MATCH) {
                String url = snapshot.endpoint()
                        + "/_dep/"
                        + match.route().routeKey()
                        + path
                        + (query == null ? "" : "?" + query);
                rewrites.incrementAndGet();
                return RewriteResult.rewritten(
                        url, "route", authority, path, query == null ? "" : query);
            }
            if (match.kind() == RouteTableSnapshot.Kind.AMBIGUOUS
                    || match.kind() == RouteTableSnapshot.Kind.UNKNOWN) {
                String reason = match.kind() == RouteTableSnapshot.Kind.AMBIGUOUS
                        ? "ambiguous"
                        : "unknown";
                StringBuilder url = new StringBuilder(snapshot.endpoint())
                        .append("/_agent/unmatched?reason=")
                        .append(reason)
                        .append("&host=")
                        .append(encode(authority))
                        .append("&method=")
                        .append(encode(method))
                        .append("&path=")
                        .append(encode(path));
                if (query != null && !query.isEmpty()) {
                    url.append("&query=").append(encode(query));
                }
                rewrites.incrementAndGet();
                return RewriteResult.rewritten(
                        url.toString(), reason, authority, path, query == null ? "" : query);
            }
            passthrough.incrementAndGet();
            Long previous = passthroughHosts.get(authority.toLowerCase(Locale.ROOT));
            passthroughHosts.put(
                    authority.toLowerCase(Locale.ROOT),
                    previous == null ? 1L : previous + 1L);
            return RewriteResult.unchanged();
        } catch (Throwable failure) {
            recordError(adapterId, failure);
            return RewriteResult.unchanged();
        }
    }

    /**
     * 该 URL 是否属于计划内下游（命中/歧义/计划内 host 未命中）。adapter 在
     * {@link #rewrite} 返回 unchanged 后用它判断能否安全放行：计划内必须失败关闭。
     */
    public boolean isPlanned(String method, String rawUrl) {
        RouteTableSnapshot snapshot = routes;
        if (snapshot == null || rawUrl == null) {
            return false;
        }
        try {
            URI uri = new URI(rawUrl);
            String scheme = uri.getScheme();
            if (scheme == null) {
                return false;
            }
            scheme = scheme.toLowerCase(Locale.ROOT);
            if (!"http".equals(scheme) && !"https".equals(scheme)) {
                return false;
            }
            String host = uri.getHost();
            if (host == null || host.isEmpty()) {
                return false;
            }
            String path = uri.getRawPath();
            if (path == null || path.isEmpty()) {
                path = "/";
            }
            for (String prefix : EXCLUDED_PREFIXES) {
                if (path.startsWith(prefix)) {
                    return false;
                }
            }
            int port = uri.getPort();
            if (isMockEndpoint(snapshot.endpoint(), host, port, scheme)) {
                return false;
            }
            String authority = host + (port == -1 ? "" : ":" + port);
            RouteTableSnapshot.Match match =
                    snapshot.resolve(method, authority, path);
            return match.kind() != RouteTableSnapshot.Kind.PASSTHROUGH;
        } catch (Throwable ignored) {
            return false;
        }
    }

    public void recordError(String adapterId, Throwable failure) {
        errors.incrementAndGet();
        note("rewrite failed [" + adapterId + "]: " + failure);
    }

    // ----------------------------------------------------------- MQ Producer

    public void mqModuleInstalled(String client) {
        mqInstalled = true;
        if (client != null && !client.isEmpty()) {
            mqClients.add(client);
        }
    }

    /**
     * 上报一条生产者消息。计划内 topic 返回 true，调用方必须抑制真实发送；
     * admin 不可用时同样返回 true（失败关闭），并计入 mqErrors。
     */
    public boolean publishMq(
            String client,
            String topic,
            JsonNode key,
            java.util.Map<String, String> headers,
            JsonNode body) {
        mqPublished.incrementAndGet();
        RouteTableSnapshot snapshot = routes;
        if (snapshot == null || !snapshot.isMqPlanned(topic)) {
            return false;
        }
        try {
            ObjectNode payload = Json.MAPPER.createObjectNode();
            payload.put("sut_id", options.sutId());
            payload.put("client", client == null ? "" : client);
            payload.put("topic", topic == null ? "" : topic);
            if (key == null) {
                payload.putNull("key");
            } else {
                payload.set("key", key);
            }
            ObjectNode headerNode = payload.putObject("headers");
            if (headers != null) {
                for (java.util.Map.Entry<String, String> entry : headers.entrySet()) {
                    headerNode.put(entry.getKey(), entry.getValue());
                }
            }
            if (body == null) {
                payload.putNull("body");
            } else {
                payload.set("body", body);
            }
            String response = admin.publishMq(payload.toString());
            JsonNode node = Json.MAPPER.readTree(response);
            boolean handled = node != null
                    && node.has("handled")
                    && node.get("handled").asBoolean(false);
            if (handled) {
                mqSuppressed.incrementAndGet();
            }
            return handled;
        } catch (Throwable failure) {
            mqErrors.incrementAndGet();
            note("mq publish failed: " + failure);
            // 计划内 MQ 不能因为 admin 故障退化为真实发送。
            return true;
        }
    }

    // ----------------------------------------------------------- Dubbo Filter

    public String adminUrl() {
        return options.adminUrl();
    }

    public String token() {
        return options.token();
    }

    public String sutId() {
        return options.sutId();
    }

    public boolean isDubboPlanned(String depId) {
        RouteTableSnapshot snapshot = routes;
        return snapshot != null && snapshot.isDubboPlanned(depId);
    }

    public boolean dubboRoutesReady() {
        return routes != null;
    }

    public void dubboModuleInstalled() {
        dubboInstalled = true;
    }

    public void dubboFilterInstantiated(String version) {
        dubboFilterInstantiated.incrementAndGet();
        if (version != null && !version.isEmpty()) {
            dubboVersion = version;
        }
    }

    public void dubboFilterHit() {
        dubboFilterHits.incrementAndGet();
    }

    public void dubboPassthrough() {
        dubboPassthrough.incrementAndGet();
    }

    public void dubboError(String message) {
        dubboErrors.incrementAndGet();
        note("dubbo: " + message);
    }

    void startBackground() {
        if (!backgroundStarted.compareAndSet(false, true)) {
            return;
        }
        Thread refresher = new Thread(new Runnable() {
            @Override
            public void run() {
                while (true) {
                    try {
                        Thread.sleep(options.routeRefreshIntervalMs());
                    } catch (InterruptedException interrupted) {
                        return;
                    }
                    refreshRoutes();
                }
            }
        }, "astsight-agent-routes");
        refresher.setDaemon(true);
        refresher.start();

        Thread heartbeat = new Thread(new Runnable() {
            @Override
            public void run() {
                while (true) {
                    try {
                        admin.sendHeartbeat(heartbeatJson());
                    } catch (Throwable failure) {
                        note("heartbeat failed: " + failure);
                    }
                    try {
                        Thread.sleep(options.heartbeatIntervalMs());
                    } catch (InterruptedException interrupted) {
                        return;
                    }
                }
            }
        }, "astsight-agent-heartbeat");
        heartbeat.setDaemon(true);
        heartbeat.start();

        // 短命进程（CLI/一次性任务）退出前补一次心跳，避免最后一次改写丢计数。
        Runtime.getRuntime().addShutdownHook(new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    admin.sendHeartbeat(heartbeatJson());
                } catch (Throwable ignored) {
                    // 退出阶段不阻断；服务端可能已经停止。
                }
            }
        }, "astsight-agent-final-heartbeat"));
    }

    private String heartbeatJson() {
        ObjectNode node = Json.MAPPER.createObjectNode();
        node.put("sut_id", options.sutId());
        node.put("agent_version", version());
        node.put("pid", currentPid());
        node.put("jvm_start_time_ms", ManagementFactory.getRuntimeMXBean().getStartTime());
        ArrayNode adapterNodes = node.putArray("http_adapters");
        ArrayNode mqAdapterNodes = node.putArray("mq_adapters");
        for (AdapterState state : adapters.values()) {
            ObjectNode item = ("mq".equals(state.kind) ? mqAdapterNodes : adapterNodes)
                    .addObject();
            item.put("id", state.id);
            item.put("version", state.version);
            item.put("active", state.active);
        }
        node.put("http_hooked", hooked.get());
        node.put("http_rewrites", rewrites.get());
        node.put("http_errors", errors.get());
        ArrayNode unsupportedNodes = node.putArray("http_stack_unsupported");
        for (String item : unsupported) {
            unsupportedNodes.add(item);
        }
        node.put("http_passthrough", passthrough.get());
        ObjectNode hosts = node.putObject("passthrough_hosts");
        for (Map.Entry<String, Long> entry : passthroughHosts.entrySet()) {
            hosts.put(entry.getKey(), entry.getValue());
        }
        if (mqInstalled) {
            ObjectNode mq = node.putObject("mq");
            mq.put("installed", true);
            ArrayNode clients = mq.putArray("clients");
            for (String client : mqClients) {
                clients.add(client);
            }
            mq.put("published", mqPublished.get());
            mq.put("suppressed", mqSuppressed.get());
            mq.put("errors", mqErrors.get());
        }
        if (dubboInstalled) {
            ObjectNode dubbo = node.putObject("dubbo");
            dubbo.put("installed", true);
            String version = dubboVersion;
            if (version == null) {
                dubbo.putNull("version");
            } else {
                dubbo.put("version", version);
            }
            dubbo.put("filter_instantiated", dubboFilterInstantiated.get());
            dubbo.put("filter_hits", dubboFilterHits.get());
            dubbo.put("passthrough", dubboPassthrough.get());
            dubbo.put("errors", dubboErrors.get());
            // Filter 在进程内构造响应对象，跳过 Hessian2 链路。
            dubbo.put("serialization_verified", false);
        }
        return node.toString();
    }

    private static boolean isMockEndpoint(
            String endpoint, String host, int port, String scheme) {
        try {
            URI mock = new URI(endpoint);
            String mockHost = mock.getHost();
            if (mockHost == null || !mockHost.equalsIgnoreCase(host)) {
                return false;
            }
            int mockPort = mock.getPort();
            if (port != -1) {
                return mockPort == port;
            }
            int defaultPort = "https".equals(scheme) ? 443 : 80;
            return mockPort == defaultPort || mockPort == -1;
        } catch (Exception ignored) {
            return false;
        }
    }

    private static String encode(String value) {
        try {
            return URLEncoder.encode(value == null ? "" : value, "UTF-8");
        } catch (UnsupportedEncodingException impossible) {
            return "";
        }
    }

    private static long currentPid() {
        try {
            String name = ManagementFactory.getRuntimeMXBean().getName();
            int at = name.indexOf('@');
            return Long.parseLong(at == -1 ? name : name.substring(0, at));
        } catch (Throwable ignored) {
            return 0L;
        }
    }

    private static String version() {
        Package pkg = AgentRuntime.class.getPackage();
        String value = pkg == null ? null : pkg.getImplementationVersion();
        return value == null || value.isEmpty() ? "0.1.0-SNAPSHOT" : value;
    }

    private void note(String message) {
        if (message.equals(lastError)) {
            return;
        }
        lastError = message;
        System.err.println("[astsight-agent] " + message);
    }

    private static final class AdapterState {
        private final String id;
        private final String version;
        private final boolean active;
        private final String kind;

        private AdapterState(String id, String version, boolean active, String kind) {
            this.id = id;
            this.version = version;
            this.active = active;
            this.kind = kind;
        }
    }

    public List<String> adapterIds() {
        List<String> ids = new ArrayList<String>();
        for (AdapterState state : adapters.values()) {
            if (state.active) {
                ids.add(state.id);
            }
        }
        Collections.sort(ids);
        return ids;
    }
}
