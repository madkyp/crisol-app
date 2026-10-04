"""API key de Nexus Mods en el keyring del sistema (libsecret)."""
from __future__ import annotations

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret  # noqa: E402

_SCHEMA = Secret.Schema.new("dev.madky.Crisol", Secret.SchemaFlags.NONE,
                            {"service": Secret.SchemaAttributeType.STRING})
_ATTRS = {"service": "nexusmods"}


def get_nexus_key() -> str | None:
    try:
        return Secret.password_lookup_sync(_SCHEMA, _ATTRS, None) or None
    except Exception:  # keyring bloqueado o sin servicio
        return None


def set_nexus_key(key: str) -> None:
    Secret.password_store_sync(_SCHEMA, _ATTRS, Secret.COLLECTION_DEFAULT, "Crisol: API key de Nexus Mods",
                               key.strip(), None)


def clear_nexus_key() -> None:
    Secret.password_clear_sync(_SCHEMA, _ATTRS, None)
