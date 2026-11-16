package io.astsight.agent.core;

import com.fasterxml.jackson.databind.JsonNode;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * 一个只读路由快照。匹配规则与 Python 端 {@code astsight.mock.routes.RouteTable}
 * 保持同一优先级：host+method+path > 唯一 method+path > 歧义/未命中；同类候选里
 * 字面量路径优先于 `{}` 模板（模板之间、字面量之间仍按歧义处理）。
 */
final class RouteTableSnapshot {

    enum Kind {
        MATCH,
        AMBIGUOUS,
        UNKNOWN,
        PASSTHROUGH
    }

    static final class Match {
        private final Kind kind;
        private final Route route;

        private Match(Kind kind, Route route) {
            this.kind = kind;
            this.route = route;
        }

        static Match of(Kind kind) {
            return new Match(kind, null);
        }

        static Match matched(Route route) {
            return new Match(Kind.MATCH, route);
        }

        Kind kind() {
            return kind;
        }

        Route route() {
            return route;
        }
    }

    static final class Route {
        private final String routeKey;
        private final String depId;
        private final String method;
        private final String pathKey;
        private final String host;
        private final String target;
        private final Pattern pathPattern;

        Route(
                String routeKey,
                String depId,
                String method,
                String pathKey,
                String host,
                String target) {
            this.routeKey = routeKey;
            this.depId = depId;
            this.method = method == null ? "" : method.trim().toUpperCase(Locale.ROOT);
            this.pathKey = normalizePath(pathKey);
            this.host = normalizeRouteHost(host);
            this.target = target;
            this.pathPattern = compilePath(this.pathKey);
        }

        String routeKey() {
            return routeKey;
        }

        String depId() {
            return depId;
        }

        String method() {
            return method;
        }

        String pathKey() {
            return pathKey;
        }

        String host() {
            return host;
        }

        String target() {
            return target;
        }

        boolean matchesPath(String path) {
            return pathPattern.matcher(path).matches();
        }

        boolean exactPath() {
            return !pathKey.contains("{}");
        }
    }

    private final String endpoint;
    private final List<Route> routes;
    private final Set<String> dubboDeps;

    RouteTableSnapshot(String endpoint, List<Route> routes) {
        this(endpoint, routes, Collections.<String>emptySet());
    }

    RouteTableSnapshot(String endpoint, List<Route> routes, Set<String> dubboDeps) {
        this.endpoint = endpoint;
        this.routes = Collections.unmodifiableList(new ArrayList<Route>(routes));
        this.dubboDeps = Collections.unmodifiableSet(
                new LinkedHashSet<String>(dubboDeps == null ? Collections.<String>emptySet() : dubboDeps));
    }

    String endpoint() {
        return endpoint;
    }

    List<Route> routes() {
        return routes;
    }

    static RouteTableSnapshot parse(String json, String fallbackEndpoint) throws Exception {
        JsonNode root = Json.MAPPER.readTree(json);
        String endpoint = text(root, "endpoint");
        if (endpoint == null || endpoint.isEmpty()) {
            endpoint = fallbackEndpoint;
        }
        List<Route> parsed = new ArrayList<Route>();
        JsonNode items = root.get("routes");
        if (items != null && items.isArray()) {
            for (JsonNode item : items) {
                parsed.add(
                        new Route(
                                text(item, "route_key"),
                                text(item, "dep_id"),
                                text(item, "method"),
                                text(item, "path_key"),
                                text(item, "host"),
                                text(item, "target")));
            }
        }
        Set<String> dubbo = new LinkedHashSet<String>();
        JsonNode dubboItems = root.get("dubbo_deps");
        if (dubboItems != null && dubboItems.isArray()) {
            for (JsonNode item : dubboItems) {
                String depId = item.asText();
                if (depId != null && !depId.isEmpty()) {
                    dubbo.add(depId);
                }
            }
        }
        return new RouteTableSnapshot(trimSlash(endpoint), parsed, dubbo);
    }

    boolean isDubboPlanned(String depId) {
        return depId != null && dubboDeps.contains(depId);
    }

    Match resolve(String method, String hostHeader, String rawPath) {
        String normalizedMethod =
                method == null ? "" : method.trim().toUpperCase(Locale.ROOT);
        String path = normalizePath(rawPath);
        List<String> candidates = hostCandidates(hostHeader);
        List<Route> hostMatches = new ArrayList<Route>();
        if (!candidates.isEmpty()) {
            for (Route route : routes) {
                if (route.host != null
                        && candidates.contains(route.host)
                        && route.method.equals(normalizedMethod)
                        && route.matchesPath(path)) {
                    hostMatches.add(route);
                }
            }
        }
        hostMatches = preferExact(hostMatches);
        if (hostMatches.size() == 1) {
            return Match.matched(hostMatches.get(0));
        }
        if (hostMatches.size() > 1) {
            return Match.of(Kind.AMBIGUOUS);
        }

        List<Route> methodPathMatches = new ArrayList<Route>();
        for (Route route : routes) {
            if (route.method.equals(normalizedMethod) && route.matchesPath(path)) {
                methodPathMatches.add(route);
            }
        }
        methodPathMatches = preferExact(methodPathMatches);
        if (methodPathMatches.size() == 1) {
            return Match.matched(methodPathMatches.get(0));
        }
        if (methodPathMatches.size() > 1) {
            return Match.of(Kind.AMBIGUOUS);
        }
        return isPlannedHost(candidates) ? Match.of(Kind.UNKNOWN) : Match.of(Kind.PASSTHROUGH);
    }

    /**
     * 候选里恰好有一个字面量路径时优先它；多个字面量或多个模板仍视为歧义。
     * 与 Python 端 {@code RouteTable.resolve} 的 `_prefer_exact` 保持一致。
     */
    private static List<Route> preferExact(List<Route> matches) {
        if (matches.size() < 2) {
            return matches;
        }
        List<Route> exact = new ArrayList<Route>();
        for (Route route : matches) {
            if (route.exactPath()) {
                exact.add(route);
            }
        }
        return exact.size() == 1 ? exact : matches;
    }

    private boolean isPlannedHost(List<String> candidates) {
        if (candidates.isEmpty()) {
            return false;
        }
        for (Route route : routes) {
            if (route.host != null && candidates.contains(route.host)) {
                return true;
            }
        }
        return false;
    }

    private static List<String> hostCandidates(String hostHeader) {
        LinkedHashSet<String> candidates = new LinkedHashSet<String>();
        if (hostHeader == null) {
            return new ArrayList<String>();
        }
        String value = hostHeader.trim().toLowerCase(Locale.ROOT);
        while (value.endsWith(".")) {
            value = value.substring(0, value.length() - 1);
        }
        if (value.isEmpty()) {
            return new ArrayList<String>();
        }
        candidates.add(value);
        if (value.startsWith("[")) {
            int close = value.indexOf(']');
            if (close != -1) {
                String bare = value.substring(0, close + 1);
                candidates.add(bare);
                if (bare.equals(value)) {
                    candidates.add(bare + ":80");
                }
            }
        } else if (value.indexOf(':') != -1) {
            candidates.add(value.substring(0, value.lastIndexOf(':')));
        } else {
            candidates.add(value + ":80");
        }
        return new ArrayList<String>(candidates);
    }

    private static String normalizeRouteHost(String host) {
        if (host == null) {
            return null;
        }
        String value = host.trim().toLowerCase(Locale.ROOT);
        while (value.endsWith(".")) {
            value = value.substring(0, value.length() - 1);
        }
        return value.isEmpty() ? null : value;
    }

    static String normalizePath(String raw) {
        if (raw == null) {
            return "/";
        }
        String value = raw;
        int hash = value.indexOf('#');
        if (hash != -1) {
            value = value.substring(0, hash);
        }
        int query = value.indexOf('?');
        if (query != -1) {
            value = value.substring(0, query);
        }
        while (value.indexOf("//") != -1) {
            value = value.replace("//", "/");
        }
        if (!value.startsWith("/")) {
            value = "/" + value;
        }
        while (value.length() > 1 && value.endsWith("/")) {
            value = value.substring(0, value.length() - 1);
        }
        return value.isEmpty() ? "/" : value;
    }

    private static Pattern compilePath(String pathKey) {
        if ("/".equals(pathKey)) {
            return Pattern.compile("^/$");
        }
        String trimmed = pathKey;
        if (trimmed.startsWith("/")) {
            trimmed = trimmed.substring(1);
        }
        if (trimmed.endsWith("/")) {
            trimmed = trimmed.substring(0, trimmed.length() - 1);
        }
        String[] segments = trimmed.split("/", -1);
        StringBuilder regex = new StringBuilder("^");
        for (String segment : segments) {
            regex.append('/');
            String[] literals = segment.split("\\{\\}", -1);
            for (int i = 0; i < literals.length; i++) {
                if (i > 0) {
                    regex.append("[^/]+");
                }
                regex.append(Pattern.quote(literals[i]));
            }
        }
        regex.append('$');
        return Pattern.compile(regex.toString());
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.get(field);
        return value == null || value.isNull() ? null : value.asText();
    }

    private static String trimSlash(String value) {
        String result = value == null ? "" : value.trim();
        while (result.endsWith("/")) {
            result = result.substring(0, result.length() - 1);
        }
        return result;
    }
}
