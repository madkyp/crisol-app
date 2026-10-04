"""¿Está abierto un juego? Aplicar o quitar mods con el juego en marcha puede dejarlo a medias.

Señales (sin depender de nombres de proceso sueltos):
- Steam: mientras el juego está abierto existe `reaper SteamLaunch AppId=<id>`.
- Umbral: $XDG_RUNTIME_DIR/umbral/running.json (PIDs exactos de sus juegos).
- Cualquier proceso de Wine/Proton (.exe) cuya carpeta de trabajo o línea de órdenes esté en el juego.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .games import Game


def _cmdline(pid: str) -> list[str]:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


def _wine_path(p: Path) -> str:
    return ("z:" + str(p).replace("/", "\\")).lower()


def _umbral_running(game: Game) -> list[str]:
    run = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "umbral" / "running.json"
    try:
        data = json.loads(run.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for g in data.get("games", []):
        if g.get("id") == game.source_id and Path(f"/proc/{g.get('pid')}").exists():
            out.append(g.get("exe_name") or g.get("name") or "juego")
    return out


def running(game: Game) -> list[str]:
    """Nombres de los procesos del juego que están en marcha (vacío si está cerrado)."""
    if game.source == "umbral":
        found = _umbral_running(game)
        if found:
            return found
    me = str(os.getpid())
    root = str(game.install_dir)
    wine_root = _wine_path(game.install_dir)
    steam_tag = f"AppId={game.source_id}" if game.source == "steam" else None
    found: list[str] = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        args = _cmdline(pid)
        if not args or not args[0]:
            continue
        if steam_tag and "SteamLaunch" in args and steam_tag in args:
            found.append(f"Steam ({game.name})")
            continue
        joined = " ".join(args).lower()
        if ".exe" not in joined:
            continue  # solo procesos de Wine/Proton: un terminal abierto en la carpeta no cuenta
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        if cwd.startswith(root) or root.lower() in joined or wine_root in joined:
            exe = next((a for a in args if a.lower().endswith(".exe")), args[0])
            found.append(exe.replace("\\", "/").rsplit("/", 1)[-1])
    return sorted(set(found))


class GameRunning(Exception):
    def __init__(self, game: Game, procs: list[str]):
        super().__init__(f"«{game.name}» está abierto ({', '.join(procs[:3])}). Ciérralo antes de cambiar sus mods.")
        self.procs = procs
