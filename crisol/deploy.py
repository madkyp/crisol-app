"""Aplicar los mods al juego y quitarlos, de forma reversible.

- Se calcula el plan: ruta final → (mod, archivo de origen). Si dos mods traen el mismo archivo,
  gana el que va después en el orden.
- Antes de colocar un archivo que ya existe en el juego, el original se mueve a backups/.
- Todo lo colocado queda anotado en deploy/<juego>.json (tamaño, mtime, inodo y carpetas creadas).
- Quitar los mods borra exactamente lo anotado (si sigue siendo nuestro), devuelve los originales y
  borra las carpetas que se crearon y quedaron vacías.

Los archivos se colocan con reflink (copia instantánea en btrfs/xfs, no ocupa espacio y el juego no
puede tocar el staging); si no se puede, enlace duro; y si tampoco, copia normal.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import jsonio, paths
from .layouts import Layout
from .store import GameState
from .i18n import _

log = logging.getLogger(__name__)
FICLONE = 0x40049409

Progress = Callable[[float, str], None]


class DeployError(Exception):
    pass


@dataclass
class Conflict:
    target: str
    winner: str                 # uid
    losers: list[str]           # uids, en orden de carga
    internal: bool = False      # conflicto dentro de paquetes (.pak), no de archivos


@dataclass
class Plan:
    files: dict[str, tuple[str, Path]] = field(default_factory=dict)     # destino → (uid, origen)
    generated: dict[str, bytes] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    placed: int = 0
    backed_up: int = 0
    removed: int = 0
    restored: int = 0
    kept_changed: list[str] = field(default_factory=list)   # archivos que cambió otro (Steam, el juego)
    method: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- utilidades de ruta

def ci_resolve(root: Path, rel: str) -> Path:
    """Ruta dentro de root respetando las mayúsculas de lo que ya existe (Wine no distingue)."""
    cur = root
    for part in rel.split("/"):
        nxt = cur / part
        if not nxt.exists() and cur.is_dir():
            low = part.lower()
            try:
                for e in list(os.scandir(cur)):
                    if e.name.lower() == low:
                        nxt = cur / e.name
                        break
            except OSError:
                pass
        cur = nxt
    return cur


def _safe_rel(rel: str) -> str:
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise DeployError(_('Ruta no permitida en un mod: {0}').format(rel))
    return "/".join(parts)


def _place(src: Path, dst: Path) -> str:
    """Coloca src en dst. Devuelve el método usado."""
    try:
        with open(src, "rb") as fs, open(dst, "wb") as fd:
            fcntl.ioctl(fd.fileno(), FICLONE, fs.fileno())
        shutil.copystat(src, dst)
        return "reflink"
    except OSError as e:
        dst.unlink(missing_ok=True)
        if e.errno not in (errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL, errno.ENOTTY):
            raise
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def _stamp(p: Path) -> dict:
    st = p.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino}


# ---------------------------------------------------------------- plan

def make_plan(state: GameState, layout: Layout, with_internal: bool = False) -> Plan:
    plan = Plan()
    owners: dict[str, list[str]] = {}
    mods = state.enabled_ordered()
    for pos, m in enumerate(mods, start=1):
        stg = state.staging(m.uid)
        for src, dst in m.files:
            target = _safe_rel(layout.target(dst, pos))
            owners.setdefault(target.lower(), []).append(m.uid)
            plan.files[target.lower()] = (m.uid, stg / src)
    # Las claves en minúsculas evitan dos archivos «iguales para Wine»; se recupera la ruta real.
    real: dict[str, str] = {}
    for pos, m in enumerate(mods, start=1):
        for _u, dst in m.files:
            t = _safe_rel(layout.target(dst, pos))
            real[t.lower()] = t
    plan.files = {real[k]: v for k, v in plan.files.items()}
    for k, uids in owners.items():
        if len(uids) > 1:
            plan.conflicts.append(Conflict(real[k], uids[-1], uids[:-1]))
    folders = [f for m in mods for f in m.folders]
    plan.generated = layout.generated(folders, {f for m in state.mods.values() for f in m.folders})
    if with_internal and layout.supports_internal_conflicts:
        plan.conflicts += internal_conflicts(state, layout)
    plan.notes = layout.notes(list(plan.files))
    return plan


def internal_conflicts(state: GameState, layout: Layout) -> list[Conflict]:
    """Archivos repetidos dentro de los paquetes de distintos mods (p. ej. .pak de KCD2)."""
    owners: dict[str, list[str]] = {}
    for m in state.enabled_ordered():
        seen: set[str] = set()
        for src, _u in m.files:
            for entry in layout.internal_entries(state.staging(m.uid) / src):
                if entry not in seen:
                    seen.add(entry)
                    owners.setdefault(entry, []).append(m.uid)
    return [Conflict(e, u[-1], u[:-1], internal=True) for e, u in sorted(owners.items()) if len(u) > 1]


# ---------------------------------------------------------------- aplicar / quitar

class Deployer:
    def __init__(self, game_dir: Path, safe_key: str):
        self.root = game_dir
        self.manifest_path = paths.DEPLOY_DIR / f"{safe_key}.json"
        self.backup_root = paths.BACKUPS_DIR / safe_key

    def manifest(self) -> dict:
        return jsonio.load(self.manifest_path, {"files": {}, "dirs": []})

    def is_deployed(self) -> bool:
        return bool(self.manifest().get("files"))

    def deploy(self, state: GameState, layout: Layout, progress: Progress | None = None) -> Report:
        """Quita lo anterior y aplica el perfil activo."""
        report = self.purge(progress)
        plan = make_plan(state, layout)
        for uid, src in plan.files.values():
            if not src.is_file():
                raise DeployError(_('Falta un archivo del mod «{0}»: {1}. Reinstálalo.').format(state.mods[uid].name, src.name))
        man = {"game_dir": str(self.root), "profile": state.active, "time": time.time(), "files": {}, "dirs": []}
        methods: set[str] = set()
        items: list[tuple[str, str, Path | bytes]] = [(t, u, s) for t, (u, s) in plan.files.items()]
        items += [(t, "", data) for t, data in plan.generated.items()]
        try:
            for i, (target, uid, src) in enumerate(items):
                if progress and i % 25 == 0:
                    progress(i / max(1, len(items)), target)
                dst = ci_resolve(self.root, target)
                rel = dst.relative_to(self.root).as_posix()
                self._mkdirs(dst.parent, man)
                backup = False
                if dst.exists() or dst.is_symlink():
                    if dst.is_dir():
                        raise DeployError(_('Un mod quiere poner un archivo donde el juego tiene una carpeta: {0}').format(rel))
                    bk = self.backup_root / rel
                    if bk.exists():
                        raise DeployError(_('Ya hay una copia de seguridad de {0}; no se sobrescribe. Revisa backups/ antes de seguir.').format(rel))
                    bk.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(dst, bk)
                    backup = True
                    report.backed_up += 1
                if isinstance(src, bytes):
                    dst.write_bytes(src)
                    method = "generated"
                else:
                    method = _place(src, dst)
                    methods.add(method)
                man["files"][rel] = {"uid": uid, "backup": backup, **_stamp(dst)}
                report.placed += 1
        finally:
            jsonio.save(self.manifest_path, man)
        report.method = ", ".join(sorted(methods))
        report.notes = plan.notes
        state.dirty_deploy = False
        state.save()
        if progress:
            progress(1.0, "")
        log.info("desplegados %d archivos en %s (%s)", report.placed, self.root, report.method)
        return report

    def _mkdirs(self, d: Path, man: dict) -> None:
        missing = []
        while not d.exists():
            missing.append(d)
            d = d.parent
        for m in reversed(missing):
            m.mkdir()
            man["dirs"].append(m.relative_to(self.root).as_posix())

    def purge(self, progress: Progress | None = None) -> Report:
        """Deja el juego como estaba antes de Crisol."""
        report = Report()
        man = self.manifest()
        files: dict[str, dict] = man.get("files") or {}
        for i, (rel, info) in enumerate(sorted(files.items(), reverse=True)):
            if progress and i % 25 == 0:
                progress(i / max(1, len(files)), rel)
            dst = self.root / rel
            bk = self.backup_root / rel
            ours = False
            if dst.is_file() and not dst.is_symlink():
                st = dst.stat()
                ours = (st.st_size, st.st_mtime_ns, st.st_ino) == (info["size"], info["mtime_ns"], info["ino"])
            if ours:
                dst.unlink()
                report.removed += 1
            elif dst.exists():
                # Lo ha cambiado otro (p. ej. «Verificar archivos» de Steam): se respeta y el
                # respaldo, ya inútil, se aparta en vez de borrarse.
                report.kept_changed.append(rel)
                if bk.exists():
                    old = bk.with_name(bk.name + f".crisol-old-{int(time.time())}")
                    bk.rename(old)
                continue
            if info.get("backup") and bk.exists():
                bk.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(bk, dst)
                report.restored += 1
        for rel in sorted(man.get("dirs") or [], key=lambda r: r.count("/"), reverse=True):
            try:
                (self.root / rel).rmdir()
            except OSError:
                pass  # no está vacía (el juego ha guardado algo) o ya no existe
        # Limpia carpetas vacías de backups/ (incluida la del juego).
        if self.backup_root.exists():
            dirs = [d for d in self.backup_root.rglob("*") if d.is_dir()] + [self.backup_root]
            for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
        self.manifest_path.unlink(missing_ok=True)
        if report.kept_changed:
            log.warning("archivos cambiados por otro programa, se dejan: %s", report.kept_changed)
        return report


# ---------------------------------------------------------------- verificación

def fingerprint(root: Path, ignore: Callable[[str], bool] | None = None) -> dict[str, tuple]:
    """Huella de una carpeta: ruta → (tamaño, mtime, inodo) y carpetas → ("dir",)."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        for d in dirnames:
            r = d if rel_dir == "." else f"{rel_dir}/{d}"
            if not (ignore and ignore(r)):
                out[r] = ("dir",)
        for f in filenames:
            r = f if rel_dir == "." else f"{rel_dir}/{f}"
            if ignore and ignore(r):
                continue
            st = os.lstat(Path(dirpath, f))
            out[r] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return out


def diff(before: dict[str, tuple], after: dict[str, tuple]) -> dict[str, list[str]]:
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(k for k in set(before) & set(after) if before[k] != after[k]),
    }
