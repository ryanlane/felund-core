from __future__ import annotations

import os
from pathlib import Path

_state_dir_env = os.getenv("FELUND_STATE_DIR", "")
APP_DIR = Path(_state_dir_env).expanduser() if _state_dir_env else Path.home() / ".felundchat"
STATE_FILE = APP_DIR / "state.json"

MSG_MAX = 16_384          # bytes per frame
READ_TIMEOUT_S = 30
MESSAGE_MAX_AGE_S = 30 * 24 * 60 * 60
MAX_MESSAGES_PER_CIRCLE = 1_000
