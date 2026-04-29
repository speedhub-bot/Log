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
import shutil
import tempfile
import time
from typing import Dict

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
from services.downloader import download_file
from services.extractor import ExtractionProgress, run_extraction_async
from services.queue import JobQueue, QueueItem
from utils.formatting import bytes_human, progress_bar, seconds_human, time_until
from utils.validators import validate_archive, validate_domain

# Conversation states
DOMAIN, FILE = range(2)

# Module-level job queue (initialised in register())
_job_queue: JobQueue | None = None

# Active progress trackers: job_id -> ExtractionProgress
_active_progress: Dict[int, ExtractionProgress] = {}


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

    # Rate limit check
    count = await db.count_user_extractions_last_hour(user.id)
    if count >= config.MAX_EXTRACTIONS_PER_HOUR and user.id != config.ADMIN_ID:
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

    remaining = await db.get_remaining_quota(user.id)
    vip = await db.is_vip(user.id)
    limit_text = "Unlimited" if vip else bytes_human(remaining)
    max_file = "10 GB" if vip else "2 GB"

    text = (
        f"\U0001f4c1 Now send your archive file\n"
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
    vip = await db.is_vip(user.id)

    # Max file size check
    max_bytes = config.VIP_MAX_FILE_BYTES if vip else config.FREE_MAX_FILE_BYTES
    if file_size > max_bytes:
        await update.message.reply_text(
            f"\u274c File too large ({bytes_human(file_size)}).\n"
            f"Max: {bytes_human(max_bytes)}\n\n"
            "\U0001f451 Get VIP for higher limits!",
            reply_markup=_cancel_kb(),
        )
        return FILE

    # Quota check
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

    # Consume quota
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

    async def _worker() -> None:
        await _process_job(
            update, context, job_id, user.id, domain,
            update.message, progress_msg, progress,  # type: ignore[arg-type]
        )

    # Enqueue
    is_vip = await db.is_vip(user.id)
    item = QueueItem(
        priority=0 if is_vip else 1,
        job_id=job_id,
        user_id=user.id,
        is_vip=is_vip,
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

        # Download
        archive_path = await download_file(original_msg, temp_dir, progress)

        # Extract
        result = await run_extraction_async(archive_path, domain, progress)

        updater_task.cancel()
        try:
            await updater_task
        except asyncio.CancelledError:
            pass

        if not result.success:
            await db.update_job(
                job_id, status="failed", error_message=result.error,
                completed_at=db._now(),
                duration_seconds=time.monotonic() - start_ts,
            )
            await progress_msg.edit_text(
                f"\u274c Extraction failed: {result.error}",
            )
            _notify_admin_error(context, user_id, "extraction", result.error)
            return

        # Update DB
        duration = time.monotonic() - start_ts
        await db.update_job(
            job_id, status="done", cookies_found=result.cookies_found,
            files_scanned=result.files_scanned, completed_at=db._now(),
            duration_seconds=duration,
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
                        await context.bot.send_document(
                            chat_id=user_id,
                            document=fh,
                            filename=os.path.basename(fpath),
                        )
            except Exception:
                logger.exception("Failed to send result file {}", fpath)

        job_row = await db.get_job(job_id)
        file_size = job_row["file_size_bytes"] if job_row else 0  # type: ignore[index]

        # Summary
        summary = (
            f"\u2705 Extraction Complete!\n\n"
            f"\U0001f310 Domain: {domain}\n"
            f"\U0001f36a Cookies found: {result.cookies_found:,}\n"
            f"\U0001f4c1 Files scanned: {result.files_scanned:,}\n"
            f"\U0001f4e6 Archive size: {bytes_human(file_size)}\n"
            f"\u23f1 Time taken: {seconds_human(duration)}\n"
            f"\U0001f4c4 Output files: {len(result.output_files)}"
        )
        await progress_msg.edit_text(
            summary,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("\U0001f50d Extract Again", callback_data="extract"),
                    InlineKeyboardButton("\U0001f4ca My Stats", callback_data="mystats"),
                ],
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


async def _progress_updater(msg, job_id: int, progress: ExtractionProgress) -> None:
    """Edit the progress message every few seconds."""
    start = time.monotonic()
    while True:
        await asyncio.sleep(config.PROGRESS_UPDATE_INTERVAL)
        elapsed = time.monotonic() - start
        try:
            if progress.phase == "downloading":
                pct = (
                    progress.download_current / max(progress.download_total, 1) * 100
                )
                speed = progress.download_current / max(elapsed, 0.001)
                text = (
                    f"\u2699\ufe0f Processing your archive...\n\n"
                    f"\U0001f4e5 Downloading: {progress_bar(progress.download_current, progress.download_total)} "
                    f"{pct:.0f}% ({bytes_human(progress.download_current)}/{bytes_human(progress.download_total)})\n"
                    f"\U0001f4c8 Speed: {bytes_human(int(speed))}/s\n"
                    f"\u23f1 Elapsed: {seconds_human(elapsed)}"
                )
            elif progress.phase == "extracting":
                text = (
                    f"\u2699\ufe0f Processing your archive...\n\n"
                    f"\U0001f4e5 Downloading: Done \u2705\n"
                    f"\U0001f4c2 Extracting archive...\n"
                    f"\u23f1 Elapsed: {seconds_human(elapsed)}"
                )
            elif progress.phase == "scanning":
                text = (
                    f"\u2699\ufe0f Processing your archive...\n\n"
                    f"\U0001f4e5 Downloading: Done \u2705\n"
                    f"\U0001f4c2 Extracting: Done \u2705\n"
                    f"\U0001f50d Scanning: {progress_bar(progress.files_scanned, progress.files_total)} "
                    f"{progress.files_scanned}/{progress.files_total} files\n"
                    f"\U0001f36a Found so far: {progress.cookies_found:,}\n\n"
                    f"\u23f1 Elapsed: {seconds_human(elapsed)}"
                )
            else:
                continue

            await msg.edit_text(
                text,
                reply_markup=_cancel_job_kb(job_id),
            )
        except Exception:
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
