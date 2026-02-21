"""Shared helpers: clipboard, peer-color palette, inline-Markdown renderer, @mention."""
from __future__ import annotations

import re
import subprocess
import sys

from rich.markup import escape as markup_escape


# ---------------------------------------------------------------------------
# Clipboard helper
# ---------------------------------------------------------------------------

def _try_copy_to_clipboard(text: str) -> bool:
    """Try to copy *text* to the system clipboard. Returns True on success."""
    if sys.platform == "win32":
        cmds = [["clip"]]
    elif sys.platform == "darwin":
        cmds = [["pbcopy"]]
    else:
        cmds = [
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
            ["clip.exe"],   # WSL
            ["wl-copy"],    # Wayland
        ]
    for cmd in cmds:
        try:
            result = subprocess.run(cmd, input=text.encode(), capture_output=True, timeout=2)
            if result.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return False


# ---------------------------------------------------------------------------
# Per-peer color palette
# ---------------------------------------------------------------------------

_PEER_COLORS = [
    "cyan", "yellow", "magenta", "bright_cyan",
    "bright_yellow", "bright_magenta", "orange1", "hot_pink",
    "chartreuse3", "cornflower_blue", "salmon1", "sky_blue2",
]


def _peer_color(node_id: str) -> str:
    """Return a deterministic Rich color name for a given node ID."""
    return _PEER_COLORS[hash(node_id) % len(_PEER_COLORS)]


# ---------------------------------------------------------------------------
# Inline Markdown → Rich markup renderer
# ---------------------------------------------------------------------------

def _render_text(text: str) -> str:
    """Escape Rich markup in *text*, then convert common inline Markdown.

    Supported: ``**bold**``  ``*italic*``  ``_italic_``  `` `code` ``  ``~~strike~~``
    """
    # Escape any Rich markup the user typed (prevents injection).
    out = markup_escape(text)

    # Code spans first — their content must not be altered by later rules.
    out = re.sub(
        r"`([^`\n]+)`",
        r"[bold bright_black on grey23] \1 [/bold bright_black on grey23]",
        out,
    )
    # Bold  **text**
    out = re.sub(r"\*\*(.+?)\*\*", r"[bold]\1[/bold]", out)
    # Italic  *text*
    out = re.sub(r"\*([^*\n]+)\*", r"[italic]\1[/italic]", out)
    # Italic  _text_  (word-boundary guard to avoid breaking snake_case)
    out = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"[italic]\1[/italic]", out)
    # Strikethrough  ~~text~~
    out = re.sub(r"~~(.+?)~~", r"[strike]\1[/strike]", out)

    return out


# ---------------------------------------------------------------------------
# @mention support
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"(?<!\w)@([\w\-]+)", re.IGNORECASE)


def _render_text_with_mentions(text: str, my_names: set[str]) -> tuple[str, bool]:
    """Render *text* as Rich markup and detect whether it mentions the local user.

    Returns ``(rendered_str, mentioned)`` where *mentioned* is True when any
    ``@token`` in the message case-insensitively matches one of *my_names*
    (display name or node-id prefix).

    @mentions are highlighted in bold yellow; ones that match *my_names* are
    additionally highlighted in reverse-video so they stand out even more.
    """
    # Escape first, then apply Markdown, then highlight @mentions.
    rendered = _render_text(text)
    mentioned = False

    def _replace_mention(m: re.Match) -> str:
        nonlocal mentioned
        token = m.group(1)
        if any(token.lower() == n.lower() for n in my_names):
            mentioned = True
            return f"[bold reverse yellow]@{markup_escape(token)}[/bold reverse yellow]"
        return f"[bold yellow]@{markup_escape(token)}[/bold yellow]"

    rendered = _MENTION_RE.sub(_replace_mention, rendered)
    return rendered, mentioned


def mentions_me(text: str, my_names: set[str]) -> bool:
    """Return True if *text* contains an @mention matching any of *my_names*."""
    return any(
        token.lower() in {n.lower() for n in my_names}
        for token in _MENTION_RE.findall(text)
    )
