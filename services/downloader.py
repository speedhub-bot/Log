"""
Large-file downloader using Pyrogram MTProto client.

Pyrogram connects via MTProto directly (not the HTTP Bot API),
so it can download files of any size using just the bot token —
no user session string required.

Files <= 20 MB are still fetched via the Bot API for speed.
Larger files are streamed through the Pyrogram client.
"""

from __future__ import annotations

import os
import time

from loguru import logger
from pyrogram import Client
from pyrogram.types import Message as PyroMessage
from telegram import Document, Message

import config
from services.extractor import ExtractionProgress

# Lazy-initialised Pyrogram client
_pyro_client: Client | None = None
_pyro_started: bool = False

MIN_EDIT_INTERVAL = 1.0


async def _get_pyrogram() -> Client:
    """Return a started Pyrogram bot client (singleton)."""
    global _pyro_client, _pyro_started
    if _pyro_client is None:
        _pyro_client = Client(
            name="cookie_downloader",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            in_memory=True,
            no_updates=True,
        )
    if not _pyro_started:
        await _pyro_client.start()
        _pyro_started = True
        logger.info("Pyrogram download client started")
    return _pyro_client


async def disconnect_pyrogram() -> None:
    """Gracefully disconnect the Pyrogram client."""
    global _pyro_started
    if _pyro_client is not None and _pyro_started:
        await _pyro_client.stop()
        _pyro_started = False
        logger.info("Pyrogram download client stopped")


async def download_file(
    message: Message,
    dest_path: str,
    progress: ExtractionProgress,
) -> str:
    """
    Download the document attached to *message* into *dest_path*.

    Automatically chooses Bot API or Pyrogram depending on file size.
    Updates *progress* for live UI feedback.

    Returns:
        Absolute path to the downloaded file.
    """
    doc: Document | None = message.document
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

    # Large file — Pyrogram MTProto download (no user session needed)
    logger.info("Large file ({} B), using Pyrogram MTProto download", file_size)
    client = await _get_pyrogram()

    # Resolve the message in Pyrogram context
    pyro_msg = await client.get_messages(message.chat_id, message.message_id)
    if not isinstance(pyro_msg, PyroMessage) or not pyro_msg.document:
        raise RuntimeError("Could not resolve file message via Pyrogram")

    start_ts = time.monotonic()
    last_update = start_ts

    async def _progress_cb(current: int, total: int) -> None:
        nonlocal last_update
        progress.download_current = current
        progress.download_total = total
        now = time.monotonic()
        if now - last_update >= MIN_EDIT_INTERVAL:
            speed = current / max(now - start_ts, 0.001)
            logger.debug("Download {}/{} ({:.1f} MB/s)", current, total, speed / 1e6)
            last_update = now

    path = await client.download_media(
        message=pyro_msg,
        file_name=out_path,
        progress=_progress_cb,
    )
    if path is None:
        raise RuntimeError("Pyrogram returned no file")

    progress.download_current = progress.download_total
    logger.info("Download complete: {}", path)
    return str(path)
