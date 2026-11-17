package io.astsight.sidecar.dubbo;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.net.MalformedURLException;
import java.net.URI;
import java.time.Duration;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;
import java.time.OffsetDateTime;
import java.time.Period;
import java.time.ZoneOffset;
import java.time.ZonedDateTime;
import java.util.Date;
import java.util.UUID;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 一条 {@code invoke} 请求的已校验形态。
 *
 * <p>首版只支持直连 {@code dubbo://host:port}。注册中心（Nacos / ZooKeeper）
 * 需要的客户端依赖不随 sidecar 分发，收到 {@code registry} 时显式失败，
 * 不猜地址。
 */
final class InvokeRequest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    static final int DEFAULT_TIMEOUT_MS = 5_000;
    static final int MAX_TIMEOUT_MS = 600_000;
    static final String HESSIAN2 = "hessian2";

    final String service;
    final String method;
    final String[] paramTypes;
    final List<JsonNode> arguments;
    final String group;
    final String version;
    final String url;
    final int timeoutMs;
    final Map<String, String> attachments;

    private InvokeRequest(
            String service,
            String method,
            String[] paramTypes,
            List<JsonNode> arguments,
            String group,
            String version,
            String url,
            int timeoutMs,
            Map<String, String> attachments) {
        this.service = service;
        this.method = method;
        this.paramTypes = paramTypes;
        this.arguments = arguments;
        this.group = group;
        this.version = version;
        this.url = url;
        this.timeoutMs = timeoutMs;
        this.attachments = attachments;
    }

    static InvokeRequest parse(JsonNode node) throws RequestException {
        String service = requiredText(node, "interface");
        String method = requiredText(node, "method");
        String[] paramTypes = paramTypes(node);
        List<JsonNode> arguments = arguments(node);
        if (paramTypes.length != arguments.size()) {
            throw new RequestException(
                    "bad_request",
                    "param_types 数量 " + paramTypes.length
                            + " 与 arguments 数量 " + arguments.size() + " 不一致");
        }

        String serialization = optionalText(node, "serialization");
        if (serialization != null && !serialization.isEmpty()
                && !HESSIAN2.equalsIgnoreCase(serialization)) {
            throw new RequestException(
                    "unsupported_serialization",
                    "首版只支持 hessian2，收到 " + serialization);
        }

        String group = nullableText(node, "group");
        String version = nullableText(node, "version");
        String registry = nullableText(node, "registry");
        if (registry != null) {
            throw new RequestException(
                    "registry_not_supported",
                    "首版只支持直连 url，注册中心发现尚未实现");
        }
        String url = directUrl(node);
        int timeoutMs = timeoutMs(node);
        Map<String, String> attachments = attachments(node);

        return new InvokeRequest(
                service,
                method,
                paramTypes,
                arguments,
                group,
                version,
                url,
                timeoutMs,
                attachments);
    }

    private static String requiredText(JsonNode node, String field) throws RequestException {
        JsonNode value = node.get(field);
        if (value == null || value.isNull() || !value.isTextual()
                || value.asText().trim().isEmpty()) {
            throw new RequestException("bad_request", "缺少或非法的 " + field);
        }
        return value.asText().trim();
    }

    private static String optionalText(JsonNode node, String field) throws RequestException {
        JsonNode value = node.get(field);
        if (value == null || value.isNull()) {
            return null;
        }
        if (!value.isTextual()) {
            throw new RequestException("bad_request", field + " 必须是字符串");
        }
        return value.asText().trim();
    }

    private static String nullableText(JsonNode node, String field) throws RequestException {
        String value = optionalText(node, field);
        return value == null || value.isEmpty() ? null : value;
    }

    private static String[] paramTypes(JsonNode node) throws RequestException {
        JsonNode value = node.get("param_types");
        if (value == null || value.isNull()) {
            return new String[0];
        }
        if (!value.isArray()) {
            throw new RequestException("bad_request", "param_types 必须是数组");
        }
        String[] result = new String[value.size()];
        for (int index = 0; index < value.size(); index++) {
            JsonNode item = value.get(index);
            if (item == null || !item.isTextual() || item.asText().trim().isEmpty()) {
                throw new RequestException(
                        "bad_request", "param_types[" + index + "] 必须是非空字符串");
            }
            result[index] = item.asText().trim();
        }
        return result;
    }

    private static List<JsonNode> arguments(JsonNode node) throws RequestException {
        JsonNode value = node.get("arguments");
        if (value == null || value.isNull()) {
            return Collections.emptyList();
        }
        if (!value.isArray()) {
            throw new RequestException("bad_request", "arguments 必须是数组");
        }
        List<JsonNode> result = new ArrayList<JsonNode>(value.size());
        for (JsonNode item : value) {
            result.add(item);
        }
        return result;
    }

    private static int timeoutMs(JsonNode node) throws RequestException {
        JsonNode value = node.get("timeout_ms");
        if (value == null || value.isNull()) {
            return DEFAULT_TIMEOUT_MS;
        }
        if (!value.isIntegralNumber()) {
            throw new RequestException("bad_request", "timeout_ms 必须是整数");
        }
        long raw = value.asLong();
        if (raw < 1 || raw > MAX_TIMEOUT_MS) {
            throw new RequestException(
                    "bad_request", "timeout_ms 必须在 1.." + MAX_TIMEOUT_MS + " 之间");
        }
        return (int) raw;
    }

    private static Map<String, String> attachments(JsonNode node) throws RequestException {
        JsonNode value = node.get("attachments");
        if (value == null || value.isNull()) {
            return Collections.emptyMap();
        }
        if (!value.isObject()) {
            throw new RequestException("bad_request", "attachments 必须是对象");
        }
        Map<String, String> result = new LinkedHashMap<String, String>();
        Iterator<Map.Entry<String, JsonNode>> fields = value.fields();
        while (fields.hasNext()) {
            Map.Entry<String, JsonNode> field = fields.next();
            JsonNode item = field.getValue();
            if (item == null || item.isNull()) {
                continue;
            }
            if (item.isContainerNode()) {
                throw new RequestException(
                        "bad_request", "attachments." + field.getKey() + " 必须是标量");
            }
            result.put(field.getKey(), item.asText());
        }
        return result;
    }

    private static String directUrl(JsonNode node) throws RequestException {
        String value = nullableText(node, "url");
        if (value == null) {
            throw new RequestException(
                    "direct_url_required", "首版必须提供直连 url（dubbo://host:port）");
        }
        String url = value.contains("://") ? value : "dubbo://" + value;
        if (!url.startsWith("dubbo://")) {
            throw new RequestException(
                    "unsupported_protocol",
                    "只支持 dubbo:// 直连地址，收到 " + url);
        }
        return url;
    }

    // ------------------------------------------------------------- 参数转换

    /** 按声明类型转换标量；POJO/集合保持 JSON 结构，交给 Dubbo 泛化调用。 */
    static Object convertArgument(JsonNode node, String type) {
        if (node == null || node.isNull()) {
            return null;
        }
        try {
            if ("java.lang.String".equals(type) || "String".equals(type)) {
                return node.asText();
            }
            if ("char".equals(type) || "java.lang.Character".equals(type)) {
                String text = node.asText();
                if (text.length() != 1) {
                    throw new BadArgument("char 参数必须是单个字符");
                }
                return Character.valueOf(text.charAt(0));
            }
            if ("boolean".equals(type) || "java.lang.Boolean".equals(type)) {
                if (!node.isBoolean()) {
                    throw new BadArgument("boolean 参数必须是 true/false");
                }
                return Boolean.valueOf(node.asBoolean());
            }
            if ("byte".equals(type) || "java.lang.Byte".equals(type)) {
                return Byte.valueOf((byte) requireNumber(node, type).intValue());
            }
            if ("short".equals(type) || "java.lang.Short".equals(type)) {
                return Short.valueOf((short) requireNumber(node, type).intValue());
            }
            if ("int".equals(type) || "java.lang.Integer".equals(type)) {
                return Integer.valueOf(requireNumber(node, type).intValue());
            }
            if ("long".equals(type) || "java.lang.Long".equals(type)) {
                return Long.valueOf(requireNumber(node, type).longValue());
            }
            if ("float".equals(type) || "java.lang.Float".equals(type)) {
                return Float.valueOf(requireNumber(node, type).floatValue());
            }
            if ("double".equals(type) || "java.lang.Double".equals(type)) {
                return Double.valueOf(requireNumber(node, type).doubleValue());
            }
            if ("java.math.BigDecimal".equals(type)) {
                return new BigDecimal(node.asText());
            }
            if ("java.math.BigInteger".equals(type)) {
                return new BigInteger(node.asText());
            }
            if ("java.time.LocalDate".equals(type)) {
                return LocalDate.parse(node.asText());
            }
            if ("java.time.LocalTime".equals(type)) {
                return LocalTime.parse(node.asText());
            }
            if ("java.time.LocalDateTime".equals(type)) {
                return LocalDateTime.parse(node.asText());
            }
            if ("java.time.OffsetDateTime".equals(type)) {
                return OffsetDateTime.parse(node.asText());
            }
            if ("java.time.ZonedDateTime".equals(type)) {
                return ZonedDateTime.parse(node.asText());
            }
            if ("java.time.Instant".equals(type)) {
                return Instant.parse(node.asText());
            }
            if ("java.time.Duration".equals(type)) {
                return Duration.parse(node.asText());
            }
            if ("java.time.Period".equals(type)) {
                return Period.parse(node.asText());
            }
            if ("java.util.Date".equals(type)) {
                if (node.isNumber()) {
                    return new Date(node.asLong());
                }
                return Date.from(parseInstant(node.asText()));
            }
            if ("java.sql.Date".equals(type)) {
                return java.sql.Date.valueOf(LocalDate.parse(node.asText()));
            }
            if ("java.sql.Time".equals(type)) {
                return java.sql.Time.valueOf(LocalTime.parse(node.asText()));
            }
            if ("java.sql.Timestamp".equals(type)) {
                if (node.isNumber()) {
                    return new java.sql.Timestamp(node.asLong());
                }
                return java.sql.Timestamp.from(parseInstant(node.asText()));
            }
            if ("java.util.UUID".equals(type)) {
                return UUID.fromString(node.asText());
            }
            if ("java.net.URI".equals(type)) {
                return URI.create(node.asText());
            }
            if ("java.net.URL".equals(type)) {
                try {
                    return URI.create(node.asText()).toURL();
                } catch (MalformedURLException failure) {
                    throw new BadArgument("URL 参数非法: " + node.asText());
                }
            }
            if (node.isContainerNode()) {
                return MAPPER.convertValue(node, Object.class);
            }
            if (node.isTextual()) {
                return node.asText();
            }
            if (node.isBoolean()) {
                return Boolean.valueOf(node.asBoolean());
            }
            return MAPPER.convertValue(node, Object.class);
        } catch (BadArgument failure) {
            throw failure;
        } catch (RuntimeException failure) {
            throw new BadArgument(
                    "参数 " + type + " 无法从 " + node + " 转换：" + failure.getMessage());
        }
    }

    private static Instant parseInstant(String text) {
        try {
            return Instant.parse(text);
        } catch (DateTimeParseException ignored) {
            // 继续尝试无时区格式。
        }
        try {
            return LocalDateTime.parse(text).toInstant(ZoneOffset.UTC);
        } catch (DateTimeParseException ignored) {
            // 继续尝试只有日期。
        }
        try {
            return LocalDate.parse(text).atStartOfDay().toInstant(ZoneOffset.UTC);
        } catch (DateTimeParseException ignored) {
            throw new BadArgument("时间格式不支持: " + text);
        }
    }

    private static Number requireNumber(JsonNode node, String type) {
        if (!node.isNumber()) {
            throw new BadArgument(type + " 参数必须是数字");
        }
        return node.numberValue();
    }

    /** 参数取值与声明类型不匹配：归为请求错误，不伪装成 RPC 异常。 */
    static final class BadArgument extends RuntimeException {
        BadArgument(String message) {
            super(message);
        }
    }
}
