"""
SQL DDL executed once at startup to guarantee the schema exists.
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER UNIQUE NOT NULL,
    username        TEXT,
    first_name      TEXT,
    is_vip          INTEGER DEFAULT 0,
    is_banned       INTEGER DEFAULT 0,
    ban_reason      TEXT,
    vip_expires_at  TIMESTAMP,
    daily_used_bytes INTEGER DEFAULT 0,
    daily_reset_at  TIMESTAMP,
    total_extractions    INTEGER DEFAULT 0,
    total_cookies_found  INTEGER DEFAULT 0,
    total_bytes_processed INTEGER DEFAULT 0,
    joined_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_active     TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    domain          TEXT,
    archive_name    TEXT,
    file_size_bytes INTEGER,
    cookies_found   INTEGER DEFAULT 0,
    files_scanned   INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'queued',
    error_message   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    duration_seconds REAL
);

CREATE TABLE IF NOT EXISTS vip_requests (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    username        TEXT,
    first_name      TEXT,
    message         TEXT,
    status          TEXT DEFAULT 'pending',
    duration_days   INTEGER,
    requested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actioned_at     TIMESTAMP,
    actioned_by     INTEGER
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id              INTEGER PRIMARY KEY,
    message         TEXT,
    sent_by         INTEGER,
    sent_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    recipient_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""
