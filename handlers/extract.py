"""
Extraction conversation handler — the core user workflow.

States:
  DOMAIN  -> user types a domain
  FILE    -> user uploads an archive
  (processing + results happen automatically)
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from typing import Dict, Optional

from loguru import logger
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from db import database as db
from services.downloader import download_file, download_from_url
from services.extractor import (
    ExtractionProgress,
    guess_archive_password_async,
    probe_encrypted_entries_async,
    run_extraction_async,
)
from services.queue import JobQueue, QueueItem
from utils.formatting import bytes_human, progress_bar, seconds_human, time_until
from utils.validators import validate_archive, validate_domain

# Conversation states
DOMAIN, FILE = range(2)

# Module-level job queue (initialised in register())
_job_queue: JobQueue | None = None

# Active progress trackers: job_id -> ExtractionProgress
_active_progress: Dict[int, ExtractionProgress] = {}

# Pending password requests: user_id -> Future that the user's next plain
# text message (or /skip command) resolves. Value is the password string,
# or None if the user chose /skip (extract only unencrypted entries).
_pending_passwords: Dict[int, "asyncio.Future[Optional[str]]"] = {}

# How long to wait for the user to reply with a password before we
# auto-skip and proceed with ``-p-``. Keeps stuck jobs from pinning a
# queue worker forever.
PASSWORD_PROMPT_TIMEOUT = 300.0  # 5 minutes


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u274c Cancel", callback_data="extract_cancel")]
    ])


def _cancel_job_kb(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f6d1 Cancel Job", callback_data=f"cancel_job_{job_id}")]
    ])


# ── Entry: ask for domain ──────────────────────────────────
async def extract_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Called via /extract command or the Extract button."""
    user = update.effective_user
    if user is None:
        return ConversationHandler.END

    row = await db.ensure_user(user.id, user.username, user.first_name)
    if row["is_banned"]:
        text = f"\U0001f6ab You are banned.\nReason: {row['ban_reason'] or 'N/A'}"
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)  # type: ignore[union-attr]
        return ConversationHandler.END

    # Maintenance check
    if await db.get_setting("maintenance") == "1" and user.id != config.ADMIN_ID:
        text = "\U0001f527 Bot is under maintenance. Please check back later."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)  # type: ignore[union-attr]
        return ConversationHandler.END

    is_admin = user.id == config.ADMIN_ID

    # Rate limit check (admin bypasses)
    count = await db.count_user_extractions_last_hour(user.id)
    if count >= config.MAX_EXTRACTIONS_PER_HOUR and not is_admin:
        text = (
            f"\u26a0\ufe0f Rate limit reached ({config.MAX_EXTRACTIONS_PER_HOUR}/hour).\n"
            "Please wait before starting another extraction."
        )
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)  # type: ignore[union-attr]
        return ConversationHandler.END

    text = (
        "\U0001f310 Enter the domain to extract cookies for:\n"
        "Example: spotify.com, netflix.com"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=_cancel_kb())
    else:
        await update.message.reply_text(text, reply_markup=_cancel_kb())  # type: ignore[union-attr]
    return DOMAIN


# ── State: DOMAIN ──────────────────────────────────────────
async def domain_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate domain and move to FILE state."""
    user = update.effective_user
    if user is None or update.message is None:
        return ConversationHandler.END

    raw = (update.message.text or "").strip()
    valid, result = validate_domain(raw)
    if not valid:
        await update.message.reply_text(f"\u274c {result}", reply_markup=_cancel_kb())
        return DOMAIN

    # Check blacklist
    if await db.is_domain_blacklisted(result):
        await update.message.reply_text(
            "\u274c This domain is blacklisted.", reply_markup=_cancel_kb()
        )
        return DOMAIN

    context.user_data["extract_domain"] = result  # type: ignore[index]

    is_admin = user.id == config.ADMIN_ID
    remaining = await db.get_remaining_quota(user.id)
    vip = await db.is_vip(user.id)
    if is_admin or vip:
        limit_text = "Unlimited"
    else:
        limit_text = bytes_human(remaining)
    if is_admin:
        max_file = "Unlimited"
    elif vip:
        max_file = "10 GB"
    else:
        max_file = "2 GB"

    text = (
        f"\U0001f4c1 Now send your archive file — OR paste a direct "
        f"download URL (.zip / .rar).\n"
        f"Supported: .zip .rar .7z .tar.gz\n"
        f"Your limit: {limit_text} remaining today\n"
        f"Max file size: {max_file}"
    )
    await update.message.reply_text(text, reply_markup=_cancel_kb())
    return FILE


# ── State: FILE ────────────────────────────────────────────
async def file_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate file, check quota, enqueue job."""
    user = update.effective_user
    if user is None or update.message is None:
        return ConversationHandler.END

    doc = update.message.document
    if doc is None:
        await update.message.reply_text(
            "\u274c Please send an archive file.", reply_markup=_cancel_kb()
        )
        return FILE

    # Validate file type
    valid, ext_or_err = validate_archive(doc.file_name, doc.mime_type)
    if not valid:
        await update.message.reply_text(f"\u274c {ext_or_err}", reply_markup=_cancel_kb())
        return FILE

    file_size = doc.file_size or 0
    is_admin = user.id == config.ADMIN_ID
    vip = await db.is_vip(user.id)

    # Max file size check (admin bypasses)
    if not is_admin:
        max_bytes = config.VIP_MAX_FILE_BYTES if vip else config.FREE_MAX_FILE_BYTES
        if file_size > max_bytes:
            await update.message.reply_text(
                f"\u274c File too large ({bytes_human(file_size)}).\n"
                f"Max: {bytes_human(max_bytes)}\n\n"
                "\U0001f451 Get VIP for higher limits!",
                reply_markup=_cancel_kb(),
            )
            return FILE

    # Quota check (admin bypasses)
    if not is_admin:
        remaining = await db.get_remaining_quota(user.id)
        if remaining != -1 and file_size > remaining:
            await update.message.reply_text(
                f"\u274c Daily quota exceeded!\n"
                f"Used: {bytes_human(config.FREE_DAILY_LIMIT_BYTES - remaining)} / "
                f"{bytes_human(config.FREE_DAILY_LIMIT_BYTES)}\n"
                f"Resets in: {time_until((await db.get_user(user.id))['daily_reset_at'])}\n\n"  # type: ignore[index]
                "\U0001f451 Get VIP for unlimited access!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("\U0001f451 Get VIP", callback_data="getvip")],
                    [InlineKeyboardButton("\u274c Cancel", callback_data="extract_cancel")],
                ]),
            )
            return ConversationHandler.END

    domain = context.user_data.get("extract_domain", "unknown")  # type: ignore[union-attr]

    # Consume quota (no-op for admin)
    if not is_admin:
        await db.consume_quota(user.id, file_size)

    # Create DB job
    job_id = await db.create_job(user.id, domain, doc.file_name or "archive", file_size)

    # Send initial progress message
    progress_msg = await update.message.reply_text(
        "\u23f3 Queued for processing...",
        reply_markup=_cancel_job_kb(job_id),
    )

    # Build the async worker
    progress = ExtractionProgress()
    _active_progress[job_id] = progress

    source_ref = update.message  # Telegram document source

    async def _worker() -> None:
        await _process_job(
            update, context, job_id, user.id, domain,
            source_ref, progress_msg, progress,
        )

    # Enqueue
    is_vip_flag = await db.is_vip(user.id)
    item = QueueItem(
        priority=0 if (is_vip_flag or is_admin) else 1,
        job_id=job_id,
        user_id=user.id,
        is_vip=is_vip_flag,
        coro_factory=_worker,
    )
    assert _job_queue is not None
    pos = await _job_queue.enqueue(item)

    if pos > 0:
        await progress_msg.edit_text(
            f"\u23f3 You are #{pos + 1} in queue.\n"
            f"Estimated wait: ~{pos * 4} minutes",
            reply_markup=_cancel_job_kb(job_id),
        )

    return ConversationHandler.END


_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


async def url_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Accept a direct download URL in the FILE state as an alternative
    to uploading a document. The URL is downloaded by the queue worker;
    we just enqueue the job here.
    """
    user = update.effective_user
    if user is None or update.message is None:
        return FILE

    raw = (update.message.text or "").strip()
    if not _URL_RE.match(raw):
        await update.message.reply_text(
            "\u274c That doesn't look like a direct URL.\n"
            "Send an archive file or paste an http(s) link to a .zip/.rar.",
            reply_markup=_cancel_kb(),
        )
        return FILE

    # Basic extension sanity check (HEAD probe is done by the worker).
    lower = raw.split("?", 1)[0].lower()
    if not any(lower.endswith(ext) for ext in (".zip", ".rar", ".7z", ".tar.gz", ".tgz")):
        # Not fatal — CDN redirects often have no extension. Just warn.
        logger.info("URL has no archive extension, trusting server: {}", raw)

    is_admin = user.id == config.ADMIN_ID
    vip = await db.is_vip(user.id)
    domain = context.user_data.get("extract_domain", "unknown")  # type: ignore[union-attr]

    # Assume unknown size for URLs; the worker will enforce caps against
    # the real content-length it sees during download.
    file_name = raw.split("?")[0].rstrip("/").split("/")[-1] or "archive"
    job_id = await db.create_job(user.id, domain, file_name, 0)

    progress_msg = await update.message.reply_text(
        "\u23f3 Queued for download...",
        reply_markup=_cancel_job_kb(job_id),
    )

    progress = ExtractionProgress()
    _active_progress[job_id] = progress

    async def _worker() -> None:
        await _process_job(
            update, context, job_id, user.id, domain,
            ("url", raw, file_name), progress_msg, progress,
        )

    item = QueueItem(
        priority=0 if (vip or is_admin) else 1,
        job_id=job_id,
        user_id=user.id,
        is_vip=vip,
        coro_factory=_worker,
    )
    assert _job_queue is not None
    pos = await _job_queue.enqueue(item)
    if pos > 0:
        await progress_msg.edit_text(
            f"\u23f3 You are #{pos + 1} in queue.\n"
            f"Estimated wait: ~{pos * 4} minutes",
            reply_markup=_cancel_job_kb(job_id),
        )

    return ConversationHandler.END


async def _process_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    job_id: int,
    user_id: int,
    domain: str,
    original_msg,
    progress_msg,
    progress: ExtractionProgress,
) -> None:
    """Download, extract, send results — runs inside the queue worker."""
    start_ts = time.monotonic()
    temp_dir = tempfile.mkdtemp(dir=str(config.TEMP_DIR))
    result = None  # set before try so finally can reference it safely

    try:
        await db.update_job(job_id, status="processing", started_at=db._now())

        # Start progress updater
        updater_task = asyncio.create_task(
            _progress_updater(progress_msg, job_id, progress)
        )

        # Download — either from a Telegram document (original_msg is the
        # Message) or from a direct URL (tuple: ("url", url, name)).
        if isinstance(original_msg, tuple) and original_msg and original_msg[0] == "url":
            _, url_value, name_hint = original_msg
            archive_path = await download_from_url(
                url_value, temp_dir, progress, file_name_hint=name_hint,
            )
        else:
            archive_path = await download_file(original_msg, temp_dir, progress)

        # Password-protected entry probe. If the archive contains
        # encrypted entries, ask the user for the password before we
        # kick off extraction — otherwise those entries would be
        # skipped silently and the cookies inside them would be lost.
        password = await _maybe_prompt_for_password(
            context, user_id, archive_path, progress_msg, job_id,
        )

        # Extract
        result = await run_extraction_async(
            archive_path, domain, progress, password=password,
        )

        updater_task.cancel()
        try:
            await updater_task
        except asyncio.CancelledError:
            pass

        # Hard failure (no partial output to ship).
        if not result.success and not result.output_files:
            duration = time.monotonic() - start_ts
            await db.update_job(
                job_id,
                status="cancelled" if result.partial else "failed",
                error_message=result.error,
                completed_at=db._now(),
                duration_seconds=duration,
            )
            await progress_msg.edit_text(
                ("\u26a0\ufe0f Cancelled: " if result.partial else "\u274c Extraction failed: ")
                + (result.error or "unknown error"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("\U0001f50d Try Again", callback_data="extract"),
                     InlineKeyboardButton("\U0001f3e0 Home", callback_data="home")],
                ]),
            )
            if not result.partial:
                _notify_admin_error(context, user_id, "extraction", result.error)
            return

        # Update DB — success path (or cancelled with partial output)
        duration = time.monotonic() - start_ts
        await db.update_job(
            job_id,
            status="cancelled" if result.partial else "done",
            cookies_found=result.cookies_found,
            files_scanned=result.files_scanned,
            completed_at=db._now(),
            duration_seconds=duration,
            error_message="Cancelled \u2014 partial results" if result.partial else None,
        )
        await db.increment_user_stats(
            user_id, result.cookies_found,
            (await db.get_job(job_id))["file_size_bytes"],  # type: ignore[index]
        )

        # Send result files
        for fpath in result.output_files:
            try:
                file_size = os.path.getsize(fpath)
                if file_size > 0:
                    with open(fpath, "rb") as fh:
                        caption = (
                            "\u26a0\ufe0f Partial results (job cancelled)"
                            if result.partial else None
                        )
                        await context.bot.send_document(
                            chat_id=user_id,
                            document=fh,
                            filename=os.path.basename(fpath),
                            caption=caption,
                        )
            except Exception:
                logger.exception("Failed to send result file {}", fpath)

        job_row = await db.get_job(job_id)
        file_size = job_row["file_size_bytes"] if job_row else 0  # type: ignore[index]

        # Summary
        header = (
            "\u26a0\ufe0f Cancelled \u2014 partial results delivered"
            if result.partial else "\u2705 Extraction Complete!"
        )
        summary = (
            f"{header}\n\n"
            f"\U0001f310 Domain: {domain}\n"
            f"\U0001f36a Cookies found: {result.cookies_found:,}\n"
            f"\U0001f4c1 Files scanned: {result.files_scanned:,}\n"
            f"\U0001f4e6 Archive size: {bytes_human(file_size)}\n"
            f"\u23f1 Time taken: {seconds_human(duration)}\n"
            f"\U0001f4c4 Output files: {len(result.output_files)}\n\n"
            f"\U0001f338 Credits: @akaza_isnt"
        )
        await progress_msg.edit_text(
            summary,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("\U0001f50d Extract Again", callback_data="extract"),
                    InlineKeyboardButton("\U0001f4ca My Stats", callback_data="mystats"),
                ],
                [InlineKeyboardButton("\U0001f3e0 Home", callback_data="home")],
            ]),
        )

    except Exception as exc:
        logger.exception("Job {} failed unexpectedly", job_id)
        await db.update_job(
            job_id, status="failed", error_message=str(exc),
            completed_at=db._now(),
            duration_seconds=time.monotonic() - start_ts,
        )
        try:
            await progress_msg.edit_text(f"\u274c Error: {exc}")
        except Exception:
            pass
        _notify_admin_error(context, user_id, "job processing", str(exc))
    finally:
        _active_progress.pop(job_id, None)
        shutil.rmtree(temp_dir, ignore_errors=True)
        # Clean output dir created by the extractor
        if result and result.output_files:
            for fpath in result.output_files:
                parent = os.path.dirname(fpath)
                if parent and os.path.isdir(parent):
                    shutil.rmtree(parent, ignore_errors=True)
                    break  # all chunks share the same output dir


async def _maybe_prompt_for_password(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    archive_path: str,
    progress_msg,
    job_id: int,
) -> "str | None":
    """Probe *archive_path* for encrypted entries. If any exist, ask the
    user for the archive password in chat and wait for their reply.

    Returns the password to use for extraction, or ``None`` to proceed
    without one (user chose /skip or didn't reply in time).
    """
    try:
        encrypted = await probe_encrypted_entries_async(archive_path)
    except Exception:
        logger.exception("Password probe failed on {}", archive_path)
        return None

    if not encrypted:
        return None

    # --- Auto-guess before prompting the user ----------------------
    # Most stealer-log dumps are locked with a common password (1234,
    # the channel @handle, etc). Try the candidate list silently — if
    # anything hits we extract with no user intervention.
    try:
        await progress_msg.edit_text(
            "\U0001f510 Encrypted archive detected — "
            "trying common passwords\u2026",
            reply_markup=_cancel_job_kb(job_id),
        )
    except Exception:
        pass

    try:
        guessed = await guess_archive_password_async(archive_path)
    except Exception:
        logger.exception("Password auto-guess crashed on {}", archive_path)
        guessed = None

    if guessed is not None:
        try:
            await progress_msg.edit_text(
                f"\U0001f513 Password auto-detected: <code>{guessed}</code>\n"
                "Extracting\u2026",
                parse_mode="HTML",
                reply_markup=_cancel_job_kb(job_id),
            )
        except Exception:
            pass
        return guessed

    # Show up to three sample names so the user knows what's locked.
    sample = ", ".join(encrypted[:3])
    if len(encrypted) > 3:
        sample += f", +{len(encrypted) - 3} more"
    text = (
        f"\U0001f510 This archive has {len(encrypted)} password-protected "
        f"file(s):\n<code>{sample}</code>\n\n"
        "I couldn't auto-guess the password. Reply with the archive "
        "password to extract everything, or tap <b>Skip</b> to extract "
        "only the unencrypted files."
    )
    try:
        await progress_msg.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "\u23ed Skip encrypted", callback_data=f"skip_pw_{job_id}"
                ),
                InlineKeyboardButton(
                    "\u274c Cancel Job", callback_data=f"cancel_job_{job_id}"
                ),
            ]]),
        )
    except Exception:
        logger.exception("Failed to edit progress msg for password prompt")

    # Create the waiter and let the catch-all message handler fill it.
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Optional[str]] = loop.create_future()
    # Replace any previous pending request for this user — last one wins.
    prev = _pending_passwords.get(user_id)
    if prev is not None and not prev.done():
        prev.cancel()
    _pending_passwords[user_id] = fut

    try:
        password = await asyncio.wait_for(fut, timeout=PASSWORD_PROMPT_TIMEOUT)
    except asyncio.TimeoutError:
        logger.info(
            "Password prompt timed out for user {} job {}; proceeding without",
            user_id, job_id,
        )
        password = None
        try:
            await progress_msg.edit_text(
                "\u23f3 No password received \u2014 extracting only the "
                "unencrypted files\u2026",
                reply_markup=_cancel_job_kb(job_id),
            )
        except Exception:
            pass
    except asyncio.CancelledError:
        password = None
    finally:
        _pending_passwords.pop(user_id, None)

    if password is None:
        try:
            await progress_msg.edit_text(
                "\u23ed Skipping encrypted entries \u2014 extracting the rest\u2026",
                reply_markup=_cancel_job_kb(job_id),
            )
        except Exception:
            pass
    else:
        try:
            await progress_msg.edit_text(
                "\U0001f511 Password received \u2014 extracting\u2026",
                reply_markup=_cancel_job_kb(job_id),
            )
        except Exception:
            pass
    return password


async def password_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fill a pending password request with the user's next text message."""
    user = update.effective_user
    if user is None or update.message is None:
        return
    fut = _pending_passwords.get(user.id)
    if fut is None or fut.done():
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    # ``/skip`` as a plain word doubles as a shortcut to the skip flow.
    if text.lower() in ("/skip", "skip"):
        fut.set_result(None)
    else:
        fut.set_result(text)
    # Try to delete the message so the password doesn't linger in chat.
    try:
        await update.message.delete()
    except Exception:
        pass
    # Stop other handlers (e.g. an active /extract conversation state)
    # from also consuming the same message.
    raise ApplicationHandlerStop


async def skip_password_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handle the inline 'Skip encrypted' button."""
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    await query.answer()
    fut = _pending_passwords.get(update.effective_user.id)
    if fut is not None and not fut.done():
        fut.set_result(None)


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /skip command as an alternative to the inline button."""
    if update.effective_user is None:
        return
    fut = _pending_passwords.get(update.effective_user.id)
    if fut is not None and not fut.done():
        fut.set_result(None)


async def _progress_updater(msg, job_id: int, progress: ExtractionProgress) -> None:
    """Edit the progress message every few seconds with a live dashboard."""
    start = time.monotonic()
    last_text = ""
    while True:
        await asyncio.sleep(config.PROGRESS_UPDATE_INTERVAL)
        elapsed = time.monotonic() - start
        try:
            if progress.phase == "downloading":
                pct = (
                    progress.download_current / max(progress.download_total, 1) * 100
                )
                dl_elapsed = (
                    time.monotonic() - progress.download_start
                    if progress.download_start else elapsed
                )
                speed = progress.download_current / max(dl_elapsed, 0.001)
                remaining_bytes = max(
                    progress.download_total - progress.download_current, 0
                )
                eta = remaining_bytes / max(speed, 1)
                text = (
                    f"\u2699\ufe0f Live Dashboard\n"
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"\U0001f4e5 Downloading\n"
                    f"   {progress_bar(progress.download_current, progress.download_total)} {pct:.0f}%\n"
                    f"   {bytes_human(progress.download_current)} / "
                    f"{bytes_human(progress.download_total)}\n"
                    f"\U0001f4c8 Speed: {bytes_human(int(speed))}/s\n"
                    f"\u23f1 ETA: {seconds_human(eta)}   Elapsed: {seconds_human(elapsed)}"
                )
            elif progress.phase == "extracting":
                cur_file = progress.current_file or "…"
                if len(cur_file) > 40:
                    cur_file = cur_file[:37] + "…"
                if progress.extract_total > 0:
                    pct = (
                        progress.extract_current
                        / max(progress.extract_total, 1) * 100
                    )
                    bar = (
                        f"   {progress_bar(progress.extract_current, progress.extract_total)} "
                        f"{pct:.0f}% "
                        f"({progress.extract_current:,}/{progress.extract_total:,})\n"
                    )
                else:
                    bar = f"   Files extracted: {progress.extract_current:,}\n"
                text = (
                    f"\u2699\ufe0f Live Dashboard\n"
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"\U0001f4e5 Download:  Done \u2705\n"
                    f"\U0001f4c2 Extracting\n"
                    f"{bar}"
                    f"   Now: {cur_file}\n"
                    f"\u23f1 Elapsed: {seconds_human(elapsed)}"
                )
            elif progress.phase == "scanning":
                cur_file = progress.current_file or "…"
                if len(cur_file) > 40:
                    cur_file = cur_file[:37] + "…"
                pct = (
                    progress.files_scanned
                    / max(progress.files_total, 1) * 100
                )
                rate = progress.files_scanned / max(elapsed, 0.001)
                eta = (
                    (progress.files_total - progress.files_scanned)
                    / max(rate, 0.001)
                    if progress.files_total else 0
                )
                text = (
                    f"\u2699\ufe0f Live Dashboard\n"
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"\U0001f4e5 Download:  Done \u2705\n"
                    f"\U0001f4c2 Extract:   Done \u2705\n"
                    f"\U0001f50d Scanning\n"
                    f"   {progress_bar(progress.files_scanned, progress.files_total)} "
                    f"{pct:.0f}% ({progress.files_scanned:,}/{progress.files_total:,})\n"
                    f"   Now: {cur_file}\n"
                    f"\U0001f36a Cookies found so far: "
                    f"{progress.cookies_found:,}\n"
                    f"\u26a1 Rate: {rate:.1f} files/s   ETA: {seconds_human(eta)}\n"
                    f"\u23f1 Elapsed: {seconds_human(elapsed)}"
                )
            elif progress.phase == "packaging":
                text = (
                    f"\u2699\ufe0f Live Dashboard\n"
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"\U0001f4e5 Download:  Done \u2705\n"
                    f"\U0001f4c2 Extract:   Done \u2705\n"
                    f"\U0001f50d Scan:      Done \u2705\n"
                    f"\U0001f4e6 Packaging cookies into .zip\u2026\n"
                    f"\U0001f36a Cookies found: {progress.cookies_found:,}\n"
                    f"\u23f1 Elapsed: {seconds_human(elapsed)}"
                )
            else:
                continue

            if text == last_text:
                continue
            last_text = text
            await msg.edit_text(
                text,
                reply_markup=_cancel_job_kb(job_id),
            )
        except Exception:
            # Telegram throws "Message is not modified" if the text/buttons
            # haven't changed since the last edit — silently ignore so the
            # updater keeps running.
            pass


def _notify_admin_error(context, user_id: int, action: str, error: str) -> None:
    """Best-effort critical error notification to admin."""
    text = (
        f"\U0001f6a8 Critical Error\n"
        f"User: {user_id}\n"
        f"Action: {action}\n"
        f"Error: {error[:500]}"
    )
    asyncio.create_task(context.bot.send_message(config.ADMIN_ID, text))


# ── Cancel handlers ────────────────────────────────────────
async def cancel_extract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("\u274c Extraction cancelled.")
    return ConversationHandler.END


async def cancel_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a running/queued job."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data = query.data or ""
    try:
        job_id = int(data.split("_")[-1])
    except (ValueError, IndexError):
        return

    # Cancel in queue
    if _job_queue and _job_queue.cancel(job_id):
        await db.update_job(job_id, status="cancelled", completed_at=db._now())
        await query.edit_message_text("\u274c Job cancelled (was queued).")
        return

    # Cancel running job
    prog = _active_progress.get(job_id)
    if prog:
        prog.cancelled = True
        await db.update_job(job_id, status="cancelled", completed_at=db._now())
        await query.edit_message_text("\u274c Cancelling job...")
        return

    await query.edit_message_text("\u274c Job not found or already completed.")


# ── Register ───────────────────────────────────────────────
def register(app, job_queue: JobQueue) -> None:
    """Attach extraction handlers to the Application."""
    global _job_queue
    _job_queue = job_queue

    conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(extract_entry, pattern="^extract$"),
            CommandHandler("extract", extract_entry),
        ],
        states={
            DOMAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, domain_received),
                CallbackQueryHandler(cancel_extract, pattern="^extract_cancel$"),
            ],
            FILE: [
                MessageHandler(filters.Document.ALL, file_received),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.Regex(
                        r"^\s*https?://\S+\s*$"
                    ),
                    url_received,
                ),
                CallbackQueryHandler(cancel_extract, pattern="^extract_cancel$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_extract, pattern="^extract_cancel$"),
            CommandHandler("cancel", cancel_extract),
        ],
        per_message=False,
    )
    app.add_handler(conv)

    # Job cancel callback (works outside conversation)
    app.add_handler(CallbackQueryHandler(cancel_job_callback, pattern=r"^cancel_job_\d+$"))

    # Archive-password prompt handlers. Registered in a negative group so
    # they run ahead of the generic conversation handlers and catch the
    # user's reply even though the /extract conversation has already ended
    # (the password wait happens inside the queue worker).
    app.add_handler(CommandHandler("skip", skip_command), group=-1)
    app.add_handler(
        CallbackQueryHandler(skip_password_callback, pattern=r"^skip_pw_\d+$"),
        group=-1,
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND, password_reply,
        ),
        group=-1,
    )
