"""Operaciones de alto nivel por juego: instalar, actualizar, aplicar y comprobar actualizaciones.

Todo lo de aquí es bloqueante (red, disco): la interfaz lo llama desde hilos.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from . import archive, fomod, paths, running, secrets
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

    @property
    def me3_profile(self) -> Path:
        return paths.ME3_DIR / f"{self.game.safe_key}.me3"

    def is_applied(self) -> bool:
        if self.layout.external:
            return self.me3_profile.exists()
        return self.deployer.is_deployed()

    def loader(self):
        return self.layout.loader(self.state.enabled_ordered(), self.state.staging)


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
    """Descarga a dest.part y la renombra al terminar. Si ya hay un .part (descarga cortada o
    cancelada), se continúa desde donde se quedó (petición Range); si el servidor no lo admite,
    se empieza de nuevo."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = tmp.stat().st_size if tmp.exists() else 0
    if expected_size and have > expected_size:
        tmp.unlink()
        have = 0
    headers = {"User-Agent": "Crisol"}
    if have:
        headers["Range"] = f"bytes={have}-"
    try:
        with requests.get(url, stream=True, timeout=30, headers=headers) as r:
            if r.status_code == 416 and expected_size and have == expected_size:
                pass  # ya estaba completo
            else:
                r.raise_for_status()
                resumed = have and r.status_code == 206
                if not resumed:
                    have = 0
                total = have + int(r.headers.get("Content-Length") or 0) or expected_size
                done = have
                if resumed:
                    log.info("reanudando %s desde %.1f MB", dest.name, have / 1e6)
                with tmp.open("ab" if resumed else "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        if cancel and cancel.is_set():
                            # El .part se conserva para poder reanudar más tarde.
                            raise DownloadError("Descarga cancelada (se podrá reanudar)")
                        f.write(chunk)
                        done += len(chunk)
                        if progress and total:
                            extra = " (reanudada)" if resumed else ""
                            progress(done / total, f"{done / 1e6:.1f} / {total / 1e6:.1f} MB{extra}")
    except requests.RequestException as e:
        raise DownloadError(f"La descarga se ha cortado: {e}. Vuelve a pulsar «Slow download» y "
                            "continuará donde se quedó.") from e
    if expected_size and tmp.stat().st_size != expected_size:
        got = tmp.stat().st_size
        if got > expected_size:
            tmp.unlink(missing_ok=True)
            raise DownloadError(f"Descarga dañada: {got} bytes, se esperaban {expected_size}. Se ha borrado.")
        raise DownloadError(f"Descarga incompleta ({got / 1e6:.1f} de {expected_size / 1e6:.1f} MB). "
                            "Vuelve a pulsar «Slow download» y continuará donde se quedó.")
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
    if not archive.is_archive(dest) or not dest.name.lower().endswith(archive.ARCHIVE_EXTS):
        ext = archive.sniff(dest)
        if ext:
            dest = dest.rename(dest.with_name(dest.name + ext))
            fname = dest.name
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

class InstallCancelled(Exception):
    pass


# Elige las opciones de un FOMOD: (módulo, raíz del mod, elección previa o None) → elección, o None = cancelar.
# Se llama desde el hilo de instalación; la interfaz muestra el asistente y espera la respuesta.
FomodChooser = Callable[[fomod.Module, Path, "fomod.Choice | None"], "fomod.Choice | None"]


def _apply_fomod(ctx: GameContext, tmp: Path, rec: ModRecord, chooser: FomodChooser | None,
                 progress: Progress | None) -> None:
    """Si el mod trae instalador FOMOD, deja en tmp solo lo que se elige instalar."""
    cfg = fomod.find_config(tmp)
    if cfg is None:
        rec.fomod_name, rec.fomod_choice = "", []
        return
    module = fomod.parse(cfg)
    mod_root = cfg.parent.parent
    previous = fomod.choice_from_json(rec.fomod_choice) if rec.fomod_choice else None
    choice = chooser(module, mod_root, previous) if chooser else (previous or fomod.default_choice(
        module, ctx.game.install_dir))
    if choice is None:
        raise InstallCancelled("Instalación cancelada")
    if progress:
        progress(0.5, "Instalando las opciones elegidas…")
    out = tmp.with_name(tmp.name + ".fomod")
    shutil.rmtree(out, ignore_errors=True)
    fomod.build(module, choice, mod_root, out, ctx.game.install_dir)
    shutil.rmtree(tmp)
    out.rename(tmp)
    rec.fomod_name = module.name or rec.name
    rec.fomod_choice = fomod.choice_to_json(choice)


def install_archive(ctx: GameContext, archive_path: Path, rec: ModRecord, layout_id: str | None = None,
                    progress: Progress | None = None, chooser: FomodChooser | None = None) -> ModRecord:
    """Extrae el archivo a staging y calcula dónde va cada archivo. Si rec ya existe, lo actualiza
    conservando su posición y estado en todos los perfiles. Si trae instalador FOMOD, chooser
    decide las opciones (sin chooser: las de la vez anterior o las recomendadas)."""
    st = ctx.state
    final = st.staging(rec.uid)
    tmp = final.with_name(final.name + ".new")
    if progress:
        progress(0.0, "Extrayendo…")
    archive.extract(archive_path, tmp)
    try:
        _apply_fomod(ctx, tmp, rec, chooser, progress)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    layout = LAYOUTS[layout_id](ctx.game.install_dir) if layout_id else ctx.layout
    layout.keep_docs = bool(rec.fomod_name)
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
    rec.packages, rec.natives, rec.savefile = mapping.packages, mapping.natives, mapping.savefile
    rec.layout = layout.id
    rec.archive = str(archive_path)
    rec.staged_size = dir_size(final)
    st.add(rec)
    st.save()
    if progress:
        progress(1.0, "Instalado")
    return rec


def dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def archive_size(rec: ModRecord) -> int:
    try:
        return Path(rec.archive).stat().st_size if rec.archive else 0
    except OSError:
        return 0


def disk_usage(ctx: GameContext) -> tuple[int, int]:
    """(bytes de mods extraídos, bytes de archivos descargados) de un juego."""
    staged = 0
    for m in ctx.state.mods.values():
        if not m.staged_size and ctx.state.staging(m.uid).exists():
            m.staged_size = dir_size(ctx.state.staging(m.uid))
        staged += m.staged_size
    return staged, sum(archive_size(m) for m in ctx.state.mods.values())


def delete_archive(ctx: GameContext, uid: str) -> int:
    """Borra el archivo descargado de un mod (ya extraído). Devuelve los bytes liberados."""
    rec = ctx.state.mods[uid]
    size = archive_size(rec)
    if rec.archive:
        Path(rec.archive).unlink(missing_ok=True)
        Path(rec.archive + ".part").unlink(missing_ok=True)
    rec.archive = ""
    ctx.state.save()
    return size


def delete_archives(ctx: GameContext) -> int:
    return sum(delete_archive(ctx, uid) for uid in list(ctx.state.mods))


def install_manual(ctx: GameContext, archive_path: Path, layout_id: str | None = None,
                   chooser: FomodChooser | None = None) -> ModRecord:
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
    return install_archive(ctx, dest, rec, layout_id, chooser=chooser)


def install_from_nxm(ctx: GameContext, nexus: Nexus, link: NxmLink, progress: Progress | None = None,
                     cancel: threading.Event | None = None, chooser: FomodChooser | None = None) -> ModRecord:
    if ctx.state.nexus_domain and link.domain != ctx.state.nexus_domain:
        raise DownloadError(f"El enlace es de otro juego ({link.domain})")
    info = nexus.mod(link.domain, link.mod_id)

    def dl_progress(f: float, msg: str) -> None:
        if progress:
            progress(f * 0.9, msg)

    path, verified, meta = download_nexus(nexus, link, dl_progress, cancel)
    # ¿Actualización de un mod ya instalado? Mismo archivo, o mismo mod y mismo título de archivo
    # (o el único archivo instalado de ese mod) → se conserva el uid, y con él su posición.
    existing = ctx.state.find("nexus", link.mod_id, link.file_id)
    if existing is None:
        same_mod = [m for m in ctx.state.mods.values() if m.provider == "nexus" and m.mod_id == link.mod_id]
        titled = [m for m in same_mod if m.file_title and m.file_title == meta.get("file_title")]
        if titled:
            existing = titled[0]
        elif len(same_mod) == 1:
            existing = same_mod[0]
    rec = existing or ModRecord(uid=ctx.state.new_uid(), name=info.name)
    old_archive = Path(rec.archive) if rec.archive else None
    rec.provider, rec.game_domain = "nexus", link.domain
    rec.mod_id, rec.file_id = link.mod_id, link.file_id
    rec.version = meta["version"] or info.version
    rec.latest_version = info.version
    rec.author, rec.thumbnail = info.author, info.thumbnail
    rec.md5, rec.size, rec.file_name = meta["md5"], meta["size"], meta["file_name"]
    rec.file_title = meta.get("file_title") or rec.file_title
    rec.requirements = info.requirements
    rec.verified = verified
    rec = install_archive(ctx, path, rec, progress=lambda f, m: progress and progress(0.9 + f * 0.1, m),
                          chooser=chooser)
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


def update_target(nexus: Nexus, rec: ModRecord):
    """Archivo de Nexus con la versión nueva de un mod instalado (FileInfo) o None."""
    files = [f for f in nexus.files(rec.game_domain, rec.mod_id)
             if f.category in ("MAIN", "UPDATE", "OPTIONAL", "MISCELLANEOUS") and f.file_id != rec.file_id]
    if not files:
        return None
    same = [f for f in files if rec.file_title and f.name == rec.file_title]
    pool = same or [f for f in files if f.category == "MAIN"] or files
    return max(pool, key=lambda f: f.date)


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
    ctx.state.updates_checked = time.time()
    ctx.state.save()
    return sum(1 for m in ctx.state.mods.values() if m.update_available)


def ensure_closed(ctx: GameContext) -> None:
    """Error si el juego está abierto: cambiar sus archivos ahora podría dejarlo a medias."""
    procs = running.running(ctx.game)
    if procs:
        raise running.GameRunning(ctx.game, procs)


def apply(ctx: GameContext, progress: Progress | None = None) -> Report:
    with game_lock(ctx.game):
        layout = ctx.layout
        if not layout.external:
            ensure_closed(ctx)
        if layout.external:
            # ME3: el juego no se toca; solo se escribe el perfil con los mods activos en orden.
            if ctx.deployer.is_deployed():
                ctx.deployer.purge(progress)  # por si antes se aplicó con otro tipo de juego
            mods = ctx.state.enabled_ordered()
            ctx.me3_profile.parent.mkdir(parents=True, exist_ok=True)
            ctx.me3_profile.write_text(layout.profile_text(mods, ctx.state.staging))
            ctx.state.dirty_deploy = False
            ctx.state.save()
            return Report(placed=sum(len(m.packages) + len(m.natives) for m in mods), method="perfil ME3")
        return ctx.deployer.deploy(ctx.state, layout, progress)


def me3_command(ctx: GameContext) -> list[str] | None:
    """Orden para lanzar el juego con ME3 y el perfil de Crisol (None si falta ME3)."""
    layout = ctx.layout
    if not layout.external:
        return None
    me3 = layout.find_me3([ctx.state.staging(m.uid) for m in ctx.state.mods.values()])
    if not me3:
        return None
    return [str(me3), "launch", "--game", layout.me3_game, "-p", str(ctx.me3_profile)]


def restore(ctx: GameContext, progress: Progress | None = None) -> Report:
    with game_lock(ctx.game):
        if ctx.deployer.is_deployed():
            ensure_closed(ctx)
        ctx.me3_profile.unlink(missing_ok=True)
        r = ctx.deployer.purge(progress)
        ctx.state.dirty_deploy = bool(ctx.state.enabled_ordered())
        ctx.state.save()
        return r


def reinstall(ctx: GameContext, uid: str, progress: Progress | None = None,
              chooser: FomodChooser | None = None) -> ModRecord:
    """Vuelve a extraer un mod desde su descarga (sin bajarlo otra vez), conservando su posición."""
    rec = ctx.state.mods[uid]
    if ctx.layout.external:
        ensure_closed(ctx)  # con ME3 el juego lee los archivos del staging mientras está abierto
    path = Path(rec.archive)
    if not path.is_file():
        raise DownloadError("Ya no está el archivo descargado de este mod; vuelve a descargarlo.")
    if not path.name.lower().endswith(archive.ARCHIVE_EXTS) and archive.sniff(path):
        path = path.rename(path.with_name(path.name + archive.sniff(path)))
        rec.file_name = path.name
    return install_archive(ctx, path, rec, progress=progress, chooser=chooser)


def remap(ctx: GameContext) -> list[str]:
    """Recalcula dónde va cada archivo de cada mod (tras cambiar el tipo de juego).

    El staging guarda el contenido del archivo tal cual, así que no hace falta descargar de nuevo.
    Devuelve los nombres de los mods que no encajan en el tipo nuevo (se dejan como estaban).
    """
    layout = ctx.layout
    failed = []
    for m in ctx.state.mods.values():
        layout.keep_docs = bool(m.fomod_name)
        try:
            mp = layout.map_files(ctx.state.staging(m.uid), m.name)
        except LayoutError:
            failed.append(m.name)
            continue
        m.files, m.skipped, m.folders, m.layout = [list(x) for x in mp.files], mp.skipped, mp.folders, layout.id
        m.packages, m.natives, m.savefile = mp.packages, mp.natives, mp.savefile
    ctx.state.dirty_deploy = True
    ctx.state.save()
    return failed
