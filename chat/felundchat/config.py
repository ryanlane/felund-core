from __future__ import annotations

from pathlib import Path

APP_DIR = Path.home() / ".felundchat"
STATE_FILE = APP_DIR / "state.json"

MSG_MAX = 16_384          # bytes per frame
READ_TIMEOUT_S = 30
MESSAGE_MAX_AGE_S = 30 * 24 * 60 * 60
MAX_MESSAGES_PER_CIRCLE = 1_000
