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

# Splits user input into individual domain tokens.
#
# We accept the obvious ASCII separators (comma, semicolon, pipe, slash,
# any whitespace) **and** the Unicode-punctuation versions that mobile
# keyboards often auto-substitute:
#   ， (U+FF0C fullwidth comma — Android/iOS smart-quote)
#   、 (U+3001 ideographic comma)
#   ； (U+FF1B fullwidth semicolon)
#   ｜ (U+FF5C fullwidth vertical bar)
#   ・ (U+30FB katakana middle dot)
#   · (U+00B7 middle dot)
#   • (U+2022 bullet)
#   – — (U+2013 / U+2014 en/em dash)  — used as visual separators
_DOMAIN_SPLIT_RE = re.compile(
    r"[,\s;|/\uff0c\u3001\uff1b\uff5c\u30fb\u00b7\u2022\u2013\u2014]+"
)

# Characters that frequently appear *around* a pasted domain (matched
# punctuation, quote marks of every flavour, brackets, parentheses,
# periods used as sentence terminators, etc). We strip these from each
# end of a token before validating so things like ``"spotify.com",``
# / ``(spotify.com)`` / ``spotify.com.`` all just work.
_TOKEN_TRIM_CHARS = (
    " \t\r\n"          # whitespace
    ".,;:!?"           # sentence punctuation
    "\"'`"             # ASCII quotes
    "\u201c\u201d"     # curly double quotes
    "\u2018\u2019"     # curly single quotes
    "()[]{}<>"         # ASCII brackets
    "\u300c\u300d"     # CJK corner brackets
    "\u3010\u3011"     # CJK black brackets
    "*~_"              # markdown noise
)


def validate_domain(raw: str) -> Tuple[bool, str]:
    """
    Validate and normalise a single domain string.

    Returns:
        (is_valid, cleaned_domain_or_error)
    """
    original = raw.strip()
    domain = original.lower()
    # Strip surrounding punctuation/quotes/brackets that frequently come
    # along with copy-pasted domains.
    domain = domain.strip(_TOKEN_TRIM_CHARS)
    # Drop a scheme if one was pasted.
    domain = domain.removeprefix("http://").removeprefix("https://")
    # Drop user-info, path, query and port.
    domain = domain.split("/", 1)[0]
    domain = domain.split("?", 1)[0]
    domain = domain.split("#", 1)[0]
    domain = domain.rsplit("@", 1)[-1]
    domain = domain.split(":", 1)[0]
    # FQDN form like 'spotify.com.' is valid in DNS; strip the trailing
    # dot so the regex accepts it.
    domain = domain.rstrip(".")
    # And one more trim in case stripping the path/scheme exposed
    # leftover punctuation at either end.
    domain = domain.strip(_TOKEN_TRIM_CHARS)

    if not domain or "." not in domain:
        return False, (
            f"Couldn't parse a domain from '{original}'. "
            "Send something like spotify.com, netflix.com."
        )
    if not _DOMAIN_RE.match(domain):
        return False, (
            f"Invalid domain format: '{original}'. "
            "Use plain names like spotify.com (no special characters)."
        )
    return True, domain


def validate_domains(
    raw: str,
    max_count: int = 10,
) -> Tuple[bool, Union[List[str], str]]:
    """
    Validate and normalise a list of domains supplied as a single string.

    Accepts any reasonable separator: commas, semicolons, pipes,
    slashes, any whitespace (including newlines), bullets, and the
    Unicode-punctuation variants mobile keyboards often produce
    (fullwidth comma ``，``, ideographic comma ``、``, en/em dash, etc).

    Surrounding quotes, brackets and stray punctuation around each
    token are stripped before validation so copy-pastes like
    ``"spotify.com", (netflix.com)`` work too.

    Each token is run through :func:`validate_domain`, duplicates are
    removed (preserving the user's original ordering), and the final
    list is capped at *max_count* entries.

    Returns:
        (True,  list_of_cleaned_domains)  on success
        (False, error_message)            on the first validation failure
    """
    if not raw or not raw.strip():
        return False, "Please send at least one domain (e.g. spotify.com)"

    # Pre-process URL forms so a pasted ``https://spotify.com/login``
    # doesn't end up as the tokens ``["https:", "spotify.com", "login"]``
    # after splitting on ``/``. Replace each URL with just its host
    # surrounded by spaces — the regular split below then picks up
    # those hosts cleanly.
    def _url_to_host(m: "re.Match[str]") -> str:
        after_scheme = m.group(0).split("://", 1)[1]
        host = (
            after_scheme.split("/", 1)[0]
            .split("?", 1)[0]
            .split("#", 1)[0]
        )
        return f" {host} "

    pre_split = re.sub(
        r"https?://\S+", _url_to_host, raw.strip(), flags=re.IGNORECASE,
    )

    tokens = [t.strip() for t in _DOMAIN_SPLIT_RE.split(pre_split)]
    # Drop tokens that, after stripping surrounding punctuation, become
    # empty — that way a stray `/` or `,` between real domains doesn't
    # produce a phantom validation failure.
    tokens = [t for t in tokens if t.strip(_TOKEN_TRIM_CHARS)]
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
