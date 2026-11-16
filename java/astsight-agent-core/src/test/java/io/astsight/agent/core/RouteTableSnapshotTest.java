package io.astsight.agent.core;

import org.junit.jupiter.api.Test;

import java.util.Arrays;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 路由快照匹配优先级：host+method+path > 字面量路径优先 > 唯一 method+path > 歧义/未命中。
 * 与 Python 端 {@code astsight.mock.routes.RouteTable} 的解析结果保持一致。
 */
final class RouteTableSnapshotTest {

    private static RouteTableSnapshot snapshot(RouteTableSnapshot.Route... routes) {
        return new RouteTableSnapshot(
                "http://127.0.0.1:9999", Arrays.asList(routes));
    }

    private static RouteTableSnapshot.Route route(
            String key, String pathKey, String host) {
        return new RouteTableSnapshot.Route(
                key,
                "http:svc:" + host + " GET " + pathKey,
                "GET",
                pathKey,
                host,
                "https://" + host);
    }

    @Test
    void exactPathWinsOverTemplateForSameHost() {
        RouteTableSnapshot table = snapshot(
                route("r1", "/orders/7", "api.example.com"),
                route("r2", "/orders/{}", "api.example.com"));

        RouteTableSnapshot.Match exact = table.resolve("GET", "api.example.com", "/orders/7");
        assertEquals(RouteTableSnapshot.Kind.MATCH, exact.kind());
        assertEquals("r1", exact.route().routeKey());

        // 模板只承接字面量覆盖不到的路径。
        RouteTableSnapshot.Match template =
                table.resolve("GET", "api.example.com", "/orders/8");
        assertEquals(RouteTableSnapshot.Kind.MATCH, template.kind());
        assertEquals("r2", template.route().routeKey());
    }

    @Test
    void exactPathWinsWhenHostIsUnknown() {
        RouteTableSnapshot table = snapshot(
                route("r1", "/orders/7", "a"),
                route("r2", "/orders/{}", "b"));

        RouteTableSnapshot.Match match = table.resolve("GET", "unknown", "/orders/7");

        assertEquals(RouteTableSnapshot.Kind.MATCH, match.kind());
        assertEquals("r1", match.route().routeKey());
    }

    @Test
    void twoTemplatesStayAmbiguous() {
        RouteTableSnapshot table = snapshot(
                route("r1", "/orders/{}", "a"),
                route("r2", "/orders/{}", "a"));

        RouteTableSnapshot.Match match = table.resolve("GET", "a", "/orders/7");

        assertEquals(RouteTableSnapshot.Kind.AMBIGUOUS, match.kind());
        assertNull(match.route());
    }

    @Test
    void plannedHostWithoutMatchIsUnknown() {
        RouteTableSnapshot table = snapshot(route("r1", "/orders/7", "a"));

        RouteTableSnapshot.Match match = table.resolve("GET", "a", "/missing");

        assertEquals(RouteTableSnapshot.Kind.UNKNOWN, match.kind());
    }

    @Test
    void unplannedHostStaysPassthrough() {
        RouteTableSnapshot table = snapshot(route("r1", "/orders/7", "a"));

        RouteTableSnapshot.Match match = table.resolve("GET", "other", "/orders/7x");

        assertEquals(RouteTableSnapshot.Kind.PASSTHROUGH, match.kind());
    }

    @Test
    void dubboDepsAreParsedForPlannedChecks() throws Exception {
        RouteTableSnapshot table = RouteTableSnapshot.parse(
                "{\"endpoint\":\"http://127.0.0.1:9\","
                        + "\"routes\":[],"
                        + "\"dubbo_deps\":[\"dubbo:com.acme.OrderService#create?group=order\"]}",
                "http://127.0.0.1:9");

        assertTrue(table.isDubboPlanned("dubbo:com.acme.OrderService#create?group=order"));
        assertFalse(table.isDubboPlanned("dubbo:com.acme.OrderService#cancel"));
        assertFalse(table.isDubboPlanned(null));
    }
}
