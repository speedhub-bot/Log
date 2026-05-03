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

# Minimum gap between dashboard log lines (separate from the in-chat
# 2 MB-boundary edits, which are throttled independently).
MIN_EDIT_INTERVAL = 1.0

# How often we edit the user-facing status message during a download.
# Pyrogram fires `_progress_cb` on every chunk (~512 KB) which is far
# more often than Telegram's per-chat edit budget, so we throttle by
# bytes (every ~2 MB) AND by wall-clock seconds (>= 1.5 s apart) to
# stay well within rate limits while still feeling responsive.
LIVE_MSG_EDIT_BYTES = 2 * 1024 * 1024
LIVE_MSG_EDIT_INTERVAL = 1.5

# Parallel chunk transfers within a single download. The user-tuned
# value is 10 — Pyrogram's bot-token sessions usually saturate well
# below this on most ISPs, so going higher rarely helps and risks
# server-side back-pressure.
MAX_CONCURRENT_TRANSMISSIONS = int(
    os.getenv("PYROGRAM_MAX_TRANSMISSIONS", "10")
)

# Worker thread pool inside Pyrogram (chunk decryption + write back).
# 16 keeps tgcrypto saturated without thrashing the event loop.
PYROGRAM_WORKERS = int(os.getenv("PYROGRAM_WORKERS", "16"))

# How long Pyrogram silently absorbs a FloodWait before raising it. 60s
# means most rate-limit hiccups recover transparently without the
# extraction job failing.
PYROGRAM_SLEEP_THRESHOLD = int(os.getenv("PYROGRAM_SLEEP_THRESHOLD", "60"))


def _check_tgcrypto_loaded() -> None:
    """Log whether TgCrypto (pyrogram's fast C MTProto crypto backend)
    is actually available. Missing TgCrypto silently falls back to the
    pure-Python implementation, which is typically **5-10x slower**.
    """
    try:
        import tgcrypto  # noqa: F401
        logger.info("tgcrypto loaded — MTProto crypto uses C backend")
    except ImportError:
        logger.warning(
            "tgcrypto NOT found; downloads will use the slow pure-Python "
            "crypto fallback. Install with: pip install tgcrypto"
        )


async def _get_pyrogram() -> Client:
    """Return a started Pyrogram bot client (singleton).

    Configured for high-throughput downloads:
      * ``workers``                       — internal thread pool for
                                            chunk decrypt + disk write.
      * ``max_concurrent_transmissions``  — parallel chunk fetches per
                                            download.
      * ``sleep_threshold``               — silently absorb FloodWait
                                            replies up to this many sec.
    """
    global _pyro_client, _pyro_started
    if _pyro_client is None:
        _pyro_client = Client(
            name="cookie_downloader",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            in_memory=True,
            no_updates=True,
            workers=PYROGRAM_WORKERS,
            max_concurrent_transmissions=MAX_CONCURRENT_TRANSMISSIONS,
            sleep_threshold=PYROGRAM_SLEEP_THRESHOLD,
        )
    if not _pyro_started:
        _check_tgcrypto_loaded()
        await _pyro_client.start()
        _pyro_started = True
        logger.info(
            "Pyrogram download client started "
            "(workers={}, concurrent_transmissions={}, sleep_threshold={}s)",
            PYROGRAM_WORKERS,
            MAX_CONCURRENT_TRANSMISSIONS,
            PYROGRAM_SLEEP_THRESHOLD,
        )
    return _pyro_client


async def disconnect_pyrogram() -> None:
    """Gracefully disconnect the Pyrogram client."""
    global _pyro_started
    if _pyro_client is not None and _pyro_started:
        await _pyro_client.stop()
        _pyro_started = False
        logger.info("Pyrogram download client stopped")


def _format_live_progress(current: int, total: int, start_ts: float) -> str:
    """Render the user-facing live download text (the format requested
    by the bot owner — speed in MB/s, percent, MB-of-MB)."""
    elapsed = max(time.monotonic() - start_ts, 0.001)
    speed_mbps = (current / elapsed) / (1024 * 1024)
    percent = (current / total * 100) if total > 0 else 0.0
    downloaded_mb = current / (1024 * 1024)
    total_mb = total / (1024 * 1024) if total > 0 else 0.0
    return (
        "\u2b07\ufe0f <b>Downloading\u2026</b>\n"
        f"Progress: {percent:.1f}%\n"
        f"\U0001f4e6 {downloaded_mb:.1f} MB / {total_mb:.1f} MB\n"
        f"\u26a1 Speed: {speed_mbps:.1f} MB/s"
    )


async def _edit_live_progress(
    status_msg,
    current: int,
    total: int,
    start_ts: float,
    cancel_kb,
) -> None:
    """Edit *status_msg* with the live download text, swallowing the
    inevitable ``MessageNotModified`` / network errors."""
    if status_msg is None:
        return
    text = _format_live_progress(current, total, start_ts)
    try:
        await status_msg.edit_text(
            text, parse_mode="HTML", reply_markup=cancel_kb,
        )
    except Exception:
        # Telegram rejects edits that produce identical text, plus we
        # can race with the dashboard updater. Both are harmless.
        pass


async def download_file(
    message: Message,
    dest_path: str,
    progress: ExtractionProgress,
    max_retries: int = 3,
    status_msg=None,
    cancel_kb=None,
) -> str:
    """
    Download the document attached to *message* into *dest_path*.

    Uses pyrogram's MTProto transport with up to ``MAX_CONCURRENT_TRANSMISSIONS``
    parallel chunk fetches and the tgcrypto C backend (if installed) for
    AES-IGE. Also:
      * Retries once on ``FloodWait`` after sleeping the server-requested
        delay, up to *max_retries* total attempts.
      * Tracks and logs **instantaneous**, **peak** and **average** MB/s.
      * Updates the ExtractionProgress so the Telegram live dashboard
        always reflects true bytes-in-flight.

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
    progress.live_download_msg = status_msg is not None

    logger.info(
        "Downloading {} ({:.1f} MB) via Pyrogram MTProto "
        "(workers={}, parallel_transmissions={}, sleep_threshold={}s)",
        file_name, file_size / 1e6,
        PYROGRAM_WORKERS, MAX_CONCURRENT_TRANSMISSIONS,
        PYROGRAM_SLEEP_THRESHOLD,
    )
    client = await _get_pyrogram()

    # Resolve the message in Pyrogram context
    pyro_msg = await client.get_messages(message.chat_id, message.message_id)
    if not isinstance(pyro_msg, PyroMessage) or not pyro_msg.document:
        raise RuntimeError("Could not resolve file message via Pyrogram")

    # Import lazily: pyrogram exceptions module location varies between
    # 2.x minor versions. We degrade gracefully if the exact exception
    # class isn't found.
    try:
        from pyrogram.errors import FloodWait
    except ImportError:
        FloodWait = None  # type: ignore[assignment]

    main_loop = asyncio.get_running_loop()

    attempt = 0
    while True:
        attempt += 1
        start_ts = time.monotonic()
        last_log = start_ts
        last_bytes = 0
        peak_mbps = 0.0
        # Bytes-thresholded throttle for the in-chat live edit. We refuse
        # to fire another edit until at least LIVE_MSG_EDIT_BYTES have
        # been transferred AND LIVE_MSG_EDIT_INTERVAL seconds have
        # elapsed since the last one.
        last_edit_bytes = 0
        last_edit_ts = 0.0
        edit_inflight = False

        def _progress_cb(current: int, total: int) -> None:
            nonlocal last_log, last_bytes, peak_mbps
            nonlocal last_edit_bytes, last_edit_ts, edit_inflight
            progress.download_current = current
            progress.download_total = total
            now = time.monotonic()
            if now - last_log >= MIN_EDIT_INTERVAL:
                dt = max(now - last_log, 0.001)
                # Instantaneous speed over the last second or so — the
                # real "are we still moving" signal during flaky links.
                inst_mbps = (current - last_bytes) / dt / 1e6
                # Average since the download began — the one most users
                # think of as "how fast was this download".
                avg_elapsed = max(now - start_ts, 0.001)
                avg_mbps = current / avg_elapsed / 1e6
                if inst_mbps > peak_mbps:
                    peak_mbps = inst_mbps
                last_log = now
                last_bytes = current
                logger.info(
                    "Download {}/{:,}B  {:.1f}% | "
                    "inst={:.1f}MB/s avg={:.1f}MB/s peak={:.1f}MB/s",
                    f"{current:,}", total,
                    (current / total * 100) if total else 0,
                    inst_mbps, avg_mbps, peak_mbps,
                )

            # In-chat live edit: every 2 MB and at most once / 1.5 s.
            # We schedule the coroutine on the bot's loop because this
            # callback runs inside Pyrogram's executor.
            if (
                status_msg is not None
                and not edit_inflight
                and (current - last_edit_bytes) >= LIVE_MSG_EDIT_BYTES
                and (now - last_edit_ts) >= LIVE_MSG_EDIT_INTERVAL
            ):
                last_edit_bytes = current
                last_edit_ts = now
                edit_inflight = True

                def _done(_task):
                    nonlocal edit_inflight
                    edit_inflight = False

                try:
                    coro = _edit_live_progress(
                        status_msg, current, total, start_ts, cancel_kb,
                    )
                    # Pyrogram fires this callback inside the bot's
                    # event loop. ``call_soon_threadsafe`` is the safe
                    # cross-thread variant if Pyrogram ever moves to an
                    # executor — it works either way.
                    if main_loop.is_running():
                        try:
                            asyncio.get_running_loop()
                            task = asyncio.ensure_future(coro)
                        except RuntimeError:
                            task = asyncio.run_coroutine_threadsafe(
                                coro, main_loop,
                            )
                    else:
                        task = asyncio.run_coroutine_threadsafe(
                            coro, main_loop,
                        )
                    task.add_done_callback(_done)
                except Exception:
                    edit_inflight = False

        try:
            path = await client.download_media(
                message=pyro_msg,
                file_name=out_path,
                progress=_progress_cb,
            )
            break
        except Exception as exc:
            # FloodWait: server asked us to back off — sleep and retry.
            if FloodWait is not None and isinstance(exc, FloodWait):
                wait = getattr(exc, "value", getattr(exc, "x", 5))
                if attempt >= max_retries:
                    raise
                logger.warning(
                    "FloodWait hit on attempt {}/{}; sleeping {}s",
                    attempt, max_retries, wait,
                )
                await asyncio.sleep(float(wait))
                continue
            raise

    if path is None:
        raise RuntimeError("Pyrogram returned no file")

    elapsed = time.monotonic() - start_ts
    speed_mbps = (file_size / max(elapsed, 0.001)) / 1e6
    progress.download_current = progress.download_total
    progress.live_download_msg = False
    logger.info(
        "Download complete: {} ({:.1f} MB in {:.1f}s, avg {:.1f} MB/s, "
        "peak {:.1f} MB/s)",
        path, file_size / 1e6, elapsed, speed_mbps, peak_mbps,
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
    status_msg=None,
    cancel_kb=None,
) -> str:
    """Stream-download *url* into *dest_path* using aiohttp.

    The Content-Disposition header is honoured for the final filename.
    Updates ``progress.download_current`` / ``download_total`` so the
    live dashboard can render the same way as a Pyrogram download.
    When *status_msg* is supplied, the message is edited every ~2 MB
    with the live download dashboard (speed in MB/s, percent, total).
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
    progress.live_download_msg = status_msg is not None

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
            last_edit_bytes = 0
            last_edit_ts = 0.0
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
                    # Live in-chat progress edits — every ~2 MB and at
                    # most once per 1.5 s, matching the Pyrogram path.
                    if (
                        status_msg is not None
                        and (downloaded - last_edit_bytes) >= LIVE_MSG_EDIT_BYTES
                        and (now - last_edit_ts) >= LIVE_MSG_EDIT_INTERVAL
                    ):
                        last_edit_bytes = downloaded
                        last_edit_ts = now
                        await _edit_live_progress(
                            status_msg,
                            downloaded,
                            total or downloaded,
                            start_ts,
                            cancel_kb,
                        )

    elapsed = time.monotonic() - start_ts
    mb = downloaded / 1e6
    speed = mb / max(elapsed, 0.001)
    progress.download_total = downloaded
    progress.download_current = downloaded
    progress.live_download_msg = False
    logger.info(
        "URL download complete: {} ({:.1f} MB in {:.1f}s, {:.1f} MB/s)",
        out_path, mb, elapsed, speed,
    )
    return out_path
