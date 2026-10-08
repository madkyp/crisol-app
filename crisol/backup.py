"""Copia de seguridad de los datos de Crisol (para reinstalar o cambiar de PC).

Va en un .tar.gz pequeño: ajustes, estado de cada juego (mods, perfiles, orden, notas, ganadores de
conflictos), la lista exportable de cada juego, lo que Crisol necesita para quitar los mods del juego
(manifiesto y originales guardados) y, si se pide, las copias de partidas. NO lleva los mods (se vuelven a
bajar) ni la API key de Nexus (es secreta).

Al restaurar:
- si en este PC están los mods de un juego, se recupera su estado tal cual;
- si no, se guarda su lista en restored/ y la página del juego ofrece importarla (baja lo que falte);
- las partidas se añaden sin pisar ninguna; antes de nada se hace una copia de lo que hay ahora.
"""
from __future__ import annotations

import io
import json
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import VERSION, jsonio, paths
from .i18n import _

FORMAT = "crisol-backup"
RESTORED_DIR = paths.DATA_DIR / "restored"       # listas de juegos cuyos mods no están en este PC
SAFETY_DIR = paths.DATA_DIR / "restore-backups"  # lo que había antes de restaurar


class BackupError(Exception):
    pass


@dataclass
class Summary:
    games: list[str] = field(default_factory=list)      # estado recuperado tal cual
    lists: list[str] = field(default_factory=list)      # sin los mods: lista lista para importar
    saves: int = 0                                       # copias de partidas añadidas
    safety: str = ""                                     # copia de lo que había antes


def saves_size() -> int:
    from .manager import dir_size
    d = paths.DATA_DIR / "saves"
    return dir_size(d) if d.is_dir() else 0


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mtime = len(data), int(time.time())
    tar.addfile(info, io.BytesIO(data))


def create(dest: Path, include_saves: bool = False) -> dict:
    """Escribe la copia en dest. Devuelve su manifiesto."""
    from . import games as games_mod
    from . import manager, modlist
    by_safe = {g.safe_key: g for g in games_mod.scan_all()}
    states = sorted(paths.GAMES_DIR.glob("*.json")) if paths.GAMES_DIR.is_dir() else []
    manifest = {"format": FORMAT, "version": 1, "app": VERSION, "created": int(time.time()),
                "saves": include_saves, "games": []}
    tmp = dest.with_name(dest.name + ".part")
    with tarfile.open(tmp, "w:gz") as tar:
        if paths.CONFIG_FILE.is_file():
            tar.add(paths.CONFIG_FILE, "config/config.json")
        for f in states:
            safe = f.stem
            raw = jsonio.load(f, {})
            entry = {"safe": safe, "key": raw.get("game", ""), "mods": len(raw.get("mods") or {})}
            tar.add(f, f"games/{safe}.json")
            g = by_safe.get(safe)
            if g and entry["mods"]:
                entry["name"] = g.name
                data = modlist.export(manager.context(g))
                _add_bytes(tar, f"lists/{safe}.json", json.dumps(data, indent=1, ensure_ascii=False).encode())
            dep = paths.DEPLOY_DIR / f"{safe}.json"
            if dep.is_file():
                tar.add(dep, f"deploy/{safe}.json")
            bk = paths.BACKUPS_DIR / safe
            if bk.is_dir():
                tar.add(bk, f"backups/{safe}")
            sv = paths.DATA_DIR / "saves" / safe
            if include_saves and sv.is_dir():
                tar.add(sv, f"saves/{safe}")
            manifest["games"].append(entry)
        _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False).encode())
    tmp.replace(dest)
    return manifest


def read_manifest(src: Path) -> dict:
    try:
        with tarfile.open(src, "r:gz") as tar:
            m = tar.extractfile("manifest.json")
            data = json.loads(m.read()) if m else None
    except (OSError, tarfile.TarError, KeyError, ValueError) as e:
        raise BackupError(_("No se pudo leer la copia: {0}").format(e)) from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise BackupError(_("Ese archivo no es una copia de seguridad de Crisol"))
    return data


def _staging_complete(safe: str, state: dict) -> bool:
    mods = state.get("mods") or {}
    return all((paths.STAGING_DIR / safe / uid).is_dir() for uid in mods)


def restore(src: Path) -> Summary:
    read_manifest(src)
    out = Summary()
    SAFETY_DIR.mkdir(parents=True, exist_ok=True)
    safety = SAFETY_DIR / f"antes-de-restaurar-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    create(safety)
    out.safety = str(safety)
    with tempfile.TemporaryDirectory(prefix="crisol-restore-") as td:
        root = Path(td)
        with tarfile.open(src, "r:gz") as tar:
            tar.extractall(root, filter="data")   # sin rutas absolutas ni «..»
        cfg = root / "config" / "config.json"
        if cfg.is_file():
            paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cfg, paths.CONFIG_FILE)
        for f in sorted((root / "games").glob("*.json")):
            safe = f.stem
            state = jsonio.load(f, {})
            if not state.get("mods"):
                continue
            if _staging_complete(safe, state):
                paths.GAMES_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, paths.GAMES_DIR / f.name)
                # Lo necesario para quitar los mods del juego, solo si aquí no hay nada aplicado.
                dep = root / "deploy" / f"{safe}.json"
                if dep.is_file() and not (paths.DEPLOY_DIR / dep.name).exists():
                    paths.DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dep, paths.DEPLOY_DIR / dep.name)
                    bk = root / "backups" / safe
                    if bk.is_dir() and not (paths.BACKUPS_DIR / safe).exists():
                        shutil.copytree(bk, paths.BACKUPS_DIR / safe)
                out.games.append(state.get("game") or safe)
            elif (root / "lists" / f"{safe}.json").is_file():
                RESTORED_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / "lists" / f"{safe}.json", RESTORED_DIR / f"{safe}.json")
                out.lists.append(state.get("game") or safe)
        for d in sorted((root / "saves").glob("*/*")) if (root / "saves").is_dir() else []:
            target = paths.DATA_DIR / "saves" / d.parent.name / d.name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                (shutil.copytree if d.is_dir() else shutil.copy2)(d, target)
                out.saves += 1
    return out


def pending_list(safe_key: str) -> dict | None:
    """La lista restaurada de un juego (si la copia la trajo y aún no se importó)."""
    f = RESTORED_DIR / f"{safe_key}.json"
    if not f.is_file():
        return None
    try:
        from . import modlist
        return modlist.load(f)
    except Exception:  # noqa: BLE001
        return None


def drop_pending(safe_key: str) -> None:
    (RESTORED_DIR / f"{safe_key}.json").unlink(missing_ok=True)
