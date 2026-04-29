"""
Input validation utilities.
"""

from __future__ import annotations

import re
from typing import Tuple

# Accepted archive extensions and their MIME types
SUPPORTED_EXTENSIONS: dict[str, list[str]] = {
    ".zip": ["application/zip", "application/x-zip-compressed"],
    ".rar": ["application/x-rar-compressed", "application/vnd.rar"],
    ".7z": ["application/x-7z-compressed"],
    ".tar.gz": ["application/gzip", "application/x-gzip", "application/x-tar"],
    ".tar.bz2": ["application/x-bzip2"],
    ".tgz": ["application/gzip"],
}

_DOMAIN_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,}$"
)


def validate_domain(raw: str) -> Tuple[bool, str]:
    """
    Validate and normalise a domain string.

    Returns:
        (is_valid, cleaned_domain_or_error)
    """
    domain = raw.strip().lower()
    domain = domain.removeprefix("http://").removeprefix("https://")
    domain = domain.split("/")[0]
    domain = domain.split(":")[0]
    if not domain or "." not in domain:
        return False, "Domain must contain at least one dot (e.g. spotify.com)"
    if not _DOMAIN_RE.match(domain):
        return False, f"Invalid domain format: {domain}"
    return True, domain


def validate_archive(file_name: str | None, mime_type: str | None) -> Tuple[bool, str]:
    """
    Check whether a file looks like a supported archive.

    Returns:
        (is_valid, cleaned_extension_or_error)
    """
    if not file_name:
        return False, "No filename provided"
    lower = file_name.lower()
    for ext in SUPPORTED_EXTENSIONS:
        if lower.endswith(ext):
            return True, ext
    return False, (
        f"Unsupported file type. Supported formats: "
        f"{', '.join(SUPPORTED_EXTENSIONS.keys())}"
    )
