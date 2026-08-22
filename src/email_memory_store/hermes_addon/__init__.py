"""Optional Hermes Telegram topic integration for email memory."""

from .installer import (
    HermesAddonError,
    HermesAddonResult,
    disable_hermes_addon,
    install_hermes_addon,
)

__all__ = [
    "HermesAddonError",
    "HermesAddonResult",
    "disable_hermes_addon",
    "install_hermes_addon",
]
