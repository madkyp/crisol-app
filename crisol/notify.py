"""Notificaciones del escritorio.

Con la ventana abierta se usan las de GTK (pulsarlas abre la página del juego) y, si la ventana tiene el foco,
no se manda nada: ya sale el aviso dentro. Desde la terminal o el temporizador, con notify-send.
Con `once` un aviso sale una sola vez por valor (p. ej. por versión del juego o por lista de mods con
actualización), aunque se vuelva a comprobar.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys

from . import APP_ID, APP_NAME, jsonio, paths

log = logging.getLogger(__name__)
SEEN = paths.CACHE_DIR / "notified.json"


def enabled() -> bool:
    from .config import Config
    return Config.load().notifications


def _gui_app():
    if "gi.repository.Gio" not in sys.modules:   # las órdenes de terminal no cargan GTK
        return None
    from gi.repository import Gio
    return Gio.Application.get_default()


def already(once: tuple[str, str] | None) -> bool:
    """¿Ya se avisó de esto? (y si no, se apunta)."""
    if not once:
        return False
    data = jsonio.load(SEEN, {})
    key, value = once
    if data.get(key) == value:
        return True
    data[key] = value
    jsonio.save(SEEN, data)
    return False


def send(title: str, body: str = "", key: str = "", game_key: str = "",
         once: tuple[str, str] | None = None) -> bool:
    if not enabled() or already(once):
        return False
    app = _gui_app()
    if app is not None:
        win = getattr(app, "win", None)
        if win is not None and win.is_active():
            return False
        from gi.repository import Gio, GLib
        n = Gio.Notification.new(title)
        if body:
            n.set_body(body)
        n.set_icon(Gio.ThemedIcon.new(APP_ID))
        if game_key:
            n.set_default_action_and_target("app.open-game", GLib.Variant("s", game_key))
        else:
            n.set_default_action("app.show")
        app.send_notification(key or None, n)
        return True
    if not shutil.which("notify-send"):
        return False
    try:
        subprocess.run(["notify-send", "-a", APP_NAME, "-i", APP_ID, "-h", f"string:desktop-entry:{APP_ID}",
                        title, body], timeout=10, capture_output=True)
        return True
    except (OSError, subprocess.TimeoutExpired) as e:
        log.info("no se pudo notificar: %s", e)
        return False


# ---------------------------------------------------------------- temporizador de systemd (usuario)

UNIT = "crisol-updates"


def _unit_dir():
    from pathlib import Path
    import os
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def set_background_checks(on: bool) -> None:
    """Buscar actualizaciones de mods cada 12 h aunque Crisol esté cerrado (avisa con una notificación)."""
    d = _unit_dir()
    service, timer = d / f"{UNIT}.service", d / f"{UNIT}.timer"
    if on:
        exe = shutil.which("crisol") or "/usr/bin/crisol"
        d.mkdir(parents=True, exist_ok=True)
        service.write_text(f"[Unit]\nDescription={APP_NAME}: buscar actualizaciones de mods\n"
                           "After=network-online.target\n\n[Service]\nType=oneshot\n"
                           f"ExecStart={exe} --check-updates --notify\n")
        timer.write_text(f"[Unit]\nDescription={APP_NAME}: buscar actualizaciones de mods cada 12 h\n\n"
                         "[Timer]\nOnBootSec=10min\nOnUnitActiveSec=12h\nPersistent=true\n\n"
                         "[Install]\nWantedBy=timers.target\n")
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", f"{UNIT}.timer")
    else:
        _systemctl("disable", "--now", f"{UNIT}.timer")
        service.unlink(missing_ok=True)
        timer.unlink(missing_ok=True)
        _systemctl("daemon-reload")


def background_checks_on() -> bool:
    return (_unit_dir() / f"{UNIT}.timer").exists()


def _systemctl(*args: str) -> None:
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=20)
