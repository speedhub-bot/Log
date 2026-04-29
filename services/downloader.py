"""
Large-file downloader using Telethon user client.

Files <= 20 MB are fetched via the Bot API (``bot.get_file``).
Larger files are streamed through a Telethon ``TelegramClient``
connected via a pre-generated session string.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from loguru import logger
from telegram import Document, Message

import config
from services.extractor import ExtractionProgress

# Lazy-initialised Telethon client
_telethon_client = None
_telethon_started = False


async def _get_telethon():
    """Return a started Telethon client (singleton)."""
    global _telethon_client, _telethon_started
    if _telethon_client is None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        _telethon_client = TelegramClient(
            StringSession(config.SESSION_STRING),
            config.API_ID,
            config.API_HASH,
        )
    if not _telethon_started:
        await _telethon_client.start()
        _telethon_started = True
    return _telethon_client


async def disconnect_telethon() -> None:
    """Gracefully disconnect the Telethon client."""
    global _telethon_started
    if _telethon_client is not None and _telethon_started:
        await _telethon_client.disconnect()
        _telethon_started = False


async def download_file(
    message: Message,
    dest_path: str,
    progress: ExtractionProgress,
) -> str:
    """
    Download the document attached to *message* into *dest_path*.

    Automatically chooses Bot API or Telethon depending on file size.
    Updates *progress* for live UI feedback.

    Returns:
        Absolute path to the downloaded file.
    """
    doc: Optional[Document] = message.document
    if doc is None:
        raise ValueError("Message has no document attached")

    file_size = doc.file_size or 0
    file_name = doc.file_name or "archive"
    out_path = os.path.join(dest_path, file_name)

    progress.phase = "downloading"
    progress.download_total = file_size
    progress.download_current = 0

    if file_size <= 20 * 1024 * 1024:
        # Small file — plain Bot API download
        logger.info("Small file ({} B), using Bot API download", file_size)
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(out_path)
        progress.download_current = file_size
        return out_path

    # Large file — Telethon user-client streaming download
    logger.info("Large file ({} B), using Telethon download", file_size)
    client = await _get_telethon()

    # Resolve the Telegram message in the Telethon context
    tl_message = await client.get_messages(
        message.chat_id,
        ids=message.message_id,
    )
    if tl_message is None:
        raise RuntimeError("Could not resolve message via Telethon")

    last_update = time.monotonic()
    downloaded = 0

    async def _progress_cb(current: int, total: int) -> None:
        nonlocal last_update, downloaded
        downloaded = current
        progress.download_current = current
        progress.download_total = total
        now = time.monotonic()
        if now - last_update >= 1.0:
            speed = current / max(now - start_ts, 0.001)
            logger.debug("Download {}/{} ({:.1f} MB/s)", current, total, speed / 1e6)
            last_update = now

    start_ts = time.monotonic()
    await client.download_media(
        tl_message,
        file=out_path,
        progress_callback=_progress_cb,
    )
    progress.download_current = progress.download_total
    logger.info("Download complete: {}", out_path)
    return out_path
