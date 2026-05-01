"""
Cookie extraction engine.

Houses the SmartCookieExtractor (reused exactly from the original
``log to cookie.py``) plus an async wrapper that runs extraction
off the event-loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Generator, Iterable, List, Optional, Tuple, Union

from loguru import logger

import config


# ════════════════════════════════════════════════════════════
#  SmartCookieExtractor
# ════════════════════════════════════════════════════════════

class SmartCookieExtractor:
    """Efficiently extracts cookies from Netscape cookie format files.

    Supports filtering against a single target domain or multiple target
    domains in a single pass. When multiple domains are provided each cookie
    line is matched against every target and routed to the first matching
    target via :meth:`match_domain`.
    """

    def __init__(
        self,
        domain: Union[str, Iterable[str]],
        patterns: Optional[List[str]] = None,
    ):
        """
        Initialize extractor

        Args:
            domain: A single domain (e.g. ``'spotify.com'``) or an iterable of
                domains (e.g. ``['spotify.com', 'netflix.com']``) to filter
                cookies against.
            patterns: Optional regex patterns for additional filtering.
        """
        if isinstance(domain, str):
            domains: List[str] = [domain]
        else:
            domains = list(domain)

        # Normalise: lower-case, strip leading dots, drop empties, dedupe.
        seen: set[str] = set()
        cleaned: List[str] = []
        for d in domains:
            norm = d.lower().lstrip(".").strip()
            if norm and norm not in seen:
                seen.add(norm)
                cleaned.append(norm)
        if not cleaned:
            raise ValueError("SmartCookieExtractor requires at least one domain")

        self.domains: List[str] = cleaned
        # Back-compat: single-domain callers still read ``.domain``.
        self.domain: str = cleaned[0]
        self.patterns = patterns or []
        self.domain_pattern = re.compile(
            "(" + "|".join(re.escape(d) for d in cleaned) + ")",
            re.IGNORECASE,
        )

    def extract_from_file(self, filepath: str) -> List[Dict[str, str]]:
        """
        Extract cookies from a Netscape cookie format file.

        Returns a flat list of cookie dicts that match *any* configured
        target domain. The matched target is recorded in each dict's
        ``target_domain`` key so callers routing per-domain output do not
        have to recompute matches.
        """
        results: List[Dict[str, str]] = []
        fp = Path(filepath)

        if not fp.exists():
            raise FileNotFoundError(f"File not found: {fp}")

        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            for _line_num, line in enumerate(f, 1):
                line = line.strip()

                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue

                cookie = self.parse_cookie_line(line)
                if cookie is None:
                    continue
                target = self.match_domain(cookie["domain"])
                if target is not None:
                    cookie["target_domain"] = target
                    results.append(cookie)

        return results

    def parse_cookie_line(self, line: str) -> Optional[Dict[str, str]]:
        """
        Parse a Netscape cookie format line
        Format: domain flag path secure expiration name value

        Args:
            line: Cookie line to parse

        Returns:
            Dictionary with cookie data or None if invalid
        """
        parts = line.split("\t")

        if len(parts) < 7:
            return None

        try:
            return {
                "domain": parts[0].strip(),
                "flag": parts[1].strip(),
                "path": parts[2].strip(),
                "secure": parts[3].strip(),
                "expiration": parts[4].strip(),
                "name": parts[5].strip(),
                "value": parts[6].strip(),
            }
        except Exception:
            return None

    def match_domain(self, cookie_domain: str) -> Optional[str]:
        """Return the configured target domain that matches *cookie_domain*.

        Match rules (per target):
          * exact match, or
          * cookie domain is a subdomain of the target, or
          * target is a subdomain of the cookie domain (legacy permissive
            behaviour preserved from the original single-domain extractor).

        Returns ``None`` when no configured target matches.
        """
        cookie_domain = cookie_domain.lower().lstrip(".")
        for target in self.domains:
            if (
                cookie_domain == target
                or cookie_domain.endswith("." + target)
                or target.endswith("." + cookie_domain)
            ):
                return target
        return None

    def _matches_domain(self, cookie_domain: str) -> bool:
        """Back-compat alias used by older callers."""
        return self.match_domain(cookie_domain) is not None

    def extract_from_directory(
        self,
        directory: str,
        output_dir: Optional[str] = None,
        realtime_save: bool = False,
    ) -> Dict[str, List[Dict[str, str]]]:
        """
        Recursively extract cookies from all cookie files in directory

        Args:
            directory: Path to directory containing cookie files
            output_dir: Base output directory for real-time saving
            realtime_save: If True, save cookies in real-time to separate files

        Returns:
            Dictionary mapping file paths to cookie lists
        """
        results: Dict[str, List[Dict[str, str]]] = {}
        dir_path = Path(directory)

        if not dir_path.exists():
            raise FileNotFoundError(f"Directory not found: {dir_path}")

        # When real-time saving is enabled, create one sub-directory per
        # configured target domain so output stays cleanly partitioned.
        domain_output_dirs: Dict[str, Path] = {}
        if realtime_save and output_dir:
            for d in self.domains:
                p = Path(output_dir) / d
                p.mkdir(parents=True, exist_ok=True)
                domain_output_dirs[d] = p

        # Find all .txt files in Cookies subdirectories
        cookie_files: List[Path] = []
        try:
            cookie_files = list(dir_path.rglob("Cookies/*.txt"))
        except Exception:
            try:
                for item in dir_path.iterdir():
                    if item.is_dir():
                        cookies_dir = item / "Cookies"
                        if cookies_dir.exists():
                            cookie_files.extend(cookies_dir.glob("*.txt"))
            except Exception:
                pass

        file_counters: Dict[str, int] = {d: 1 for d in self.domains}
        for filepath in cookie_files:
            try:
                cookies = self.extract_from_file(str(filepath))
                if not cookies:
                    continue
                results[str(filepath)] = cookies

                if not (realtime_save and domain_output_dirs):
                    continue

                # Group cookies by their matched target domain.
                grouped: Dict[str, List[Dict[str, str]]] = {}
                for c in cookies:
                    grouped.setdefault(c["target_domain"], []).append(c)

                for target, items in grouped.items():
                    out_dir = domain_output_dirs.get(target)
                    if out_dir is None:
                        continue
                    idx = file_counters[target]
                    output_path = out_dir / f"akaza_{target}_{idx}.txt"
                    with open(output_path, "w", encoding="utf-8") as f:
                        for cookie in items:
                            f.write(
                                f"{cookie['domain']}\t{cookie['flag']}\t{cookie['path']}\t"
                                f"{cookie['secure']}\t{cookie['expiration']}\t"
                                f"{cookie['name']}\t{cookie['value']}\n"
                            )
                    file_counters[target] = idx + 1
            except Exception:
                pass

        return results


# ════════════════════════════════════════════════════════════
#  Async wrapper & archive handling
# ════════════════════════════════════════════════════════════

@dataclass
class ExtractionProgress:
    """Mutable progress state shared between the worker thread and the bot."""
    phase: str = "idle"          # downloading / extracting / scanning / done / failed
    files_total: int = 0
    files_scanned: int = 0
    cookies_found: int = 0
    download_current: int = 0
    download_total: int = 0
    download_start: float = 0.0     # monotonic timestamp when download began
    extract_total: int = 0          # total members in the archive (when known)
    extract_current: int = 0        # members already written to disk
    extract_start: float = 0.0      # monotonic timestamp when extraction began
    current_file: str = ""          # name of the file currently being processed
    cancelled: bool = False


@dataclass
class ExtractionResult:
    """Final outcome of an extraction job."""
    success: bool
    output_files: List[str] = field(default_factory=list)
    cookies_found: int = 0
    files_scanned: int = 0
    error: str = ""
    duration_seconds: float = 0.0
    partial: bool = False           # True when results came from a cancelled job
    # Per-target-domain cookie counts. Empty for legacy single-domain callers
    # that don't care about the breakdown.
    per_domain_counts: Dict[str, int] = field(default_factory=dict)


def _safe_zip_extract(
    zf: zipfile.ZipFile,
    dest: str,
    progress: Optional["ExtractionProgress"] = None,
) -> None:
    """Extract zip with path traversal protection and per-member progress."""
    dest_real = os.path.realpath(dest)
    members = zf.namelist()
    for member in members:
        target = os.path.realpath(os.path.join(dest, member))
        if not target.startswith(dest_real + os.sep) and target != dest_real:
            raise ValueError(f"Path traversal detected in zip: {member}")
    if progress is not None:
        progress.extract_total = len(members)
        progress.extract_current = 0
    for name in members:
        if progress is not None and progress.cancelled:
            return
        if progress is not None:
            progress.current_file = os.path.basename(name) or name
        zf.extract(name, dest)
        if progress is not None:
            progress.extract_current += 1


def _safe_tar_extract(
    tf: tarfile.TarFile,
    dest: str,
    progress: Optional["ExtractionProgress"] = None,
) -> None:
    """Extract tar with path traversal/symlink protection and per-member progress."""
    dest_real = os.path.realpath(dest)
    safe_members = []
    for member in tf.getmembers():
        if member.issym() or member.islnk():
            logger.warning("Skipping symlink/hardlink in tar: {}", member.name)
            continue
        target = os.path.realpath(os.path.join(dest, member.name))
        if not target.startswith(dest_real + os.sep) and target != dest_real:
            raise ValueError(f"Path traversal detected in tar: {member.name}")
        safe_members.append(member)
    if progress is not None:
        progress.extract_total = len(safe_members)
        progress.extract_current = 0
    for member in safe_members:
        if progress is not None and progress.cancelled:
            return
        if progress is not None:
            progress.current_file = os.path.basename(member.name) or member.name
        tf.extract(member, dest)
        if progress is not None:
            progress.extract_current += 1



def _is_split_archive(path: str) -> bool:
    """Detect split/multipart archive naming patterns."""
    base = os.path.basename(path).lower()
    if re.search(r"\.part-?\d+\.zip$", base):
        return True
    if re.search(r"\.part-?\d+\.rar$", base):
        return True
    if re.search(r"\.part-?\d+\.7z$", base):
        return True
    if re.search(r"\.zip\.\d+$", base):
        return True
    if re.search(r"\.7z\.\d+$", base):
        return True
    return False


# Magic-byte signatures used to detect the *actual* archive format,
# regardless of the filename extension the user sent.
_MAGIC_SIGNATURES: List[tuple[bytes, str]] = [
    (b"PK\x03\x04", "zip"),         # standard zip
    (b"PK\x05\x06", "zip"),         # empty zip (EOCD only)
    (b"PK\x07\x08", "zip"),         # spanned zip data descriptor
    (b"Rar!\x1a\x07\x00", "rar"),   # RAR 1.5+
    (b"Rar!\x1a\x07\x01\x00", "rar"),  # RAR 5.0
    (b"7z\xbc\xaf\x27\x1c", "7z"),  # 7z
    (b"\x1f\x8b", "gz"),            # gzip / .tar.gz
    (b"BZh", "bz2"),                # bzip2 / .tar.bz2
    (b"\xfd7zXZ\x00", "xz"),        # xz / .tar.xz
]


def _sniff_archive_type(path: str) -> Optional[str]:
    """Return a normalised archive-type tag based on file magic bytes.

    Returns one of: "zip", "rar", "7z", "gz", "bz2", "xz", or None
    if the file is empty / unreadable / unrecognised.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if not head:
        return None
    for sig, kind in _MAGIC_SIGNATURES:
        if head.startswith(sig):
            return kind
    return None


class _DirCountPoller:
    """Background polling thread that updates progress by counting files in *dest*.

    Acts as a robust fallback when the underlying extraction tool's own
    progress output can't be parsed (e.g. patoolib, or 7z when it streams
    progress on a different fd than we expect). Only ever moves the counter
    forward, never backward.
    """

    def __init__(
        self,
        dest: str,
        progress: Optional["ExtractionProgress"],
        interval: float = 1.5,
    ) -> None:
        self._dest = dest
        self._progress = progress
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "_DirCountPoller":
        if self._progress is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            progress = self._progress
            if progress is None:
                return
            try:
                count = 0
                for _root, _dirs, files in os.walk(self._dest):
                    count += len(files)
                if count > progress.extract_current:
                    progress.extract_current = count
            except Exception:
                pass


def _validate_extracted_paths(dest: str) -> None:
    """Post-extraction check: ensure no file escaped the destination directory."""
    dest_real = os.path.realpath(dest)
    for root, dirs, files in os.walk(dest):
        for name in files + dirs:
            full = os.path.realpath(os.path.join(root, name))
            if not full.startswith(dest_real + os.sep) and full != dest_real:
                raise ValueError(f"Path traversal detected after extraction: {name}")


def _extract_with_7z(
    archive_path: str,
    dest: str,
    progress: Optional["ExtractionProgress"] = None,
) -> None:
    """Extract using 7z command-line tool with live per-file progress.

    7z's ``-bsp2`` option streams progress lines to stderr. We parse them
    so the bot can show ``extract_current / extract_total`` while the
    process runs. A ``_DirCountPoller`` also runs alongside as a fallback
    so the counter advances even if 7z's output format changes.
    """
    import shutil as _shutil

    sz = _shutil.which("7z")
    if not sz:
        raise RuntimeError(
            "7z not found. Install with: apt-get install -y p7zip-full"
        )

    # First, count entries so we can show a real progress bar.
    if progress is not None:
        try:
            count_proc = subprocess.run(
                [sz, "l", "-slt", archive_path],
                capture_output=True, text=True, timeout=120,
            )
            if count_proc.returncode == 0:
                # "Path =" lines, minus the archive header line.
                paths = [
                    ln for ln in count_proc.stdout.splitlines()
                    if ln.startswith("Path = ")
                ]
                progress.extract_total = max(len(paths) - 1, 0)
                progress.extract_current = 0
        except Exception as exc:
            logger.debug("7z list failed ({}); progress count unavailable", exc)

    # Stream extraction with line-buffered stderr so we can update progress.
    # 7z output-control flags:
    #   -bb1   log level 1 (one line per extracted entry: "- relative/path")
    #   -bso2  output stream    -> stderr (fd 2)
    #   -bse2  error messages   -> stderr (fd 2)
    #   -bsp2  progress info    -> stderr (fd 2)
    # We capture stderr below and parse per-file progress lines from it.
    # A directory-count poller runs alongside as a robust fallback so the
    # dashboard advances even if 7z's progress output format changes.
    proc = subprocess.Popen(
        [sz, "x", archive_path, f"-o{dest}", "-y",
         "-bb1", "-bso2", "-bse2", "-bsp2"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_chunks: List[str] = []
    file_lines_seen = 0
    try:
        assert proc.stderr is not None
        with _DirCountPoller(dest, progress):
            for line in proc.stderr:
                stderr_chunks.append(line)
                # 7z streams progress with embedded backspaces and CRs to
                # repaint a TTY counter; strip those before parsing so the
                # regex can find the "- relative/path" suffix.
                cleaned = line.replace("\x08", "").replace("\r", "").strip()
                if not cleaned:
                    continue
                if progress is not None:
                    # Per-file lines (with -bb1) look like:
                    #   "- relative/path/inside/archive"
                    # Combined with progress (-bsp2) they may look like:
                    #   "  3% 12      - relative/path"
                    m = re.search(r"-\s+([^\s].*)$", cleaned)
                    if m:
                        name = m.group(1).strip()
                        progress.current_file = os.path.basename(name) or name
                        file_lines_seen += 1
                        if file_lines_seen > progress.extract_current:
                            progress.extract_current = file_lines_seen
                if progress is not None and progress.cancelled:
                    proc.terminate()
                    break
            proc.wait(timeout=60)
    except Exception:
        proc.kill()
        raise

    if progress is not None and progress.cancelled:
        # Caller will short-circuit; don't raise.
        return
    if proc.returncode != 0:
        raise RuntimeError(
            f"7z extraction failed: {''.join(stderr_chunks).strip()[:500]}"
        )
    _validate_extracted_paths(dest)


def _extract_archive(
    archive_path: str,
    dest: str,
    progress: Optional["ExtractionProgress"] = None,
) -> None:
    """Extract an archive into *dest* using the best available tool.

    Strategy:
    1. Split/multipart archives → 7z directly.
    2. Detect the *actual* archive format from magic bytes (so a file
       mis-named with the wrong extension still works).
    3. Try the cheapest pure-Python handler first (zipfile / tarfile),
       fall through to patoolib for .rar, and finally fall back to 7z
       for anything that didn't extract cleanly.
    Path-traversal protection (ValueError) is never swallowed.
    """
    if not os.path.exists(archive_path):
        raise RuntimeError(f"Archive not found: {archive_path}")
    if os.path.getsize(archive_path) == 0:
        raise RuntimeError(
            "Uploaded file is empty (0 bytes). "
            "Please re-upload a valid archive."
        )

    # Split archives — go straight to 7z
    if _is_split_archive(archive_path):
        logger.info("Split archive detected, using 7z: {}", archive_path)
        _extract_with_7z(archive_path, dest, progress)
        return

    # Determine the real archive format from the file's magic bytes; if
    # that's inconclusive, fall back to the filename extension. This
    # prevents e.g. a 7z file mis-named as .zip from killing the job
    # with "File is not a zip file".
    sniffed = _sniff_archive_type(archive_path)
    lower = archive_path.lower()
    if sniffed is None:
        if lower.endswith(".zip"):
            sniffed = "zip"
        elif lower.endswith((".tar.gz", ".tgz")):
            sniffed = "gz"
        elif lower.endswith(".tar.bz2"):
            sniffed = "bz2"
        elif lower.endswith(".rar"):
            sniffed = "rar"
        elif lower.endswith(".7z"):
            sniffed = "7z"

    if sniffed != _ext_kind(lower):
        logger.info(
            "Archive content ({}) differs from extension ({}); routing by content",
            sniffed, _ext_kind(lower),
        )

    # Pure-Python zip
    if sniffed == "zip":
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                _safe_zip_extract(zf, dest, progress)
            return
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("zipfile failed ({}), falling back to 7z", exc)
            _extract_with_7z(archive_path, dest, progress)
            return

    # Pure-Python tarball (gzip / bzip2 / xz / plain tar)
    if sniffed in ("gz", "bz2", "xz"):
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                _safe_tar_extract(tf, dest, progress)
            return
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("tarfile failed ({}), falling back to 7z", exc)
            _extract_with_7z(archive_path, dest, progress)
            return

    # .rar / .7z / unknown — try patoolib first, then 7z
    import shutil as _shutil

    has_tool = (
        _shutil.which("unrar") or _shutil.which("7z") or _shutil.which("unar")
    )
    if not has_tool:
        raise RuntimeError(
            "No extraction tool found for this archive format. "
            "Install p7zip-full on the server: "
            "apt-get install -y p7zip-full"
        )
    try:
        import patoolib
        # patoolib has no progress callback, so a directory-count poller is
        # the only way to advance the dashboard during this step.
        with _DirCountPoller(dest, progress):
            patoolib.extract_archive(archive_path, outdir=dest, interactive=False)
        _validate_extracted_paths(dest)
        return
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("patoolib failed ({}), falling back to 7z", exc)
        _extract_with_7z(archive_path, dest, progress)


def _ext_kind(lower_path: str) -> Optional[str]:
    """Return the archive-kind tag implied by a (lowercased) filename."""
    if lower_path.endswith(".zip"):
        return "zip"
    if lower_path.endswith((".tar.gz", ".tgz")):
        return "gz"
    if lower_path.endswith(".tar.bz2"):
        return "bz2"
    if lower_path.endswith(".rar"):
        return "rar"
    if lower_path.endswith(".7z"):
        return "7z"
    return None


def _write_output_chunks(
    cookies: Iterable[Tuple[str, str]],
    output_dir: str,
    domains: Iterable[str],
) -> List[str]:
    """
    Stream ``(target_domain, cookie_line)`` tuples into per-domain
    ``<=OUTPUT_CHUNK_SIZE_BYTES`` chunk files. Each domain gets its own
    independent chunk counter so output filenames look like
    ``spotify.com_cookies_part1.txt``, ``netflix.com_cookies_part1.txt``…

    Returns the list of all output file paths created (across every domain).
    """

    domains = list(domains)

    @dataclass
    class _Bucket:
        domain: str
        chunk_idx: int = 1
        current_size: int = 0
        fh: Optional["object"] = None  # type: ignore[type-arg]
        path: str = ""

    buckets: Dict[str, _Bucket] = {d: _Bucket(domain=d) for d in domains}
    paths: List[str] = []

    def _open_chunk(b: _Bucket) -> None:
        b.path = os.path.join(
            output_dir, f"{b.domain}_cookies_part{b.chunk_idx}.txt"
        )
        paths.append(b.path)
        b.fh = open(b.path, "w", encoding="utf-8")
        b.current_size = 0

    try:
        for target_domain, line in cookies:
            b = buckets.get(target_domain)
            if b is None:
                # Unknown target — create a bucket on the fly so we never
                # silently drop cookies.
                b = _Bucket(domain=target_domain)
                buckets[target_domain] = b
            if b.fh is None:
                _open_chunk(b)
            encoded = line.encode("utf-8")
            if (
                b.current_size + len(encoded) > config.OUTPUT_CHUNK_SIZE_BYTES
                and b.current_size > 0
            ):
                b.fh.close()  # type: ignore[union-attr]
                b.chunk_idx += 1
                _open_chunk(b)
            b.fh.write(line)  # type: ignore[union-attr]
            b.current_size += len(encoded)
    finally:
        for b in buckets.values():
            if b.fh is not None:
                try:
                    b.fh.close()  # type: ignore[union-attr]
                except Exception:
                    pass

    return paths


def _coerce_domains(domain: Union[str, Iterable[str]]) -> List[str]:
    """Normalise the ``domain`` parameter into a non-empty list of domains."""
    if isinstance(domain, str):
        domains = [domain]
    else:
        domains = list(domain)
    cleaned: List[str] = []
    seen: set[str] = set()
    for d in domains:
        norm = d.lower().lstrip(".").strip()
        if norm and norm not in seen:
            seen.add(norm)
            cleaned.append(norm)
    if not cleaned:
        raise ValueError("At least one domain must be provided")
    return cleaned


def _run_extraction(
    archive_path: str,
    domain: Union[str, Iterable[str]],
    progress: ExtractionProgress,
) -> ExtractionResult:
    """Blocking extraction — meant to run inside ``asyncio.to_thread``.

    ``domain`` may be a single domain string (legacy) or any iterable of
    domain strings. When multiple domains are supplied, each cookie file is
    scanned once and matched cookies are routed to per-domain output files.
    """
    import time

    start = time.monotonic()
    domains = _coerce_domains(domain)
    per_domain_counts: Dict[str, int] = {d: 0 for d in domains}
    temp_dir = tempfile.mkdtemp(dir=str(config.TEMP_DIR))
    output_dir = tempfile.mkdtemp(dir=str(config.TEMP_DIR))

    try:
        # Phase 1: extract archive
        progress.phase = "extracting"
        progress.extract_start = time.monotonic()
        progress.current_file = ""
        logger.info(
            "Extracting archive {} into {} for domains={}",
            archive_path, temp_dir, domains,
        )
        _extract_archive(archive_path, temp_dir, progress)

        if progress.cancelled:
            # Nothing useful to send if the user cancelled mid-extraction.
            return ExtractionResult(
                success=False,
                error="Cancelled by user before any files were scanned",
                duration_seconds=time.monotonic() - start,
                partial=True,
                per_domain_counts=per_domain_counts,
            )

        # Phase 2: scan files
        progress.phase = "scanning"
        extractor = SmartCookieExtractor(domains)

        all_files: List[str] = []
        for root, _dirs, files in os.walk(temp_dir):
            for fname in files:
                all_files.append(os.path.join(root, fname))
        progress.files_total = len(all_files)

        def _cookie_generator() -> Generator[Tuple[str, str], None, None]:
            for fpath in all_files:
                if progress.cancelled:
                    # Stop the generator cleanly so any cookies already
                    # written to the current chunk file are flushed by
                    # _write_output_chunks' final fh.close().
                    return
                progress.current_file = os.path.basename(fpath)
                try:
                    cookies = extractor.extract_from_file(fpath)
                    for c in cookies:
                        target = c.get("target_domain", domains[0])
                        line = (
                            f"{c['domain']}\t{c['flag']}\t{c['path']}\t"
                            f"{c['secure']}\t{c['expiration']}\t"
                            f"{c['name']}\t{c['value']}\n"
                        )
                        per_domain_counts[target] = (
                            per_domain_counts.get(target, 0) + 1
                        )
                        progress.cookies_found += 1
                        yield target, line
                except Exception:
                    pass
                progress.files_scanned += 1

        output_files = _write_output_chunks(
            _cookie_generator(), output_dir, domains,
        )

        # Drop empty chunk files (e.g. cancelled before any cookie was found,
        # or domains that simply matched zero cookies).
        output_files = [
            p for p in output_files
            if os.path.exists(p) and os.path.getsize(p) > 0
        ]

        duration = time.monotonic() - start

        if progress.cancelled:
            progress.phase = "cancelled"
            return ExtractionResult(
                success=bool(output_files),
                output_files=output_files,
                cookies_found=progress.cookies_found,
                files_scanned=progress.files_scanned,
                duration_seconds=duration,
                partial=True,
                error="" if output_files else "Cancelled by user (no cookies found yet)",
                per_domain_counts=per_domain_counts,
            )

        progress.phase = "done"
        return ExtractionResult(
            success=True,
            output_files=output_files,
            cookies_found=progress.cookies_found,
            files_scanned=progress.files_scanned,
            duration_seconds=duration,
            per_domain_counts=per_domain_counts,
        )

    except Exception as exc:
        logger.exception("Extraction failed")
        progress.phase = "failed"
        return ExtractionResult(
            success=False,
            error=str(exc),
            duration_seconds=time.monotonic() - start,
            per_domain_counts=per_domain_counts,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        # output_dir is cleaned up by the caller after sending files


async def run_extraction_async(
    archive_path: str,
    domain: Union[str, Iterable[str]],
    progress: ExtractionProgress,
) -> ExtractionResult:
    """Non-blocking facade — offloads heavy work to a thread.

    ``domain`` may be either a single domain string (legacy callers) or an
    iterable of domain strings (multi-domain callers).
    """
    return await asyncio.to_thread(_run_extraction, archive_path, domain, progress)
