"""
Input validation utilities.
"""

from __future__ import annotations

import re
from typing import List, Tuple, Union

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

# Splits user input into individual domain tokens. We accept commas,
# semicolons, pipes, and any whitespace (incl. newlines) as separators.
_DOMAIN_SPLIT_RE = re.compile(r"[,\s;|]+")


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


def validate_domains(
    raw: str,
    max_count: int = 10,
) -> Tuple[bool, Union[List[str], str]]:
    """
    Validate and normalise a list of domains supplied as a single string.

    Accepts any combination of commas, semicolons, pipes, spaces and
    newlines as separators. Each token is run through :func:`validate_domain`,
    duplicates are removed (preserving the user's original ordering), and the
    final list is capped at *max_count* entries.

    Returns:
        (True,  list_of_cleaned_domains)  on success
        (False, error_message)            on the first validation failure
    """
    if not raw or not raw.strip():
        return False, "Please send at least one domain (e.g. spotify.com)"

    tokens = [t for t in _DOMAIN_SPLIT_RE.split(raw.strip()) if t]
    if not tokens:
        return False, "Please send at least one domain (e.g. spotify.com)"

    if len(tokens) > max_count:
        return False, (
            f"Too many domains ({len(tokens)}). "
            f"Maximum allowed per extraction: {max_count}."
        )

    cleaned: List[str] = []
    seen: set[str] = set()
    for tok in tokens:
        ok, value = validate_domain(tok)
        if not ok:
            return False, value
        if value not in seen:
            seen.add(value)
            cleaned.append(value)

    return True, cleaned


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
