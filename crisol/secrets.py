"""API key de Nexus Mods: en el llavero del sistema (Secret Service) si lo hay; si no, en un
archivo legible solo por el usuario (~/.config/crisol/nexus.key, permisos 600), avisando en la interfaz."""
from __future__ import annotations

import logging
import os

from . import paths

log = logging.getLogger(__name__)
_ATTRS = {"service": "nexusmods"}
_LIB: tuple | None = None


def _lib():
    """(GLib, Secret, esquema) cargados al usarlos, o None si gi no carga (p. ej. con las librerías del
    entorno de Steam en LD_LIBRARY_PATH): entonces solo se usa el archivo."""
    global _LIB
    if _LIB is None:
        try:
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import GLib, Secret
            schema = Secret.Schema.new("dev.madky.Crisol", Secret.SchemaFlags.NONE,
                                       {"service": Secret.SchemaAttributeType.STRING})
            _LIB = (GLib, Secret, schema)
        except (ImportError, ValueError) as e:
            log.info("libsecret no disponible: %s", e)
            _LIB = ()
    return _LIB or None
KEY_FILE = paths.CONFIG_DIR / "nexus.key"


def _file_get() -> str | None:
    try:
        return KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def _file_set(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    os.chmod(KEY_FILE, 0o600)


def get_nexus_key() -> str | None:
    lib = _lib()
    if lib:
        GLib, Secret, schema = lib
        try:
            key = Secret.password_lookup_sync(schema, _ATTRS, None)
            if key:
                return key
        except GLib.Error as e:
            log.info("llavero no disponible: %s", e.message)
    return _file_get()


def set_nexus_key(key: str) -> str:
    """Guarda la clave. Devuelve dónde: "keyring" o "file"."""
    key = key.strip()
    lib = _lib()
    if lib:
        GLib, Secret, schema = lib
        try:
            Secret.password_store_sync(schema, _ATTRS, Secret.COLLECTION_DEFAULT, "Crisol: API key de Nexus Mods",
                                       key, None)
            KEY_FILE.unlink(missing_ok=True)  # si antes estaba en archivo, se quita
            return "keyring"
        except GLib.Error as e:
            log.warning("no hay llavero del sistema (%s); la clave se guarda en %s (600)", e.message, KEY_FILE)
    _file_set(key)
    return "file"


def clear_nexus_key() -> None:
    lib = _lib()
    if lib:
        GLib, Secret, schema = lib
        try:
            Secret.password_clear_sync(schema, _ATTRS, None)
        except GLib.Error:
            pass
    KEY_FILE.unlink(missing_ok=True)


def storage() -> str:
    """Dónde está guardada ahora la clave: "keyring", "file" o ""."""
    lib = _lib()
    if lib:
        GLib, Secret, schema = lib
        try:
            if Secret.password_lookup_sync(schema, _ATTRS, None):
                return "keyring"
        except GLib.Error:
            pass
    return "file" if _file_get() else ""
