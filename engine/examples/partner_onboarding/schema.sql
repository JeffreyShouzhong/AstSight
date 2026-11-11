-- 合伙人注册示例库（SQLite）。
-- 既能被示例服务直接执行，也能作为 AstSight C5a 的 DDL 输入：
--   python -m astsight data catalog --project <repo> --ddl schema.sql

CREATE TABLE IF NOT EXISTS partner_seq (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

INSERT OR IGNORE INTO partner_seq(name, value) VALUES ('partner', 0);

CREATE TABLE IF NOT EXISTS partner (
    seq               INTEGER PRIMARY KEY,
    partner_id        TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    mobile            TEXT NOT NULL UNIQUE,
    city              TEXT NOT NULL,
    id_card           TEXT NOT NULL,
    status            TEXT NOT NULL,
    audit_status      TEXT NOT NULL,
    audit_step        INTEGER NOT NULL DEFAULT 1,
    submit_at         TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL,
    agreement_version TEXT NOT NULL,
    agreement_checked_at   TEXT NOT NULL DEFAULT '',
    agreement_ip           TEXT NOT NULL DEFAULT '',
    agreement_user_agent   TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_partner_id_card ON partner(id_card);

CREATE TABLE IF NOT EXISTS partner_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id  TEXT NOT NULL,
    action      TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    audit_step  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (partner_id) REFERENCES partner(partner_id)
);

CREATE INDEX IF NOT EXISTS idx_audit_partner ON partner_audit_log(partner_id);
