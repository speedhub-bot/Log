"""
Large-file downloader using Pyrogram MTProto client.

Pyrogram connects via MTProto directly (not the HTTP Bot API),
so it can download files of any size using just the bot token —
no user session string required.

All files are downloaded via Pyrogram MTProto for maximum speed.
MTProto is significantly faster than the Bot API HTTP endpoint
because it uses persistent encrypted TCP connections with parallel
chunk transfers.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import urllib.parse

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

# Parallel chunk transfers within a single download — higher = faster on
# high-bandwidth servers. Pyrogram tops out around 50; bot-token sessions
# typically saturate well below that, but 16 noticeably outperforms the
# previous default of 10 on big files (>200 MB).
MAX_CONCURRENT_TRANSMISSIONS = int(
    os.getenv("PYROGRAM_MAX_TRANSMISSIONS", "16")
)


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
            max_concurrent_transmissions=MAX_CONCURRENT_TRANSMISSIONS,
        )
    if not _pyro_started:
        await _pyro_client.start()
        _pyro_started = True
        logger.info(
            "Pyrogram download client started (concurrent_transmissions={})",
            MAX_CONCURRENT_TRANSMISSIONS,
        )
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

    All downloads use Pyrogram MTProto for maximum speed —
    parallel chunk transfers over persistent TCP connections.

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
    progress.download_start = time.monotonic()

    logger.info(
        "Downloading {} ({} B) via Pyrogram MTProto",
        file_name, file_size,
    )
    client = await _get_pyrogram()

    # Resolve the message in Pyrogram context
    pyro_msg = await client.get_messages(message.chat_id, message.message_id)
    if not isinstance(pyro_msg, PyroMessage) or not pyro_msg.document:
        raise RuntimeError("Could not resolve file message via Pyrogram")

    start_ts = time.monotonic()
    last_update = start_ts

    def _progress_cb(current: int, total: int) -> None:
        # Pyrogram calls this synchronously from its IO loop; keep it cheap.
        nonlocal last_update
        progress.download_current = current
        progress.download_total = total
        now = time.monotonic()
        if now - last_update >= MIN_EDIT_INTERVAL:
            elapsed = max(now - start_ts, 0.001)
            speed = current / elapsed
            logger.debug(
                "Download {}/{} ({:.1f} MB/s)",
                current, total, speed / 1e6,
            )
            last_update = now

    path = await client.download_media(
        message=pyro_msg,
        file_name=out_path,
        progress=_progress_cb,
    )
    if path is None:
        raise RuntimeError("Pyrogram returned no file")

    elapsed = time.monotonic() - start_ts
    speed_mbps = (file_size / max(elapsed, 0.001)) / 1e6
    progress.download_current = progress.download_total
    logger.info(
        "Download complete: {} ({:.1f} MB in {:.1f}s, {:.1f} MB/s)",
        path, file_size / 1e6, elapsed, speed_mbps,
    )
    return str(path)


# ─── Direct URL downloader (HTTP/HTTPS) ──────────────────────────────

_URL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
}


def _filename_from_headers(url: str, content_disposition: str) -> str:
    """Extract filename from Content-Disposition or URL path."""
    m = re.search(
        r"filename\*?=(?:UTF-8''|[\"']?)([^\"';\r\n]+)",
        content_disposition,
        re.IGNORECASE,
    )
    if m:
        name = m.group(1).strip().strip('"').strip("'")
        try:
            name = urllib.parse.unquote(name)
        except Exception:
            pass
        if name:
            return name
    path = urllib.parse.urlparse(url).path
    tail = path.rsplit("/", 1)[-1]
    return tail or "archive"


async def download_from_url(
    url: str,
    dest_path: str,
    progress: ExtractionProgress,
    file_name_hint: str = "",
    chunk_size: int = 512 * 1024,
    timeout: float = 60.0,
) -> str:
    """Stream-download *url* into *dest_path* using aiohttp.

    The Content-Disposition header is honoured for the final filename.
    Updates ``progress.download_current`` / ``download_total`` so the
    live dashboard can render the same way as a Pyrogram download.
    """
    # Import aiohttp lazily — it isn't used on every code path and
    # Railway may not have it pre-installed on older images.
    try:
        import aiohttp
    except ImportError as exc:
        raise RuntimeError(
            "aiohttp is required for URL downloads but isn't installed. "
            "`pip install aiohttp` and retry."
        ) from exc

    progress.phase = "downloading"
    progress.download_current = 0
    progress.download_total = 0
    progress.download_start = time.monotonic()

    os.makedirs(dest_path, exist_ok=True)

    timeout_cfg = aiohttp.ClientTimeout(sock_connect=timeout, sock_read=None)
    async with aiohttp.ClientSession(
        timeout=timeout_cfg, headers=_URL_HEADERS,
    ) as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise RuntimeError(
                    f"HTTP {resp.status} — server rejected the request. "
                    "Use a direct download link (.zip / .rar)."
                )
            total = int(resp.headers.get("Content-Length", 0) or 0)
            progress.download_total = total
            cd = resp.headers.get("Content-Disposition", "") or ""
            file_name = file_name_hint or _filename_from_headers(url, cd)
            if not any(file_name.lower().endswith(ext) for ext in (
                ".zip", ".rar", ".7z", ".tar.gz", ".tgz", ".tar",
            )):
                # Force an extension so the rest of the pipeline's
                # filename-extension sniffing doesn't misclassify. The
                # magic-byte sniffer in extractor.py is the final word
                # on format.
                if file_name and "." not in file_name:
                    file_name = file_name + ".zip"

            out_path = os.path.join(dest_path, file_name)
            start_ts = time.monotonic()
            last_log = start_ts
            downloaded = 0
            with open(out_path, "wb") as fh:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    if progress.cancelled:
                        try:
                            resp.close()
                        except Exception:
                            pass
                        raise RuntimeError(
                            "Download cancelled by user"
                        )
                    fh.write(chunk)
                    downloaded += len(chunk)
                    progress.download_current = downloaded
                    now = time.monotonic()
                    if now - last_log >= MIN_EDIT_INTERVAL:
                        elapsed = max(now - start_ts, 0.001)
                        speed = downloaded / elapsed / 1e6
                        logger.debug(
                            "URL download {}/{} ({:.1f} MB/s)",
                            downloaded, total or "?", speed,
                        )
                        last_log = now

    elapsed = time.monotonic() - start_ts
    mb = downloaded / 1e6
    speed = mb / max(elapsed, 0.001)
    progress.download_total = downloaded
    progress.download_current = downloaded
    logger.info(
        "URL download complete: {} ({:.1f} MB in {:.1f}s, {:.1f} MB/s)",
        out_path, mb, elapsed, speed,
    )
    return out_path
