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


def _safe_zip_extract(zf: zipfile.ZipFile, dest: str) -> None:
    """Extract zip with path traversal protection."""
    dest_real = os.path.realpath(dest)
    for member in zf.namelist():
        target = os.path.realpath(os.path.join(dest, member))
        if not target.startswith(dest_real + os.sep) and target != dest_real:
            raise ValueError(f"Path traversal detected in zip: {member}")
    zf.extractall(dest)


def _safe_tar_extract(tf: tarfile.TarFile, dest: str) -> None:
    """Extract tar with path traversal and symlink protection."""
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
    tf.extractall(dest, members=safe_members)



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


def _validate_extracted_paths(dest: str) -> None:
    """Post-extraction check: ensure no file escaped the destination directory."""
    dest_real = os.path.realpath(dest)
    for root, dirs, files in os.walk(dest):
        for name in files + dirs:
            full = os.path.realpath(os.path.join(root, name))
            if not full.startswith(dest_real + os.sep) and full != dest_real:
                raise ValueError(f"Path traversal detected after extraction: {name}")


def _extract_with_7z(archive_path: str, dest: str) -> None:
    """Extract using 7z command-line tool (handles split archives, damaged files, etc.)."""
    import shutil as _shutil

    sz = _shutil.which("7z")
    if not sz:
        raise RuntimeError(
            "7z not found. Install with: apt-get install -y p7zip-full"
        )
    result = subprocess.run(
        [sz, "x", archive_path, f"-o{dest}", "-y", "-bso0", "-bse1"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"7z extraction failed: {result.stderr.strip()}")
    _validate_extracted_paths(dest)


def _extract_archive(archive_path: str, dest: str) -> None:
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
        _extract_with_7z(archive_path, dest)
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
                _safe_zip_extract(zf, dest)
            return
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("zipfile failed ({}), falling back to 7z", exc)
            _extract_with_7z(archive_path, dest)
            return

    # Pure-Python tarball (gzip / bzip2 / xz / plain tar)
    if sniffed in ("gz", "bz2", "xz"):
        try:
            with tarfile.open(archive_path, "r:*") as tf:
                _safe_tar_extract(tf, dest)
            return
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("tarfile failed ({}), falling back to 7z", exc)
            _extract_with_7z(archive_path, dest)
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
        patoolib.extract_archive(archive_path, outdir=dest, interactive=False)
        _validate_extracted_paths(dest)
        return
    except ValueError:
        raise
    except Exception as exc:
        logger.warning("patoolib failed ({}), falling back to 7z", exc)
        _extract_with_7z(archive_path, dest)


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
) -> ExtractionResult:
    """Blocking extraction — meant to run inside ``asyncio.to_thread``."""
    import time

    start = time.monotonic()
    temp_dir = tempfile.mkdtemp(dir=str(config.TEMP_DIR))
    output_dir = tempfile.mkdtemp(dir=str(config.TEMP_DIR))

    try:
        # Phase 1: extract archive
        progress.phase = "extracting"
        logger.info("Extracting archive {} into {}", archive_path, temp_dir)
        _extract_archive(archive_path, temp_dir)

        if progress.cancelled:
            return ExtractionResult(success=False, error="Cancelled by user")

        # Phase 2: scan files
        progress.phase = "scanning"
        extractor = SmartCookieExtractor(domain)

        all_files: List[str] = []
        for root, _dirs, files in os.walk(temp_dir):
            for fname in files:
                all_files.append(os.path.join(root, fname))
        progress.files_total = len(all_files)

        def _cookie_generator() -> Generator[str, None, None]:
            batch_count = 0
            for fpath in all_files:
                if progress.cancelled:
                    return
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
                batch_count += 1

        output_files = _write_output_chunks(_cookie_generator(), output_dir, domain)

        if progress.cancelled:
            return ExtractionResult(success=False, error="Cancelled by user")

        duration = time.monotonic() - start
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
) -> ExtractionResult:
    """Non-blocking facade — offloads heavy work to a thread."""
    return await asyncio.to_thread(_run_extraction, archive_path, domain, progress)
