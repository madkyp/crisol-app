"""Rutas XDG de la aplicación."""
import os
from pathlib import Path

HOME = Path.home()


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or HOME / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "forja"
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache") / "forja"
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state") / "forja"
DATA_HOME = _xdg("XDG_DATA_HOME", ".local/share")
DATA_DIR = DATA_HOME / "forja"

CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_DIR = STATE_DIR / "logs"
DOWNLOADS_DIR = DATA_DIR / "downloads"   # archivos tal cual se bajaron
STAGING_DIR = DATA_DIR / "staging"       # mods extraídos: staging/<juego>/<mod>/
GAMES_DIR = DATA_DIR / "games"           # estado por juego: mods, perfiles, orden
BACKUPS_DIR = DATA_DIR / "backups"       # originales del juego que un mod pisa
DEPLOY_DIR = DATA_DIR / "deploy"         # manifiesto de lo colocado en cada juego
THUMBS_DIR = CACHE_DIR / "thumbs"

STEAM_ROOTS = [
    DATA_HOME / "Steam",
    HOME / ".steam" / "steam",
    HOME / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
]
UMBRAL_CONFIG = _xdg("XDG_CONFIG_HOME", ".config") / "umbral" / "config.json"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, LOG_DIR, DOWNLOADS_DIR, STAGING_DIR, GAMES_DIR, BACKUPS_DIR, DEPLOY_DIR, THUMBS_DIR):
        d.mkdir(parents=True, exist_ok=True)
