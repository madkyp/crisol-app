"""Operaciones de alto nivel por juego: instalar, actualizar, aplicar y comprobar actualizaciones.

Todo lo de aquí es bloqueante (red, disco): la interfaz lo llama desde hilos.
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from . import archive, paths, secrets
from .deploy import Deployer, Plan, Report, make_plan
from .games import Game
from .layouts import LAYOUTS, Layout, LayoutError, detect
from .providers.base import ProviderError
from .providers.nexus import Nexus, NxmLink
from .store import GameState, ModRecord

log = logging.getLogger(__name__)
Progress = Callable[[float, str], None]


class DownloadError(Exception):
    pass


@dataclass
class GameContext:
    game: Game
    state: GameState
    deployer: Deployer

    @property
    def layout(self) -> Layout:
        cls = LAYOUTS.get(self.state.layout or "") or detect(self.game.install_dir, self.state.nexus_domain)
        return cls(self.game.install_dir)

    def plan(self, with_internal: bool = False) -> Plan:
        return make_plan(self.state, self.layout, with_internal)


_contexts: dict[str, GameContext] = {}
_ctx_lock = threading.Lock()
# Un solo despliegue/instalación a la vez por juego.
_game_locks: dict[str, threading.Lock] = {}


def context(game: Game) -> GameContext:
    with _ctx_lock:
        ctx = _contexts.get(game.key)
        if ctx is None or ctx.game.install_dir != game.install_dir:
            ctx = GameContext(game, GameState(game.key, game.safe_key), Deployer(game.install_dir, game.safe_key))
            _contexts[game.key] = ctx
        return ctx


def game_lock(game: Game) -> threading.Lock:
    with _ctx_lock:
        return _game_locks.setdefault(game.key, threading.Lock())


# ---------------------------------------------------------------- descargas

def download(url: str, dest: Path, expected_size: int = 0, progress: Progress | None = None,
             cancel: threading.Event | None = None) -> Path:
    tmp = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=30, headers={"User-Agent": "Crisol"}) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or expected_size or 0)
            done = 0
            with tmp.open("wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if cancel and cancel.is_set():
                        raise DownloadError("Descarga cancelada")
                    f.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(done / total, f"{done / 1e6:.1f} / {total / 1e6:.1f} MB")
    except requests.RequestException as e:
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"La descarga ha fallado: {e}") from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if expected_size and tmp.stat().st_size != expected_size:
        got = tmp.stat().st_size
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"Descarga incompleta o dañada: {got} bytes, se esperaban {expected_size}")
    tmp.replace(dest)
    return dest


def _safe_name(name: str) -> str:
    return re.sub(r"[/\\\0]", "_", name).strip() or "mod"


def download_nexus(nexus: Nexus, link: NxmLink, progress: Progress | None = None,
                   cancel: threading.Event | None = None) -> tuple[Path, str, dict]:
    """Descarga un archivo de Nexus desde un enlace nxm (o directo si la cuenta es Premium).

    Devuelve (ruta, verificación, datos del archivo).
    """
    files = {f.file_id: f for f in nexus.files(link.domain, link.mod_id)}
    finfo = files.get(link.file_id)
    urls = nexus.download_urls(link.domain, link.mod_id, link.file_id, link.key, link.expires)
    if not urls:
        raise DownloadError("Nexus Mods no ha dado ningún enlace de descarga")
    fname = _safe_name(finfo.file_name if finfo and finfo.file_name else Path(urls[0].split("?")[0]).name)
    dest = paths.DOWNLOADS_DIR / link.domain / fname
    expected = finfo.size if finfo else 0
    last: Exception | None = None
    for url in urls[:3]:  # varios servidores: si uno falla, el siguiente
        try:
            download(url, dest, expected, progress, cancel)
            break
        except DownloadError as e:
            if cancel and cancel.is_set():
                raise
            last = e
    else:
        raise last or DownloadError("La descarga ha fallado")
    # Verificación: el md5 del archivo debe corresponder a este mismo archivo en Nexus.
    verified = "size" if expected else ""
    md5 = archive.md5sum(dest)
    try:
        hits = nexus.md5_search(link.domain, md5)
        ok = any(int((h.get("file_details") or {}).get("file_id") or 0) == link.file_id for h in hits)
        if ok:
            verified = "md5"
        elif hits:
            dest.unlink(missing_ok=True)
            raise DownloadError("El archivo descargado no coincide con el de Nexus Mods (md5). Se ha borrado.")
    except ProviderError as e:
        log.warning("no se pudo verificar el md5: %s", e)
    meta = {"md5": md5, "size": dest.stat().st_size, "file_name": fname,
            "version": finfo.version if finfo else "", "file_title": finfo.name if finfo else ""}
    return dest, verified, meta


# ---------------------------------------------------------------- instalación

def install_archive(ctx: GameContext, archive_path: Path, rec: ModRecord, layout_id: str | None = None,
                    progress: Progress | None = None) -> ModRecord:
    """Extrae el archivo a staging y calcula dónde va cada archivo. Si rec ya existe, lo actualiza
    conservando su posición y estado en todos los perfiles."""
    st = ctx.state
    final = st.staging(rec.uid)
    tmp = final.with_name(final.name + ".new")
    if progress:
        progress(0.0, "Extrayendo…")
    archive.extract(archive_path, tmp)
    layout = LAYOUTS[layout_id](ctx.game.install_dir) if layout_id else ctx.layout
    try:
        mapping = layout.map_files(tmp, rec.name)
    except LayoutError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)
    rec.files = [list(m) for m in mapping.files]
    rec.skipped = mapping.skipped
    rec.folders = mapping.folders
    rec.layout = layout.id
    rec.archive = str(archive_path)
    st.add(rec)
    st.save()
    if progress:
        progress(1.0, "Instalado")
    return rec


def install_manual(ctx: GameContext, archive_path: Path, layout_id: str | None = None) -> ModRecord:
    """Importa un archivo descargado a mano (p. ej. desde el navegador)."""
    name = re.sub(r"[-_ ]?\d+(-\d+)*\.(zip|7z|rar|pak)$", "", archive_path.name, flags=re.I) or archive_path.stem
    dest = paths.DOWNLOADS_DIR / "manual" / archive_path.name
    if archive_path.resolve() != dest.resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive_path, dest)
    rec = ModRecord(uid=ctx.state.new_uid(), name=name.replace("_", " "), md5=archive.md5sum(dest),
                    size=dest.stat().st_size, file_name=dest.name)
    # Si el md5 corresponde a un archivo de Nexus de este juego, se enlaza para poder actualizarlo.
    if ctx.state.nexus_domain:
        try:
            hits = Nexus(secrets.get_nexus_key()).md5_search(ctx.state.nexus_domain, rec.md5)
            if hits:
                h = hits[0]
                md, fd = h.get("mod") or {}, h.get("file_details") or {}
                rec.provider, rec.game_domain = "nexus", ctx.state.nexus_domain
                rec.mod_id, rec.file_id = int(md.get("mod_id") or 0), int(fd.get("file_id") or 0)
                rec.name = md.get("name") or rec.name
                rec.version = fd.get("version") or md.get("version") or ""
                rec.author = md.get("author") or ""
                rec.thumbnail = md.get("picture_url") or ""
                rec.verified = "md5"
        except ProviderError as e:
            log.info("md5 no encontrado en Nexus: %s", e)
    return install_archive(ctx, dest, rec, layout_id)


def install_from_nxm(ctx: GameContext, nexus: Nexus, link: NxmLink, progress: Progress | None = None,
                     cancel: threading.Event | None = None) -> ModRecord:
    if ctx.state.nexus_domain and link.domain != ctx.state.nexus_domain:
        raise DownloadError(f"El enlace es de otro juego ({link.domain})")
    info = nexus.mod(link.domain, link.mod_id)

    def dl_progress(f: float, msg: str) -> None:
        if progress:
            progress(f * 0.9, msg)

    path, verified, meta = download_nexus(nexus, link, dl_progress, cancel)
    # ¿Actualización de un mod ya instalado? Mismo mod y mismo título de archivo → se conserva el uid.
    existing = ctx.state.find("nexus", link.mod_id, link.file_id)
    if existing is None:
        same_mod = [m for m in ctx.state.mods.values() if m.provider == "nexus" and m.mod_id == link.mod_id]
        if len(same_mod) == 1:
            existing = same_mod[0]
    rec = existing or ModRecord(uid=ctx.state.new_uid(), name=info.name)
    old_archive = Path(rec.archive) if rec.archive else None
    rec.provider, rec.game_domain = "nexus", link.domain
    rec.mod_id, rec.file_id = link.mod_id, link.file_id
    rec.version = meta["version"] or info.version
    rec.latest_version = info.version
    rec.author, rec.thumbnail = info.author, info.thumbnail
    rec.md5, rec.size, rec.file_name = meta["md5"], meta["size"], meta["file_name"]
    rec.requirements = info.requirements
    rec.verified = verified
    rec = install_archive(ctx, path, rec, progress=lambda f, m: progress and progress(0.9 + f * 0.1, m))
    if old_archive and old_archive != path:
        old_archive.unlink(missing_ok=True)
    return rec


def missing_requirements(ctx: GameContext) -> dict[str, list[dict]]:
    """Requisitos de Nexus de los mods activos que no están instalados ni activos (por uid)."""
    active_ids = {m.mod_id for m in ctx.state.enabled_ordered() if m.provider == "nexus"}
    out: dict[str, list[dict]] = {}
    for m in ctx.state.enabled_ordered():
        miss = [r for r in m.requirements if not r.get("external") and r.get("mod_id") and r["mod_id"] not in active_ids]
        miss += [r for r in m.requirements if r.get("external")]  # externos: no se pueden comprobar
        if miss:
            out[m.uid] = miss
    return out


def check_updates(ctx: GameContext, nexus: Nexus) -> int:
    """Consulta las versiones actuales en Nexus. Devuelve cuántos mods tienen actualización."""
    by_domain: dict[str, list[ModRecord]] = {}
    for m in ctx.state.mods.values():
        if m.provider == "nexus" and m.mod_id:
            by_domain.setdefault(m.game_domain, []).append(m)
    for domain, mods in by_domain.items():
        versions = nexus.latest_versions(domain, sorted({m.mod_id for m in mods}))
        for m in mods:
            if m.mod_id in versions:
                m.latest_version = versions[m.mod_id]
    ctx.state.save()
    return sum(1 for m in ctx.state.mods.values() if m.update_available)


def apply(ctx: GameContext, progress: Progress | None = None) -> Report:
    with game_lock(ctx.game):
        return ctx.deployer.deploy(ctx.state, ctx.layout, progress)


def restore(ctx: GameContext, progress: Progress | None = None) -> Report:
    with game_lock(ctx.game):
        r = ctx.deployer.purge(progress)
        ctx.state.dirty_deploy = bool(ctx.state.enabled_ordered())
        ctx.state.save()
        return r


def remap(ctx: GameContext) -> list[str]:
    """Recalcula dónde va cada archivo de cada mod (tras cambiar el tipo de juego).

    El staging guarda el contenido del archivo tal cual, así que no hace falta descargar de nuevo.
    Devuelve los nombres de los mods que no encajan en el tipo nuevo (se dejan como estaban).
    """
    layout = ctx.layout
    failed = []
    for m in ctx.state.mods.values():
        try:
            mp = layout.map_files(ctx.state.staging(m.uid), m.name)
        except LayoutError:
            failed.append(m.name)
            continue
        m.files, m.skipped, m.folders, m.layout = [list(x) for x in mp.files], mp.skipped, mp.folders, layout.id
    ctx.state.dirty_deploy = True
    ctx.state.save()
    return failed
