"""Copias de seguridad de las partidas antes de aplicar mods (y restaurarlas).

Cada juego guarda en un sitio distinto. Se buscan, dentro del prefijo de Proton (usuario de Wine) y
de la carpeta del juego:
- carpetas con steam_autocloud.vdf (Steam lo pone en las carpetas de partidas que sincroniza);
- carpetas con archivos de partida (.sav, .sl2, .save, .cnv…);
- carpetas «Saved»/«saves»/«SaveGames» con archivos sueltos.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import jsonio, paths
from .games import Game

SAVE_EXTS = (".sav", ".sl2", ".save", ".cnv", ".ess", ".sv", ".savegame")
SAVE_DIRS = {"saved", "saves", "savegames", "save", "savedata", "savegame"}
# Carpetas que nunca contienen partidas y pueden ser enormes.
_PRUNE = {"temp", "microsoft", "d3dscache", "nvidia corporation", "nvidia", "inetcache", "packages",
          "shadercache", "shader_cache", "crashes", "crashreportclient", "logs", "webcache", "cef", "gpucache"}
_SKIP_FILES = (".log", ".tmp", ".dmp", ".upipelinecache", ".ushaderprecache")
MAX_TOTAL = 2 * 1024 ** 3   # no copiar más de 2 GB por juego
KEEP = 5                    # copias que se guardan por juego


@dataclass
class SaveDir:
    root: str      # "prefix" | "game"
    rel: str       # relativo a la raíz
    size: int


def _roots(game: Game) -> dict[str, Path]:
    roots = {"game": game.install_dir}
    if game.prefix:
        users = game.prefix / "drive_c" / "users"
        user = users / "steamuser" if (users / "steamuser").is_dir() else next(
            (u for u in sorted(users.iterdir()) if u.is_dir() and u.name not in ("Public",)), None) \
            if users.is_dir() else None
        if user:
            roots["prefix"] = user
    return roots


def find(game: Game) -> list[SaveDir]:
    found: list[SaveDir] = []
    for tag, root in _roots(game).items():
        if not root.is_dir():
            continue
        cands: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in _PRUNE and not d.lower().endswith(" backup")]
            here = Path(dirpath)
            depth = len(here.relative_to(root).parts)
            if depth > 8:
                dirnames[:] = []
                continue
            names = [f.lower() for f in filenames]
            is_save = ("steam_autocloud.vdf" in names
                       or any(n.endswith(SAVE_EXTS) for n in names)
                       or (here.name.lower() in SAVE_DIRS and any(not n.endswith(_SKIP_FILES + (".ini",))
                                                                  for n in names)))
            if is_save and tag == "game" and depth == 0:
                continue  # la raíz del juego no es una carpeta de partidas
            if is_save:
                cands.append(here)
                dirnames[:] = []  # lo de dentro ya va incluido
        for c in cands:
            size = sum(f.stat().st_size for f in c.rglob("*") if f.is_file())
            found.append(SaveDir(tag, c.relative_to(root).as_posix(), size))
    return found


def _dir(game: Game) -> Path:
    return paths.DATA_DIR / "saves" / game.safe_key


def backups(game: Game) -> list[dict]:
    """Copias existentes, la más reciente primero: [{id, time, reason, size, dirs}]."""
    out = []
    base = _dir(game)
    for d in sorted(base.iterdir(), reverse=True) if base.is_dir() else []:
        meta = jsonio.load(d / "backup.json", None)
        if meta:
            out.append({"id": d.name, **meta})
    return out


def backup(game: Game, reason: str, protect: str = "") -> dict | None:
    """Copia las partidas encontradas. None si no hay ninguna. Borra las copias más antiguas."""
    dirs = find(game)
    total = sum(d.size for d in dirs)
    if not dirs or total > MAX_TOTAL:
        return None
    roots = _roots(game)
    # Nombre único: dos copias en el mismo segundo no deben compartir carpeta (al restaurar se hace
    # una copia previa, y no puede pisar la que se está restaurando).
    base = time.strftime("%Y%m%d-%H%M%S")
    stamp, n = base, 1
    while (_dir(game) / stamp).exists():
        n += 1
        stamp = f"{base}-{n}"
    dest = _dir(game) / stamp
    for d in dirs:
        shutil.copytree(roots[d.root] / d.rel, dest / d.root / d.rel)
    meta = {"time": time.time(), "reason": reason, "size": total,
            "dirs": [{"root": d.root, "rel": d.rel, "size": d.size} for d in dirs]}
    jsonio.save(dest / "backup.json", meta)
    for old in backups(game)[KEEP:]:
        if old["id"] != protect:  # la copia que se está restaurando no se borra
            shutil.rmtree(_dir(game) / old["id"], ignore_errors=True)
    return {"id": stamp, **meta}


def recent(game: Game, seconds: float) -> bool:
    b = backups(game)
    return bool(b) and time.time() - b[0]["time"] < seconds


def restore(game: Game, backup_id: str) -> int:
    """Devuelve las partidas de una copia a su sitio (antes, copia las actuales). Devuelve los archivos."""
    src = _dir(game) / backup_id
    meta = jsonio.load(src / "backup.json", None)
    if not meta:
        raise FileNotFoundError("Esa copia ya no existe")
    backup(game, "antes de restaurar otra copia", protect=backup_id)
    roots = _roots(game)
    count = 0
    for d in meta["dirs"]:
        if d["root"] not in roots:
            continue
        target = roots[d["root"]] / d["rel"]
        from_dir = src / d["root"] / d["rel"]
        for f in from_dir.rglob("*"):
            if f.is_file():
                to = target / f.relative_to(from_dir)
                to.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, to)
                count += 1
    return count
