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
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Generator, List, Optional

from loguru import logger

import config


# ════════════════════════════════════════════════════════════
#  SmartCookieExtractor  — REUSED EXACTLY AS-IS
# ════════════════════════════════════════════════════════════

class SmartCookieExtractor:
    """Efficiently extracts cookies from Netscape cookie format files"""

    def __init__(self, domain: str, patterns: Optional[List[str]] = None):
        """
        Initialize extractor

        Args:
            domain: Domain to filter cookies (e.g., 'spotify.com')
            patterns: Optional regex patterns for additional filtering
        """
        self.domain = domain.lower()
        self.patterns = patterns or []
        self.domain_pattern = re.compile(rf"({re.escape(self.domain)})", re.IGNORECASE)

    def extract_from_file(self, filepath: str) -> List[Dict[str, str]]:
        """
        Extract cookies from Netscape format cookie file

        Args:
            filepath: Path to cookie file

        Returns:
            List of cookie dictionaries
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
                if cookie and self._matches_domain(cookie["domain"]):
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

    def _matches_domain(self, cookie_domain: str) -> bool:
        """Check if cookie domain matches the target domain"""
        cookie_domain = cookie_domain.lower().lstrip(".")
        target_domain = self.domain.lstrip(".")

        # Exact match or subdomain match
        return (
            cookie_domain == target_domain
            or cookie_domain.endswith("." + target_domain)
            or target_domain.endswith("." + cookie_domain)
        )

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

        # Setup output directory structure if real-time saving
        domain_output_dir: Optional[Path] = None
        if realtime_save and output_dir:
            domain_output_dir = Path(output_dir) / self.domain
            domain_output_dir.mkdir(parents=True, exist_ok=True)

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

        file_counter = 1
        for i, filepath in enumerate(cookie_files, 1):
            try:
                cookies = self.extract_from_file(str(filepath))
                if cookies:
                    results[str(filepath)] = cookies

                    if realtime_save and domain_output_dir:
                        output_filename = f"akaza_{self.domain}_{file_counter}.txt"
                        output_path = domain_output_dir / output_filename

                        with open(output_path, "w", encoding="utf-8") as f:
                            for cookie in cookies:
                                f.write(
                                    f"{cookie['domain']}\t{cookie['flag']}\t{cookie['path']}\t"
                                    f"{cookie['secure']}\t{cookie['expiration']}\t"
                                    f"{cookie['name']}\t{cookie['value']}\n"
                                )

                        file_counter += 1
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



def _probe_encrypted_entries(archive_path: str) -> List[str]:
    """Return a list of password-protected entry names inside *archive_path*.

    Returns an empty list if the archive has no encrypted entries, or if
    we can't tell (missing tools, unknown format). Never raises.

    Detection order:
      * For ``.rar``: prefer ``unrar lt -p-`` and look for ``Flags: enc``.
      * Fallback / other formats: ``7z l -slt`` and look for
        ``Encrypted = +``.
    """
    import shutil as _shutil

    lower = archive_path.lower()
    encrypted: List[str] = []

    if lower.endswith(".rar"):
        unrar = _shutil.which("unrar")
        if unrar:
            try:
                proc = subprocess.run(
                    [unrar, "lt", "-p-", archive_path],
                    capture_output=True, text=True, timeout=60,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                # ``lt`` (technical listing) emits blocks like:
                #     Name: foo.txt
                #     ...
                #     Flags: encrypted
                # We parse blocks split on "Name:" lines.
                blocks = re.split(r"(?m)^Name:\s+", proc.stdout)
                for blk in blocks[1:]:
                    first_nl = blk.find("\n")
                    name = blk[:first_nl].strip() if first_nl >= 0 else blk.strip()
                    if re.search(r"(?mi)^\s*Flags:.*encrypted", blk):
                        encrypted.append(name)
                if encrypted:
                    return encrypted
            except Exception as exc:
                logger.debug("unrar probe failed ({}); falling back", exc)

    sz = _shutil.which("7z")
    if sz:
        try:
            proc = subprocess.run(
                [sz, "l", "-slt", archive_path],
                capture_output=True, text=True, timeout=60,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            cur_name: Optional[str] = None
            for line in proc.stdout.splitlines():
                if line.startswith("Path = "):
                    cur_name = line[len("Path = "):].strip()
                elif line.startswith("Encrypted = +") and cur_name:
                    encrypted.append(cur_name)
        except Exception as exc:
            logger.debug("7z probe failed ({})", exc)

    return encrypted


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
    password: Optional[str] = None,
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
    # stdin=DEVNULL + start_new_session=True for the listing step too:
    # archives with encrypted headers (``-mhe=on``) require the password
    # to even read the file list, and 7z would otherwise hang prompting
    # before extraction even begins.
    list_cmd = [sz, "l", "-slt", archive_path]
    if password:
        list_cmd.append(f"-p{password}")
    if progress is not None:
        try:
            count_proc = subprocess.run(
                list_cmd,
                capture_output=True, text=True, timeout=60,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
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
    # stdin=DEVNULL + start_new_session=True together neutralise every way
    # 7z could otherwise hang on a password prompt:
    #   * stdin=DEVNULL gives 7z immediate EOF when it tries to read input.
    #   * start_new_session=True puts 7z in its own process group with no
    #     controlling terminal, so even if it tries to open /dev/tty
    #     directly to bypass stdin (some builds do that), the open fails.
    # Combined effect: 7z fails any password-protected entry with a
    # non-zero exit code rather than blocking forever.
    extract_cmd = [
        sz, "x", archive_path, f"-o{dest}", "-y",
        "-bb1", "-bso2", "-bse2", "-bsp2",
    ]
    if password:
        extract_cmd.append(f"-p{password}")
    proc = subprocess.Popen(
        extract_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    stderr_chunks: List[str] = []
    file_lines_seen = 0
    # Watchdog: if neither the line parser nor the directory poller
    # advance progress for this many seconds, assume the underlying tool
    # is wedged and kill it. Picked generously so big-file decompression
    # still finishes naturally.
    WATCHDOG_IDLE_SECONDS = 90.0
    last_progress_count = 0
    last_progress_time = time.monotonic()
    stop_watchdog = threading.Event()

    def _watchdog() -> None:
        nonlocal last_progress_count, last_progress_time
        while not stop_watchdog.wait(5.0):
            if proc.poll() is not None:
                return
            current = (
                progress.extract_current
                if progress is not None
                else file_lines_seen
            )
            if current > last_progress_count:
                last_progress_count = current
                last_progress_time = time.monotonic()
                continue
            if time.monotonic() - last_progress_time > WATCHDOG_IDLE_SECONDS:
                logger.error(
                    "7z made no progress for {}s (stuck at {} entries); "
                    "terminating.",
                    int(WATCHDOG_IDLE_SECONDS), current,
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                return

    watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
    watchdog_thread.start()

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
    finally:
        stop_watchdog.set()
        watchdog_thread.join(timeout=2.0)

    if progress is not None and progress.cancelled:
        # Caller will short-circuit; don't raise.
        return

    # Ground-truth the file count — see identical logic in
    # _extract_with_unrar for the reasoning.
    actual_count = 0
    for _r, _d, files in os.walk(dest):
        actual_count += len(files)
    if progress is not None:
        progress.extract_current = max(progress.extract_current, actual_count)

    if proc.returncode != 0:
        # If at least some files made it out, treat this as a partial success
        # rather than aborting the whole job. Common cause: a single
        # password-protected entry inside an otherwise-fine archive (e.g. a
        # bundled "KeyGen.rar" inside a log dump). We still want the cookies
        # from the 95% that extracted cleanly.
        if actual_count > 0:
            logger.warning(
                "7z exited with code {} after extracting {} files; "
                "treating as partial success. Last error output: {}",
                proc.returncode,
                actual_count,
                "".join(stderr_chunks[-5:]).strip()[:200],
            )
        else:
            raise RuntimeError(
                f"7z extraction failed: {''.join(stderr_chunks).strip()[:2000]}"
            )
    _validate_extracted_paths(dest)


def _extract_with_unrar(
    archive_path: str,
    dest: str,
    progress: Optional["ExtractionProgress"] = None,
    password: Optional[str] = None,
) -> None:
    """Extract a .rar archive with the proprietary ``unrar`` binary.

    unrar is RARLab's reference implementation and is the only free tool
    that correctly handles RAR5 format plus per-entry password protection.
    We pass ``-p-`` to make it skip password-protected entries silently
    instead of prompting, and ``-o+`` to overwrite any conflicts.

    A ``_DirCountPoller`` advances the dashboard (unrar doesn't stream
    per-file progress in a structured format), and the same watchdog
    pattern as 7z guards against hangs.
    """
    import shutil as _shutil

    unrar = _shutil.which("unrar")
    if not unrar:
        raise RuntimeError("unrar not found")

    # ``-p<password>`` unlocks encrypted entries without prompting.
    # ``-p-`` keeps the old skip-encrypted behaviour when no password
    # was provided.
    pw_flag = f"-p{password}" if password else "-p-"

    # Count entries first for the progress bar.
    if progress is not None:
        try:
            count_proc = subprocess.run(
                [unrar, "lb", pw_flag, archive_path],
                capture_output=True, text=True, timeout=60,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            if count_proc.returncode == 0:
                lines = [
                    ln for ln in count_proc.stdout.splitlines() if ln.strip()
                ]
                progress.extract_total = len(lines)
                progress.extract_current = 0
        except Exception as exc:
            logger.debug(
                "unrar list failed ({}); progress count unavailable", exc
            )

    # ``x``    extract with full paths
    # ``-p-``  never ask for password; skip encrypted entries with error
    # ``-o+``  overwrite existing files without prompting
    # ``-y``   yes to all queries
    # ``-idq`` quiet mode (reduce noise)
    proc = subprocess.Popen(
        [unrar, "x", pw_flag, "-o+", "-y", archive_path, dest + os.sep],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    output_chunks: List[str] = []
    WATCHDOG_IDLE_SECONDS = 90.0
    last_progress_count = 0
    last_progress_time = time.monotonic()
    stop_watchdog = threading.Event()

    def _watchdog() -> None:
        nonlocal last_progress_count, last_progress_time
        while not stop_watchdog.wait(5.0):
            if proc.poll() is not None:
                return
            current = progress.extract_current if progress is not None else 0
            if current > last_progress_count:
                last_progress_count = current
                last_progress_time = time.monotonic()
                continue
            if time.monotonic() - last_progress_time > WATCHDOG_IDLE_SECONDS:
                logger.error(
                    "unrar made no progress for {}s (stuck at {} entries); "
                    "terminating.",
                    int(WATCHDOG_IDLE_SECONDS), current,
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                return

    watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
    watchdog_thread.start()

    try:
        assert proc.stdout is not None
        with _DirCountPoller(dest, progress):
            for line in proc.stdout:
                output_chunks.append(line)
                cleaned = line.strip()
                if not cleaned:
                    continue
                # unrar emits lines like "Extracting  dir/file.txt"
                m = re.match(r"Extracting\s+(.+?)(?:\s+OK\s*)?$", cleaned)
                if m and progress is not None:
                    name = m.group(1).strip()
                    progress.current_file = os.path.basename(name) or name
                if progress is not None and progress.cancelled:
                    proc.terminate()
                    break
            proc.wait(timeout=60)
    except Exception:
        proc.kill()
        raise
    finally:
        stop_watchdog.set()
        watchdog_thread.join(timeout=2.0)

    if progress is not None and progress.cancelled:
        return

    # Ground-truth the extracted file count by walking the destination
    # directory. _DirCountPoller may have missed the final state, and
    # progress.extract_current can lag behind reality right after the
    # process exits. This gives us an authoritative number to decide
    # whether we got partial success.
    actual_count = 0
    for _r, _d, files in os.walk(dest):
        actual_count += len(files)
    if progress is not None:
        progress.extract_current = max(progress.extract_current, actual_count)

    # unrar exit codes:
    #   0    success
    #   1    non-fatal warning (still success for us)
    #   3    corrupt header / CRC (can be partial)
    #   10   nothing to extract (hard failure if count==0, partial otherwise)
    #   11   wrong password — an archive-wide or per-entry password issue;
    #        with -p- any encrypted entry triggers this. If other entries
    #        extracted cleanly this is a partial success.
    # Anything else we treat as a hard failure iff nothing was extracted.
    if proc.returncode not in (0, 1):
        if actual_count > 0:
            logger.warning(
                "unrar exited with code {} after extracting {} files; "
                "treating as partial success (archive likely has "
                "password-protected entries). Last output: {}",
                proc.returncode,
                actual_count,
                "".join(output_chunks[-5:]).strip()[:200],
            )
        else:
            raise RuntimeError(
                f"unrar extraction failed (exit {proc.returncode}): "
                f"{''.join(output_chunks).strip()[:2000]}"
            )
    _validate_extracted_paths(dest)


def _extract_archive(
    archive_path: str,
    dest: str,
    progress: Optional["ExtractionProgress"] = None,
    password: Optional[str] = None,
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
        _extract_with_7z(archive_path, dest, progress, password=password)
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

    # Pure-Python zip. Skip it when a password is provided; stdlib
    # ``zipfile`` only supports the weak ZipCrypto password format, and
    # falling straight to 7z gives us AES-protected zip support for free.
    if sniffed == "zip" and not password:
        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                _safe_zip_extract(zf, dest, progress)
            return
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("zipfile failed ({}), falling back to 7z", exc)
            _extract_with_7z(archive_path, dest, progress, password=password)
            return
    if sniffed == "zip":  # password provided — go straight to 7z
        _extract_with_7z(archive_path, dest, progress, password=password)
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
            _extract_with_7z(archive_path, dest, progress, password=password)
            return

    # .rar / .7z / unknown.
    #
    # For ``.rar``, prefer the proprietary ``unrar`` binary when installed:
    # it's the RARLab reference implementation, is the only free tool that
    # handles RAR5 correctly, and has a native ``-p-`` flag that skips
    # password-protected entries non-interactively. This matters because
    # p7zip 16.02 (the Debian 12 default) has no RAR5 support and hangs
    # on per-entry encryption even with stdin=DEVNULL / setsid.
    #
    # Fall through to ``_extract_with_7z`` if unrar isn't available or
    # failed. Both paths are hardened against stdin prompts and have a
    # watchdog. patoolib is kept as an absolute last resort for exotic
    # formats (e.g. ACE, ARJ) that neither 7z nor unrar handle.
    import shutil as _shutil

    has_tool = (
        _shutil.which("unrar") or _shutil.which("7z") or _shutil.which("unar")
    )
    if not has_tool:
        raise RuntimeError(
            "No extraction tool found for this archive format. "
            "Install p7zip-full + unrar on the server: "
            "apt-get install -y p7zip-full unrar"
        )

    unrar_path = _shutil.which("unrar")
    sevenz_path = _shutil.which("7z")
    logger.info(
        "Extracting {} (sniffed={}); available tools: unrar={}, 7z={}",
        archive_path, sniffed, unrar_path, sevenz_path,
    )

    errors: List[str] = []

    # Try unrar first for .rar files (best RAR5 + encrypted-entry support).
    if sniffed == "rar" and unrar_path:
        logger.info("Trying unrar first for {}", archive_path)
        try:
            _extract_with_unrar(archive_path, dest, progress, password=password)
            logger.info("unrar extraction succeeded for {}", archive_path)
            return
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("unrar extraction failed ({}), falling back to 7z", exc)
            errors.append(f"unrar: {exc}")

    # Try 7z next.
    if sevenz_path:
        logger.info("Trying 7z for {}", archive_path)
        try:
            _extract_with_7z(archive_path, dest, progress, password=password)
            logger.info("7z extraction succeeded for {}", archive_path)
            return
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("7z extraction failed ({}), trying patoolib", exc)
            errors.append(f"7z: {exc}")

    # Last resort: patoolib.
    logger.info("Trying patoolib as final fallback for {}", archive_path)
    try:
        import patoolib
        with _DirCountPoller(dest, progress):
            patoolib.extract_archive(archive_path, outdir=dest, interactive=False)
        _validate_extracted_paths(dest)
        logger.info("patoolib extraction succeeded for {}", archive_path)
        return
    except ValueError:
        raise
    except Exception as exc:
        errors.append(f"patoolib: {exc}")

    # Everything failed. Surface every tool's error — truncate each
    # tool's message so the combined string stays within Telegram's
    # 4096-char message limit.
    combined = "; ".join(
        f"[{e[:700]}{'...' if len(e) > 700 else ''}]" for e in errors
    )
    raise RuntimeError(
        f"All extraction tools failed on this archive. Errors: {combined}"
    )


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
    cookies: Generator[str, None, None],
    output_dir: str,
    domain: str,
) -> List[str]:
    """
    Stream cookie lines into <=45 MB chunk files.
    Returns list of output file paths.
    """
    chunk_idx = 1
    current_size = 0
    paths: List[str] = []

    def _open_chunk() -> "tuple[str, object]":
        p = os.path.join(output_dir, f"{domain}_cookies_part{chunk_idx}.txt")
        paths.append(p)
        return p, open(p, "w", encoding="utf-8")

    path, fh = _open_chunk()
    for line in cookies:
        encoded = line.encode("utf-8")
        if current_size + len(encoded) > config.OUTPUT_CHUNK_SIZE_BYTES and current_size > 0:
            fh.close()  # type: ignore[union-attr]
            chunk_idx += 1
            current_size = 0
            path, fh = _open_chunk()
        fh.write(line)  # type: ignore[union-attr]
        current_size += len(encoded)
    fh.close()  # type: ignore[union-attr]
    return paths


def _run_extraction(
    archive_path: str,
    domain: str,
    progress: ExtractionProgress,
    password: Optional[str] = None,
) -> ExtractionResult:
    """Blocking extraction — meant to run inside ``asyncio.to_thread``."""
    import time

    start = time.monotonic()
    temp_dir = tempfile.mkdtemp(dir=str(config.TEMP_DIR))
    output_dir = tempfile.mkdtemp(dir=str(config.TEMP_DIR))

    try:
        # Phase 1: extract archive
        progress.phase = "extracting"
        progress.extract_start = time.monotonic()
        progress.current_file = ""
        logger.info("Extracting archive {} into {}", archive_path, temp_dir)
        _extract_archive(archive_path, temp_dir, progress, password=password)

        if progress.cancelled:
            # Nothing useful to send if the user cancelled mid-extraction.
            return ExtractionResult(
                success=False,
                error="Cancelled by user before any files were scanned",
                duration_seconds=time.monotonic() - start,
                partial=True,
            )

        # Phase 2: scan files
        progress.phase = "scanning"
        extractor = SmartCookieExtractor(domain)

        all_files: List[str] = []
        for root, _dirs, files in os.walk(temp_dir):
            for fname in files:
                all_files.append(os.path.join(root, fname))
        progress.files_total = len(all_files)

        def _cookie_generator() -> Generator[str, None, None]:
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
                        yield (
                            f"{c['domain']}\t{c['flag']}\t{c['path']}\t"
                            f"{c['secure']}\t{c['expiration']}\t"
                            f"{c['name']}\t{c['value']}\n"
                        )
                        progress.cookies_found += 1
                except Exception:
                    pass
                progress.files_scanned += 1

        output_files = _write_output_chunks(_cookie_generator(), output_dir, domain)

        # Drop empty chunk files (e.g. cancelled before any cookie was found).
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
            )

        progress.phase = "done"
        return ExtractionResult(
            success=True,
            output_files=output_files,
            cookies_found=progress.cookies_found,
            files_scanned=progress.files_scanned,
            duration_seconds=duration,
        )

    except Exception as exc:
        logger.exception("Extraction failed")
        progress.phase = "failed"
        return ExtractionResult(
            success=False,
            error=str(exc),
            duration_seconds=time.monotonic() - start,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        # output_dir is cleaned up by the caller after sending files


async def run_extraction_async(
    archive_path: str,
    domain: str,
    progress: ExtractionProgress,
    password: Optional[str] = None,
) -> ExtractionResult:
    """Non-blocking facade — offloads heavy work to a thread."""
    return await asyncio.to_thread(
        _run_extraction, archive_path, domain, progress, password
    )


async def probe_encrypted_entries_async(archive_path: str) -> List[str]:
    """Async wrapper around ``_probe_encrypted_entries``."""
    return await asyncio.to_thread(_probe_encrypted_entries, archive_path)
