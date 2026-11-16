package io.astsight.agent.core;

/** 一次出站 URL 改写的判定结果。 */
public final class RewriteResult {

    private static final RewriteResult UNCHANGED =
            new RewriteResult(false, null, null, null, null, null);

    private final boolean rewritten;
    private final String url;
    private final String reason;
    private final String originalHost;
    private final String originalPath;
    private final String originalQuery;

    private RewriteResult(
            boolean rewritten,
            String url,
            String reason,
            String originalHost,
            String originalPath,
            String originalQuery) {
        this.rewritten = rewritten;
        this.url = url;
        this.reason = reason;
        this.originalHost = originalHost;
        this.originalPath = originalPath;
        this.originalQuery = originalQuery;
    }

    public static RewriteResult unchanged() {
        return UNCHANGED;
    }

    public static RewriteResult rewritten(
            String url,
            String reason,
            String originalHost,
            String originalPath,
            String originalQuery) {
        return new RewriteResult(true, url, reason, originalHost, originalPath, originalQuery);
    }

    public boolean isRewritten() {
        return rewritten;
    }

    public String url() {
        return url;
    }

    public String reason() {
        return reason;
    }

    public String originalHost() {
        return originalHost;
    }

    public String originalPath() {
        return originalPath;
    }

    public String originalQuery() {
        return originalQuery;
    }
}
