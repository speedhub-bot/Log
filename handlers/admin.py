"""
Admin panel handlers — full control over users, VIP, jobs, settings, broadcasts.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
import time
from datetime import datetime, timezone

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from db import database as db
from services.queue import JobQueue
from utils.formatting import bytes_human, number_human, seconds_human

# Conversation states
BROADCAST_MSG = 0
CUSTOM_VIP_DAYS = 1
MSG_USER_TEXT = 2

_job_queue: JobQueue | None = None
_start_time: float = time.monotonic()


def _is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_ID


def _admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\U0001f465 Users", callback_data="adm_users_0"),
            InlineKeyboardButton("\U0001f4ca Stats", callback_data="adm_stats"),
        ],
        [
            InlineKeyboardButton("\U0001f4cb VIP Reqs", callback_data="adm_vipreqs"),
            InlineKeyboardButton("\U0001f4e2 Broadcast", callback_data="adm_broadcast"),
        ],
        [
            InlineKeyboardButton("\u2699\ufe0f Settings", callback_data="adm_settings"),
            InlineKeyboardButton("\U0001f4dc Logs", callback_data="adm_logs"),
        ],
        [
            InlineKeyboardButton("\U0001f527 Jobs", callback_data="adm_jobs"),
            InlineKeyboardButton("\U0001f6ab Banned", callback_data="adm_banned"),
        ],
    ])


# ── /admin ──────────────────────────────────────────────────
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_admin(user.id):
        return
    await update.message.reply_text(  # type: ignore[union-attr]
        "\u2699\ufe0f Admin Panel",
        reply_markup=_admin_kb(),
    )


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    user = update.effective_user
    if user is None or not _is_admin(user.id):
        return
    await query.edit_message_text("\u2699\ufe0f Admin Panel", reply_markup=_admin_kb())


# ── Users (paginated) ──────────────────────────────────────
async def users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    data = query.data or "adm_users_0"
    try:
        page = int(data.split("_")[-1])
    except (ValueError, IndexError):
        page = 0

    rows, total = await db.get_users_page(page)
    total_pages = max(1, (total + 9) // 10)

    lines = [f"\U0001f465 Users — Page {page + 1}/{total_pages} (total: {total})\n"]
    for r in rows:
        status = "VIP" if r["is_vip"] else ("Banned" if r["is_banned"] else "Free")
        lines.append(
            f"\u2022 {r['first_name'] or 'N/A'} | {r['user_id']} | {status} | "
            f"{r['total_extractions']} extractions"
        )

    buttons: list[list[InlineKeyboardButton]] = []
    # User detail buttons
    for r in rows:
        buttons.append([
            InlineKeyboardButton(
                f"\U0001f464 {r['first_name'] or r['user_id']}",
                callback_data=f"adm_user_{r['user_id']}",
            )
        ])
    # Pagination
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("\u25c0", callback_data=f"adm_users_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("\u25b6", callback_data=f"adm_users_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("\u25c0 Back", callback_data="adm_panel")])

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── User detail ────────────────────────────────────────────
async def user_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    try:
        uid = int((query.data or "").split("_")[-1])
    except (ValueError, IndexError):
        return

    row = await db.get_user(uid)
    if row is None:
        await query.edit_message_text("User not found.")
        return

    vip = await db.is_vip(uid)
    status = "VIP" if vip else ("Banned" if row["is_banned"] else "Free")

    text = (
        f"\U0001f464 User Detail\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"Name: {row['first_name']} (@{row['username']})\n"
        f"ID: {uid}\n"
        f"Status: {status}\n"
        f"Extractions: {row['total_extractions']}\n"
        f"Cookies found: {number_human(row['total_cookies_found'])}\n"
        f"Data processed: {bytes_human(row['total_bytes_processed'])}\n"
        f"Joined: {(row['joined_at'] or '')[:10]}\n"
        f"Last active: {(row['last_active'] or '')[:10]}"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\U0001f451 Grant VIP", callback_data=f"adm_gvip_{uid}"),
            InlineKeyboardButton("\U0001f6ab Ban", callback_data=f"adm_ban_{uid}"),
        ],
        [
            InlineKeyboardButton("\U0001f4ca Stats", callback_data=f"adm_ustats_{uid}"),
            InlineKeyboardButton("\u2709\ufe0f Message", callback_data=f"adm_msg_{uid}"),
        ],
        [
            InlineKeyboardButton("\u23f1 Set VIP Duration", callback_data=f"adm_gvip_{uid}"),
            InlineKeyboardButton("\U0001f513 Unban", callback_data=f"adm_unban_{uid}"),
        ],
        [
            InlineKeyboardButton("\u274c Revoke VIP", callback_data=f"adm_rvip_{uid}"),
            InlineKeyboardButton("\u25c0 Back", callback_data="adm_users_0"),
        ],
    ])
    await query.edit_message_text(text, reply_markup=kb)


# ── Quick admin actions (inline) ───────────────────────────
async def grant_vip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    try:
        uid = int((query.data or "").split("_")[-1])
    except (ValueError, IndexError):
        return

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("7 days", callback_data=f"adm_setvip_{uid}_7"),
            InlineKeyboardButton("30 days", callback_data=f"adm_setvip_{uid}_30"),
        ],
        [
            InlineKeyboardButton("90 days", callback_data=f"adm_setvip_{uid}_90"),
            InlineKeyboardButton("Forever", callback_data=f"adm_setvip_{uid}_0"),
        ],
        [InlineKeyboardButton("\u25c0 Back", callback_data=f"adm_user_{uid}")],
    ])
    await query.edit_message_text(f"Select VIP duration for {uid}:", reply_markup=kb)


async def set_vip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    parts = (query.data or "").split("_")
    try:
        uid = int(parts[-2])
        days = int(parts[-1])
    except (ValueError, IndexError):
        return

    await db.set_vip(uid, days)
    dur = "forever" if days == 0 else f"{days} days"
    await query.edit_message_text(f"\u2705 VIP granted to {uid} for {dur}.")

    try:
        await context.bot.send_message(
            uid,
            f"\U0001f389 You now have VIP access for {dur}!",
        )
    except Exception:
        pass


async def revoke_vip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return
    try:
        uid = int((query.data or "").split("_")[-1])
    except (ValueError, IndexError):
        return
    await db.revoke_vip(uid)
    await query.edit_message_text(f"\u2705 VIP revoked for {uid}.")


async def ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return
    try:
        uid = int((query.data or "").split("_")[-1])
    except (ValueError, IndexError):
        return
    await db.ban_user(uid, "Banned by admin")
    await query.edit_message_text(f"\u2705 User {uid} banned.")


async def unban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return
    try:
        uid = int((query.data or "").split("_")[-1])
    except (ValueError, IndexError):
        return
    await db.unban_user(uid)
    await query.edit_message_text(f"\u2705 User {uid} unbanned.")


# ── VIP request actions ────────────────────────────────────
async def vip_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    parts = (query.data or "").split("_")
    try:
        uid = int(parts[-2])
        days = int(parts[-1])
    except (ValueError, IndexError):
        return

    await db.set_vip(uid, days)
    # Update the VIP request record
    pending = await db.get_pending_vip_requests()
    for req in pending:
        if req["user_id"] == uid:
            await db.action_vip_request(
                req["id"], "approved", days, update.effective_user.id  # type: ignore[union-attr]
            )
            break

    dur = "forever" if days == 0 else f"{days} days"
    await query.edit_message_text(f"\u2705 VIP approved for {uid} ({dur}).")

    try:
        await context.bot.send_message(
            uid,
            f"\U0001f389 Your VIP request was approved!\nYou now have VIP access for {dur}.",
        )
    except Exception:
        pass


async def vip_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    try:
        uid = int((query.data or "").split("_")[-1])
    except (ValueError, IndexError):
        return

    pending = await db.get_pending_vip_requests()
    for req in pending:
        if req["user_id"] == uid:
            await db.action_vip_request(
                req["id"], "rejected", 0, update.effective_user.id  # type: ignore[union-attr]
            )
            break

    await query.edit_message_text(f"\u274c VIP request rejected for {uid}.")

    try:
        await context.bot.send_message(
            uid,
            "\u274c Your VIP request was reviewed and not approved at this time.",
        )
    except Exception:
        pass


async def vip_custom_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return ConversationHandler.END

    try:
        uid = int((query.data or "").split("_")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END

    context.user_data["vip_custom_uid"] = uid  # type: ignore[index]
    await query.edit_message_text(f"Enter number of VIP days for user {uid}:")
    return CUSTOM_VIP_DAYS


async def vip_custom_days_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None:
        return ConversationHandler.END
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return ConversationHandler.END

    uid = context.user_data.get("vip_custom_uid")  # type: ignore[union-attr]
    if uid is None:
        return ConversationHandler.END

    try:
        days = int(update.message.text or "0")
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return CUSTOM_VIP_DAYS

    await db.set_vip(uid, days)
    dur = "forever" if days == 0 else f"{days} days"
    await update.message.reply_text(f"\u2705 VIP granted to {uid} for {dur}.")

    try:
        await context.bot.send_message(
            uid,
            f"\U0001f389 Your VIP request was approved!\nYou now have VIP access for {dur}.",
        )
    except Exception:
        pass
    return ConversationHandler.END


# ── VIP requests list ──────────────────────────────────────
async def vipreqs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    reqs = await db.get_pending_vip_requests()
    if not reqs:
        await query.edit_message_text(
            "No pending VIP requests.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u25c0 Back", callback_data="adm_panel")]
            ]),
        )
        return

    for req in reqs[:10]:
        text = (
            f"\U0001f451 VIP Request\n"
            f"\U0001f464 {req['first_name']} (@{req['username']})\n"
            f"\U0001f194 {req['user_id']}\n"
            f"\U0001f4ac {req['message']}\n"
            f"\U0001f552 {req['requested_at']}"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("\u2705 7d", callback_data=f"vip_approve_{req['user_id']}_7"),
                InlineKeyboardButton("\u2705 30d", callback_data=f"vip_approve_{req['user_id']}_30"),
            ],
            [
                InlineKeyboardButton("\u2705 Forever", callback_data=f"vip_approve_{req['user_id']}_0"),
                InlineKeyboardButton("\u23f1 Custom", callback_data=f"vip_custom_{req['user_id']}"),
            ],
            [InlineKeyboardButton("\u274c Reject", callback_data=f"vip_reject_{req['user_id']}")],
        ])
        await context.bot.send_message(update.effective_user.id, text, reply_markup=kb)  # type: ignore[union-attr]


# ── Stats ──────────────────────────────────────────────────
async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    s = await db.global_stats()
    uptime = seconds_human(time.monotonic() - _start_time)
    disk = shutil.disk_usage("/")

    active = _job_queue.active_count if _job_queue else 0
    pending = _job_queue.pending_count if _job_queue else 0

    text = (
        f"\U0001f4ca Bot Statistics\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f465 Total users: {s['total_users']:,}\n"
        f"\U0001f451 VIP users: {s['vip_users']}\n"
        f"\U0001f6ab Banned users: {s['banned_users']}\n\n"
        f"\U0001f4c8 Today:\n"
        f"\u2022 New users: {s['new_users_today']}\n"
        f"\u2022 Extractions: {s['extractions_today']}\n"
        f"\u2022 Cookies found: {number_human(s['cookies_today'])}\n"
        f"\u2022 Data processed: {bytes_human(s['bytes_today'])}\n\n"
        f"\U0001f4c8 All Time:\n"
        f"\u2022 Total extractions: {s['total_extractions']:,}\n"
        f"\u2022 Total cookies: {number_human(s['total_cookies'])}\n"
        f"\u2022 Total data: {bytes_human(s['total_bytes'])}\n\n"
        f"\u2699\ufe0f System:\n"
        f"\u2022 Active jobs: {active}/{config.MAX_CONCURRENT_JOBS}\n"
        f"\u2022 Queue: {pending} waiting\n"
        f"\u2022 Uptime: {uptime}\n"
        f"\u2022 Disk free: {bytes_human(disk.free)}"
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\u25c0 Back", callback_data="adm_panel")]
        ]),
    )


# ── Broadcast ──────────────────────────────────────────────
async def broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return ConversationHandler.END
    await query.edit_message_text("Enter your broadcast message:")
    return BROADCAST_MSG


async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None:
        return ConversationHandler.END
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return ConversationHandler.END

    text = update.message.text or ""
    user_ids = await db.get_all_user_ids()
    total = len(user_ids)
    sent = 0
    failed = 0
    status_msg = await update.message.reply_text(f"Sending... 0/{total}")

    for i, uid in enumerate(user_ids, 1):
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
        if i % 50 == 0:
            try:
                await status_msg.edit_text(f"Sending... {i}/{total}")
            except Exception:
                pass

    await db.create_broadcast(text, update.effective_user.id, sent)  # type: ignore[union-attr]
    await status_msg.edit_text(
        f"\u2705 Sent to {sent} users ({failed} failed)",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\u25c0 Back", callback_data="adm_panel")]
        ]),
    )
    return ConversationHandler.END


# ── Settings ───────────────────────────────────────────────
async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    maintenance = await db.get_setting("maintenance", "0")
    maint_status = "ON" if maintenance == "1" else "OFF"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Maintenance: {maint_status}", callback_data="adm_toggle_maint")],
        [InlineKeyboardButton("Clear Temp Files", callback_data="adm_clear_temp")],
        [InlineKeyboardButton("\u25c0 Back", callback_data="adm_panel")],
    ])
    await query.edit_message_text("\u2699\ufe0f Bot Settings", reply_markup=kb)


async def toggle_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    current = await db.get_setting("maintenance", "0")
    new_val = "0" if current == "1" else "1"
    await db.set_setting("maintenance", new_val)
    status = "ON" if new_val == "1" else "OFF"
    await query.edit_message_text(
        f"\u2705 Maintenance mode: {status}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\u25c0 Back", callback_data="adm_settings")]
        ]),
    )


async def clear_temp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    temp = str(config.TEMP_DIR)
    if os.path.isdir(temp):
        shutil.rmtree(temp, ignore_errors=True)
        os.makedirs(temp, exist_ok=True)
    await query.edit_message_text(
        "\u2705 Temp files cleared.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("\u25c0 Back", callback_data="adm_settings")]
        ]),
    )


# ── Logs ───────────────────────────────────────────────────
async def logs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    log_path = config.LOG_FILE
    if not os.path.exists(log_path):
        await query.edit_message_text("No log file found.")
        return

    # Read last 50 lines
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-50:]
        text = "".join(lines)
        if len(text) > 4000:
            text = text[-4000:]
        await query.edit_message_text(
            f"\U0001f4dc Last 50 log lines:\n\n{text}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u2b07\ufe0f Download Full Log", callback_data="adm_dl_log")],
                [InlineKeyboardButton("\u25c0 Back", callback_data="adm_panel")],
            ]),
        )
    except Exception as e:
        await query.edit_message_text(f"Error reading logs: {e}")


async def download_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    log_path = config.LOG_FILE
    if os.path.exists(log_path):
        with open(log_path, "rb") as fh:
            await context.bot.send_document(
                chat_id=update.effective_user.id,  # type: ignore[union-attr]
                document=fh,
                filename="bot.log",
            )


# ── Jobs ───────────────────────────────────────────────────
async def jobs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    active = await db.get_active_jobs()
    recent = await db.get_recent_jobs(10)

    lines = ["\U0001f527 Jobs\n"]
    if active:
        lines.append("Active:")
        for j in active:
            lines.append(
                f"  #{j['id']} | {j['user_id']} | {j['domain']} | "
                f"{j['status']} | {bytes_human(j['file_size_bytes'])}"
            )
    else:
        lines.append("No active jobs.\n")

    lines.append("\nRecent:")
    for j in recent[:10]:
        lines.append(
            f"  #{j['id']} | {j['user_id']} | {j['domain']} | "
            f"{j['status']} | {j['cookies_found'] or 0} cookies"
        )

    buttons: list[list[InlineKeyboardButton]] = []
    for j in active:
        buttons.append([
            InlineKeyboardButton(
                f"\U0001f6d1 Kill #{j['id']}",
                callback_data=f"cancel_job_{j['id']}",
            )
        ])
    buttons.append([InlineKeyboardButton("\u25c0 Back", callback_data="adm_panel")])

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Banned users ───────────────────────────────────────────
async def banned_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return

    db_conn = await db.get_db()
    async with db_conn.execute(
        "SELECT * FROM users WHERE is_banned = 1 ORDER BY last_active DESC LIMIT 20"
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        await query.edit_message_text(
            "No banned users.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\u25c0 Back", callback_data="adm_panel")]
            ]),
        )
        return

    lines = ["\U0001f6ab Banned Users\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    for r in rows:
        lines.append(f"\u2022 {r['first_name']} ({r['user_id']}) — {r['ban_reason']}")
        buttons.append([
            InlineKeyboardButton(f"\U0001f513 Unban {r['user_id']}", callback_data=f"adm_unban_{r['user_id']}")
        ])
    buttons.append([InlineKeyboardButton("\u25c0 Back", callback_data="adm_panel")])

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Message a user (admin) ─────────────────────────────────
async def msg_user_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None:
        return ConversationHandler.END
    await query.answer()
    if not _is_admin(update.effective_user.id):  # type: ignore[union-attr]
        return ConversationHandler.END
    try:
        uid = int((query.data or "").split("_")[-1])
    except (ValueError, IndexError):
        return ConversationHandler.END
    context.user_data["msg_target_uid"] = uid  # type: ignore[index]
    await query.edit_message_text(f"Enter message to send to user {uid}:")
    return MSG_USER_TEXT


async def msg_user_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message is None:
        return ConversationHandler.END
    uid = context.user_data.get("msg_target_uid")  # type: ignore[union-attr]
    if uid is None:
        return ConversationHandler.END
    try:
        await context.bot.send_message(uid, update.message.text or "")
        await update.message.reply_text(f"\u2705 Message sent to {uid}.")
    except Exception as e:
        await update.message.reply_text(f"\u274c Failed: {e}")
    return ConversationHandler.END


# ── /debug ─────────────────────────────────────────────────
async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_admin(user.id):
        return

    import importlib
    import telegram
    try:
        import telethon
        tl_ver = telethon.__version__
    except Exception:
        tl_ver = "N/A"

    disk = shutil.disk_usage("/")
    try:
        import resource
        mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        mem_str = f"{mem:.0f} MB"
    except Exception:
        mem_str = "N/A"

    temp_files = 0
    temp_size = 0
    if os.path.isdir(str(config.TEMP_DIR)):
        for root, dirs, files in os.walk(str(config.TEMP_DIR)):
            for f in files:
                temp_files += 1
                try:
                    temp_size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass

    active = _job_queue.active_count if _job_queue else 0
    pending = _job_queue.pending_count if _job_queue else 0
    uptime = seconds_human(time.monotonic() - _start_time)

    text = (
        f"\U0001f527 Debug Info\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"Python: {platform.python_version()}\n"
        f"PTB: {telegram.__version__}\n"
        f"Telethon: {tl_ver}\n\n"
        f"Memory: {mem_str}\n"
        f"Disk: {bytes_human(disk.free)} free / {bytes_human(disk.total)} total\n"
        f"Temp files: {temp_files} ({bytes_human(temp_size)})\n\n"
        f"Active jobs: {active}\n"
        f"Queue size: {pending}\n"
        f"Uptime: {uptime}"
    )
    await update.message.reply_text(text)  # type: ignore[union-attr]


# ── Text commands ──────────────────────────────────────────
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_admin(user.id) or update.message is None:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: /ban <user_id> <reason>")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    reason = " ".join(args[1:])
    await db.ban_user(uid, reason)
    await update.message.reply_text(f"\u2705 User {uid} banned: {reason}")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_admin(user.id) or update.message is None:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    await db.unban_user(uid)
    await update.message.reply_text(f"\u2705 User {uid} unbanned.")


async def vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_admin(user.id) or update.message is None:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: /vip <user_id> <days> (0 = forever)")
        return
    try:
        uid = int(args[0])
        days = int(args[1])
    except ValueError:
        await update.message.reply_text("Invalid arguments.")
        return
    await db.set_vip(uid, days)
    dur = "forever" if days == 0 else f"{days} days"
    await update.message.reply_text(f"\u2705 VIP granted to {uid} for {dur}.")


async def revokevip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_admin(user.id) or update.message is None:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /revokevip <user_id>")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    await db.revoke_vip(uid)
    await update.message.reply_text(f"\u2705 VIP revoked for {uid}.")


async def setlimit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_admin(user.id) or update.message is None:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: /setlimit <user_id> <gb>")
        return
    try:
        uid = int(args[0])
        gb = int(args[1])
    except ValueError:
        await update.message.reply_text("Invalid arguments.")
        return
    await db.set_custom_limit(uid, gb)
    await update.message.reply_text(f"\u2705 Custom limit set: {uid} -> {gb} GB/day.")


async def msg_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_admin(user.id) or update.message is None:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: /msg <user_id> <message>")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return
    msg = " ".join(args[1:])
    try:
        await context.bot.send_message(uid, msg)
        await update.message.reply_text(f"\u2705 Message sent to {uid}.")
    except Exception as e:
        await update.message.reply_text(f"\u274c Failed: {e}")


async def addquota_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or not _is_admin(user.id) or update.message is None:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: /addquota <user_id> <gb>")
        return
    try:
        uid = int(args[0])
        gb = int(args[1])
    except ValueError:
        await update.message.reply_text("Invalid arguments.")
        return
    await db.add_quota(uid, gb * 1024 ** 3)
    await update.message.reply_text(f"\u2705 Added {gb} GB quota to {uid}.")


# ── Register ───────────────────────────────────────────────
def register(app, job_queue: JobQueue) -> None:
    """Attach admin handlers to the Application."""
    global _job_queue
    _job_queue = job_queue

    # Broadcast conversation
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(broadcast_entry, pattern="^adm_broadcast$")],
        states={
            BROADCAST_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_send)],
        },
        fallbacks=[],
        per_message=False,
    )
    app.add_handler(broadcast_conv)

    # VIP custom duration conversation
    vip_custom_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(vip_custom_callback, pattern=r"^vip_custom_\d+$")],
        states={
            CUSTOM_VIP_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, vip_custom_days_received)],
        },
        fallbacks=[],
        per_message=False,
    )
    app.add_handler(vip_custom_conv)

    # Admin message user conversation
    msg_user_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(msg_user_entry, pattern=r"^adm_msg_\d+$")],
        states={
            MSG_USER_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_user_send)],
        },
        fallbacks=[],
        per_message=False,
    )
    app.add_handler(msg_user_conv)

    # Commands
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("debug", debug_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("vip", vip_command))
    app.add_handler(CommandHandler("revokevip", revokevip_command))
    app.add_handler(CommandHandler("setlimit", setlimit_command))
    app.add_handler(CommandHandler("msg", msg_command))
    app.add_handler(CommandHandler("addquota", addquota_command))

    # Callbacks
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^adm_panel$"))
    app.add_handler(CallbackQueryHandler(users_callback, pattern=r"^adm_users_\d+$"))
    app.add_handler(CallbackQueryHandler(user_detail_callback, pattern=r"^adm_user_\d+$"))
    app.add_handler(CallbackQueryHandler(grant_vip_callback, pattern=r"^adm_gvip_\d+$"))
    app.add_handler(CallbackQueryHandler(set_vip_callback, pattern=r"^adm_setvip_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(revoke_vip_callback, pattern=r"^adm_rvip_\d+$"))
    app.add_handler(CallbackQueryHandler(ban_callback, pattern=r"^adm_ban_\d+$"))
    app.add_handler(CallbackQueryHandler(unban_callback, pattern=r"^adm_unban_\d+$"))
    app.add_handler(CallbackQueryHandler(vipreqs_callback, pattern="^adm_vipreqs$"))
    app.add_handler(CallbackQueryHandler(stats_callback, pattern="^adm_stats$"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^adm_settings$"))
    app.add_handler(CallbackQueryHandler(toggle_maintenance, pattern="^adm_toggle_maint$"))
    app.add_handler(CallbackQueryHandler(clear_temp, pattern="^adm_clear_temp$"))
    app.add_handler(CallbackQueryHandler(logs_callback, pattern="^adm_logs$"))
    app.add_handler(CallbackQueryHandler(download_log, pattern="^adm_dl_log$"))
    app.add_handler(CallbackQueryHandler(jobs_callback, pattern="^adm_jobs$"))
    app.add_handler(CallbackQueryHandler(banned_callback, pattern="^adm_banned$"))
    app.add_handler(CallbackQueryHandler(vip_approve_callback, pattern=r"^vip_approve_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(vip_reject_callback, pattern=r"^vip_reject_\d+$"))
    app.add_handler(CallbackQueryHandler(user_detail_callback, pattern=r"^adm_ustats_\d+$"))
