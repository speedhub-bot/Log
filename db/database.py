"""
Async SQLite helpers — thin wrapper around aiosqlite.
Every public function is an *awaitable*.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

import config
from db.models import SCHEMA_SQL

_db_lock = asyncio.Lock()
_db: Optional[aiosqlite.Connection] = None


async def get_db() -> aiosqlite.Connection:
    """Return the shared connection, creating it on first call."""
    global _db
    if _db is None:
        async with _db_lock:
            if _db is None:
                _db = await aiosqlite.connect(config.DATABASE_PATH)
                _db.row_factory = aiosqlite.Row
                await _db.executescript(SCHEMA_SQL)
                await _db.commit()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ── Helpers ─────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetchone(sql: str, params: Tuple[Any, ...] = ()) -> Optional[aiosqlite.Row]:
    db = await get_db()
    async with db.execute(sql, params) as cur:
        return await cur.fetchone()


async def _fetchall(sql: str, params: Tuple[Any, ...] = ()) -> List[aiosqlite.Row]:
    db = await get_db()
    async with db.execute(sql, params) as cur:
        return await cur.fetchall()


async def _execute(sql: str, params: Tuple[Any, ...] = ()) -> int:
    db = await get_db()
    async with db.execute(sql, params) as cur:
        await db.commit()
        return cur.lastrowid or 0


async def _execute_returning_rowcount(sql: str, params: Tuple[Any, ...] = ()) -> int:
    db = await get_db()
    async with db.execute(sql, params) as cur:
        await db.commit()
        return cur.rowcount


# ── Users ───────────────────────────────────────────────────
async def ensure_user(user_id: int, username: Optional[str] = None,
                      first_name: Optional[str] = None) -> aiosqlite.Row:
    """Insert-or-update user row and return it."""
    row = await _fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
    now = _now()
    if row is None:
        await _execute(
            "INSERT INTO users (user_id, username, first_name, daily_reset_at, last_active) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, first_name, now, now),
        )
        return await _fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))  # type: ignore[return-value]
    await _execute(
        "UPDATE users SET username = ?, first_name = ?, last_active = ? WHERE user_id = ?",
        (username or row["username"], first_name or row["first_name"], now, user_id),
    )
    return await _fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))  # type: ignore[return-value]


async def get_user(user_id: int) -> Optional[aiosqlite.Row]:
    return await _fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))


async def set_vip(user_id: int, days: int) -> None:
    """Grant VIP. days=0 means forever (NULL expiry)."""
    expires: Optional[str] = None
    if days > 0:
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    await _execute(
        "UPDATE users SET is_vip = 1, vip_expires_at = ? WHERE user_id = ?",
        (expires, user_id),
    )


async def revoke_vip(user_id: int) -> None:
    await _execute(
        "UPDATE users SET is_vip = 0, vip_expires_at = NULL WHERE user_id = ?",
        (user_id,),
    )


async def ban_user(user_id: int, reason: str) -> None:
    await _execute(
        "UPDATE users SET is_banned = 1, ban_reason = ? WHERE user_id = ?",
        (reason, user_id),
    )


async def unban_user(user_id: int) -> None:
    await _execute(
        "UPDATE users SET is_banned = 0, ban_reason = NULL WHERE user_id = ?",
        (user_id,),
    )


async def is_vip(user_id: int) -> bool:
    row = await get_user(user_id)
    if row is None or not row["is_vip"]:
        return False
    expires = row["vip_expires_at"]
    if expires is None:
        return True
    return datetime.fromisoformat(expires) > datetime.now(timezone.utc)


async def get_remaining_quota(user_id: int) -> int:
    """Return remaining bytes for today. -1 = unlimited (VIP)."""
    if await is_vip(user_id):
        return -1
    row = await get_user(user_id)
    if row is None:
        return config.FREE_DAILY_LIMIT_BYTES
    used = row["daily_used_bytes"] or 0
    return max(0, config.FREE_DAILY_LIMIT_BYTES - used)


async def consume_quota(user_id: int, size_bytes: int) -> None:
    await _execute(
        "UPDATE users SET daily_used_bytes = daily_used_bytes + ? WHERE user_id = ?",
        (size_bytes, user_id),
    )


async def reset_all_quotas() -> None:
    now = _now()
    await _execute(
        "UPDATE users SET daily_used_bytes = 0, daily_reset_at = ?",
        (now,),
    )


async def expire_vip_users() -> List[int]:
    """Down-grade expired VIPs and return their user_ids."""
    now = _now()
    rows = await _fetchall(
        "SELECT user_id FROM users WHERE is_vip = 1 AND vip_expires_at IS NOT NULL "
        "AND vip_expires_at < ?",
        (now,),
    )
    ids = [r["user_id"] for r in rows]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        await _execute(
            f"UPDATE users SET is_vip = 0, vip_expires_at = NULL WHERE user_id IN ({placeholders})",
            tuple(ids),
        )
    return ids


async def increment_user_stats(user_id: int, cookies: int, bytes_processed: int) -> None:
    await _execute(
        "UPDATE users SET total_extractions = total_extractions + 1, "
        "total_cookies_found = total_cookies_found + ?, "
        "total_bytes_processed = total_bytes_processed + ? "
        "WHERE user_id = ?",
        (cookies, bytes_processed, user_id),
    )


async def get_all_user_ids() -> List[int]:
    rows = await _fetchall("SELECT user_id FROM users")
    return [r["user_id"] for r in rows]


async def get_users_page(page: int, per_page: int = 10) -> Tuple[List[aiosqlite.Row], int]:
    total_row = await _fetchone("SELECT COUNT(*) as cnt FROM users")
    total = total_row["cnt"] if total_row else 0
    rows = await _fetchall(
        "SELECT * FROM users ORDER BY last_active DESC LIMIT ? OFFSET ?",
        (per_page, page * per_page),
    )
    return rows, total


async def set_custom_limit(user_id: int, gb: int) -> None:
    """Store a per-user override in the settings table."""
    await _execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (f"user_limit_{user_id}", str(gb)),
    )


async def add_quota(user_id: int, extra_bytes: int) -> None:
    """Give extra quota by reducing daily_used_bytes (floor 0)."""
    await _execute(
        "UPDATE users SET daily_used_bytes = MAX(0, daily_used_bytes - ?) WHERE user_id = ?",
        (extra_bytes, user_id),
    )


# ── Jobs ────────────────────────────────────────────────────
async def create_job(user_id: int, domain: str, archive_name: str,
                     file_size_bytes: int) -> int:
    return await _execute(
        "INSERT INTO jobs (user_id, domain, archive_name, file_size_bytes, status, created_at) "
        "VALUES (?, ?, ?, ?, 'queued', ?)",
        (user_id, domain, archive_name, file_size_bytes, _now()),
    )


async def update_job(job_id: int, **kwargs: Any) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    await _execute(f"UPDATE jobs SET {cols} WHERE id = ?", tuple(vals))


async def get_job(job_id: int) -> Optional[aiosqlite.Row]:
    return await _fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))


async def get_active_jobs() -> List[aiosqlite.Row]:
    return await _fetchall(
        "SELECT * FROM jobs WHERE status IN ('queued', 'processing') ORDER BY created_at"
    )


async def get_recent_jobs(limit: int = 20) -> List[aiosqlite.Row]:
    return await _fetchall(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    )


async def count_user_extractions_last_hour(user_id: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    row = await _fetchone(
        "SELECT COUNT(*) as cnt FROM jobs WHERE user_id = ? AND created_at > ?",
        (user_id, cutoff),
    )
    return row["cnt"] if row else 0


# ── VIP requests ────────────────────────────────────────────
async def create_vip_request(user_id: int, username: Optional[str],
                             first_name: Optional[str], message: str) -> int:
    return await _execute(
        "INSERT INTO vip_requests (user_id, username, first_name, message, requested_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, username, first_name, message, _now()),
    )


async def get_pending_vip_requests() -> List[aiosqlite.Row]:
    return await _fetchall(
        "SELECT * FROM vip_requests WHERE status = 'pending' ORDER BY requested_at"
    )


async def action_vip_request(request_id: int, status: str,
                             duration_days: int, admin_id: int) -> Optional[aiosqlite.Row]:
    await _execute(
        "UPDATE vip_requests SET status = ?, duration_days = ?, actioned_at = ?, actioned_by = ? "
        "WHERE id = ?",
        (status, duration_days, _now(), admin_id, request_id),
    )
    return await _fetchone("SELECT * FROM vip_requests WHERE id = ?", (request_id,))


# ── Broadcasts ──────────────────────────────────────────────
async def create_broadcast(message: str, sent_by: int, recipient_count: int) -> int:
    return await _execute(
        "INSERT INTO broadcasts (message, sent_by, sent_at, recipient_count) VALUES (?, ?, ?, ?)",
        (message, sent_by, _now(), recipient_count),
    )


# ── Settings ────────────────────────────────────────────────
async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    row = await _fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    await _execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value),
    )


# ── Stats ───────────────────────────────────────────────────
async def global_stats() -> Dict[str, Any]:
    """Return aggregate statistics for admin panel."""
    db = await get_db()
    stats: Dict[str, Any] = {}

    row = await _fetchone("SELECT COUNT(*) as cnt FROM users")
    stats["total_users"] = row["cnt"] if row else 0

    row = await _fetchone("SELECT COUNT(*) as cnt FROM users WHERE is_vip = 1")
    stats["vip_users"] = row["cnt"] if row else 0

    row = await _fetchone("SELECT COUNT(*) as cnt FROM users WHERE is_banned = 1")
    stats["banned_users"] = row["cnt"] if row else 0

    today = datetime.now(timezone.utc).date().isoformat()
    row = await _fetchone(
        "SELECT COUNT(*) as cnt FROM users WHERE DATE(joined_at) = ?", (today,)
    )
    stats["new_users_today"] = row["cnt"] if row else 0

    row = await _fetchone(
        "SELECT COUNT(*) as cnt, "
        "COALESCE(SUM(cookies_found), 0) as cookies, "
        "COALESCE(SUM(file_size_bytes), 0) as bytes_proc "
        "FROM jobs WHERE DATE(created_at) = ?",
        (today,),
    )
    stats["extractions_today"] = row["cnt"] if row else 0
    stats["cookies_today"] = row["cookies"] if row else 0
    stats["bytes_today"] = row["bytes_proc"] if row else 0

    row = await _fetchone(
        "SELECT COUNT(*) as cnt, "
        "COALESCE(SUM(cookies_found), 0) as cookies, "
        "COALESCE(SUM(file_size_bytes), 0) as bytes_proc "
        "FROM jobs"
    )
    stats["total_extractions"] = row["cnt"] if row else 0
    stats["total_cookies"] = row["cookies"] if row else 0
    stats["total_bytes"] = row["bytes_proc"] if row else 0

    return stats


# ── Blacklisted domains ────────────────────────────────────
async def is_domain_blacklisted(domain: str) -> bool:
    val = await get_setting("blacklisted_domains")
    if not val:
        return False
    return domain.lower() in [d.strip().lower() for d in val.split(",")]


async def add_blacklisted_domain(domain: str) -> None:
    current = await get_setting("blacklisted_domains", "")
    domains = [d.strip() for d in (current or "").split(",") if d.strip()]
    if domain.lower() not in [d.lower() for d in domains]:
        domains.append(domain.lower())
    await set_setting("blacklisted_domains", ",".join(domains))
