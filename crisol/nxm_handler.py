"""Registro de Crisol como app de los enlaces nxm:// (los del botón «Mod Manager Download»)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_ID, paths

DESKTOP = f"{APP_ID}.desktop"
MIME = "x-scheme-handler/nxm"


def current() -> str:
    try:
        return subprocess.run(["xdg-mime", "query", "default", MIME], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def is_default() -> bool:
    return current() == DESKTOP


def _installed_desktop() -> bool:
    return any((d / "applications" / DESKTOP).is_file()
               for d in (Path("/usr/share"), Path("/usr/local/share"), paths.DATA_HOME))


def register() -> None:
    """Hace a Crisol la app por defecto para nxm://. Si no está instalada como paquete, crea un
    .desktop de usuario que lanza esta copia."""
    if not _installed_desktop():
        here = Path(__file__).resolve().parent.parent
        exe = shutil.which("crisol") or f"env PYTHONPATH={here} {sys.executable} -m crisol"
        d = paths.DATA_HOME / "applications" / DESKTOP
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text(f"""[Desktop Entry]
Type=Application
Name=Crisol
Comment=Gestor de mods para Steam y Umbral
Exec={exe} %u
Icon={APP_ID}
Terminal=false
Categories=Game;
MimeType={MIME};
StartupWMClass={APP_ID}
""")
        subprocess.run(["update-desktop-database", str(d.parent)], capture_output=True)
    subprocess.run(["xdg-mime", "default", DESKTOP, MIME], check=True, capture_output=True)
