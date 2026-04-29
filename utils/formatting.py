"""
Human-readable formatting helpers.
"""

from __future__ import annotations

from datetime import datetime, timezone


def bytes_human(n: int) -> str:
    """Convert byte count to human string: 1234567 -> '1.18 MB'."""
    if n < 0:
        return "Unlimited"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.2f} PB"


def seconds_human(s: float) -> str:
    """Convert seconds to 'Xh Ym Zs' style."""
    if s < 0:
        return "0s"
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{sec}s")
    return " ".join(parts)


def time_until(iso: str | None) -> str:
    """Human-readable duration from now until an ISO timestamp."""
    if iso is None:
        return "Never"
    try:
        target = datetime.fromisoformat(iso)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = target - datetime.now(timezone.utc)
        total = int(delta.total_seconds())
        if total <= 0:
            return "Expired"
        return seconds_human(total)
    except (ValueError, TypeError):
        return "Unknown"


def progress_bar(current: int, total: int, length: int = 10) -> str:
    """Simple text progress bar."""
    if total <= 0:
        return "\u2591" * length
    filled = int(length * current / total)
    filled = min(filled, length)
    return "\u2588" * filled + "\u2591" * (length - filled)


def number_human(n: int) -> str:
    """1234567 -> '1.2M'."""
    if abs(n) < 1_000:
        return str(n)
    if abs(n) < 1_000_000:
        return f"{n / 1_000:.1f}K"
    if abs(n) < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1_000_000_000:.1f}B"
