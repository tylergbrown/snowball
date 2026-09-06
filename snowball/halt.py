from __future__ import annotations

import os
from pathlib import Path

from snowball.config import Settings


def halt_active(path: Path) -> bool:
    """Kill switch: any existing HALT path (file or accidental directory) blocks orders."""
    try:
        return path.exists()
    except OSError:
        return True


def write_halt(path: Path) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("halt\n", encoding="utf-8")


def clear_halt(path: Path) -> bool:
    """Remove HALT file. Returns True if it was present."""
    if not path.exists():
        return False
    if path.is_dir():
        raise IsADirectoryError(f"HALT path is a directory: {path}")
    path.unlink()
    return True


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def trading_enabled(settings: Settings) -> bool:
    return env_flag("TRADING_ENABLED", settings.trading_enabled)


def live_enabled_env(settings: Settings) -> bool:
    return env_flag("LIVE_ENABLED", settings.live_enabled)
