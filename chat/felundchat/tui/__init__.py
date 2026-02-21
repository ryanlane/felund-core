"""felundchat TUI package.

Public surface: ``FelundApp`` — unchanged from the old single-file module,
so all existing imports (``from felundchat.tui import FelundApp``) keep working.
"""
from .app import FelundApp

__all__ = ["FelundApp"]
