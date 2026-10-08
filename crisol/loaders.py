"""Cargadores de mods desde sus versiones oficiales en GitHub, instalados en un clic.

BepInEx, MelonLoader y UE4SS se instalan como un mod más (el primero de la lista), así se pueden
desactivar o quitar como cualquier otro y el juego se restaura igual. ME3 no toca el juego: es una
herramienta que va a ~/.local/share/crisol/tools/me3.

Cuando Nexus tiene una versión del cargador hecha para el juego (p. ej. «BepinEx for TG FoA (IL2CPP
branch)» o «UE4SS for Lies of P»), la interfaz la recomienda antes que la genérica de GitHub.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from . import USER_AGENT, archive, jsonio, paths
from .i18n import _

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoaderSpec:
    key: str
    name: str
    repo: str
    asset: str            # expresión regular del archivo de la versión
    prerelease: bool      # buscar también versiones de prueba
    dest: str             # "root" (raíz del juego) | "win64" (<Proyecto>/Binaries/Win64) | "tool" (fuera del juego)
    note: str = ""


SPECS = {s.key: s for s in (
    LoaderSpec("bepinex5", "BepInEx 5 (Mono)", "BepInEx/BepInEx", r"^BepInEx_win_x64_5[\d.]+\.zip$", False, "root"),
    LoaderSpec("bepinex6-il2cpp", "BepInEx 6 (IL2CPP)", "BepInEx/BepInEx",
               r"^BepInEx-Unity\.IL2CPP-win-x64-6[\w.\-]+\.zip$", True, "root",
               _("La versión de GitHub es una versión de prueba antigua y no suele funcionar con juegos Unity "
                 "recientes: si Nexus tiene una hecha para este juego, usa esa.")),
    LoaderSpec("melonloader", "MelonLoader", "LavaGang/MelonLoader", r"^MelonLoader\.x64\.zip$", False, "root"),
    LoaderSpec("ue4ss", "UE4SS", "UE4SS-RE/RE-UE4SS", r"^UE4SS_v[\d.]+\.zip$", False, "win64",
               _("Algunos juegos Unreal 5 recientes necesitan una versión experimental o una configuración "
                 "propia: si Nexus tiene una para este juego, usa esa.")),
    LoaderSpec("me3", "Mod Engine 3 (ME3)", "garyttierney/me3", r"^me3-linux-amd64\.tar\.gz$", False, "tool"),
)}

TOOLS_DIR = paths.DATA_DIR / "tools"
DLL_OVERRIDE = {"bepinex5": "winhttp", "bepinex6-il2cpp": "winhttp", "melonloader": "version", "ue4ss": "dwmapi"}


class LoaderError(Exception):
    pass


def release(spec: LoaderSpec) -> dict:
    """{tag, url, name, size} de la versión más reciente que trae el archivo (caché de 6 h)."""
    cache = paths.CACHE_DIR / "loaders.json"
    data = jsonio.load(cache, {})
    hit = data.get(spec.key)
    if hit and time.time() - hit.get("checked", 0) < 6 * 3600:
        return hit
    try:
        r = requests.get(f"https://api.github.com/repos/{spec.repo}/releases?per_page=15", timeout=20,
                         headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        releases = r.json()
    except (requests.RequestException, ValueError) as e:
        if hit:
            return hit
        raise LoaderError(_("No se pudo consultar GitHub: {0}").format(e)) from e
    rx = re.compile(spec.asset)
    for rel in releases:
        if rel.get("draft") or (rel.get("prerelease") and not spec.prerelease):
            continue
        if not spec.prerelease and not re.match(r"^v?\d", rel.get("tag_name", "")):
            continue  # p. ej. «experimental-latest» de UE4SS
        for a in rel.get("assets", []):
            if rx.match(a.get("name", "")):
                hit = {"tag": rel["tag_name"], "url": a["browser_download_url"], "name": a["name"],
                       "size": int(a.get("size") or 0), "page": rel.get("html_url", ""), "checked": time.time()}
                data[spec.key] = hit
                jsonio.save(cache, data)
                return hit
    raise LoaderError(_("No se ha encontrado {0} en las versiones de {1}").format(spec.name, spec.repo))


def me3_tool() -> Path | None:
    exe = TOOLS_DIR / "me3" / "bin" / "me3"
    return exe if exe.is_file() and (exe.parent / "win64").is_dir() else None


def install(ctx, key: str, progress=None):
    """Descarga e instala un cargador. Devuelve el ModRecord (o None para ME3, que es una herramienta)."""
    from . import manager
    from .layouts import UnrealLayout, staged_files
    from .store import ModRecord
    spec = SPECS[key]
    rel = release(spec)
    dest = paths.DOWNLOADS_DIR / "github" / rel["name"]
    manager.download(rel["url"], dest, rel["size"], progress)
    if spec.dest == "tool":
        target = TOOLS_DIR / "me3"
        tmp = target.with_name("me3.new")
        archive.extract(dest, tmp)
        # El .tar.gz trae «./bin/me3»: se busca la carpeta que contiene bin/me3.
        root = next((p.parent.parent for p in tmp.rglob("me3") if p.parent.name == "bin" and p.is_file()), None)
        if root is None:
            shutil.rmtree(tmp, ignore_errors=True)
            raise LoaderError(_("El paquete de ME3 no tiene el formato esperado"))
        if target.exists():
            shutil.rmtree(target)
        root.rename(target)
        shutil.rmtree(tmp, ignore_errors=True)
        (target / "bin" / "me3").chmod(0o755)
        return None
    # Como un mod más: se fija la carpeta de destino (no se adivina como en los mods normales).
    prefix = ""
    if spec.dest == "win64":
        project = UnrealLayout.find_project(ctx.game.install_dir)
        if not project:
            raise LoaderError(_("No se ha encontrado la carpeta Binaries/Win64 de este juego Unreal"))
        prefix = f"{project}/Binaries/Win64/"
    st = ctx.state
    rec = next((m for m in st.mods.values() if m.provider == "github" and m.file_name.startswith(spec.key + ":")), None)
    rec = rec or ModRecord(uid=st.new_uid(), name=spec.name)
    rec.provider, rec.version, rec.author = "github", rel["tag"].lstrip("v"), spec.repo.split("/")[0]
    rec.file_name, rec.archive = f"{spec.key}:{rel['name']}", str(dest)
    final = st.staging(rec.uid)
    tmp = final.with_name(final.name + ".new")
    archive.extract(dest, tmp)
    files = staged_files(tmp)
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        raise LoaderError(_("El paquete de {0} está vacío").format(spec.name))
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)
    rec.files = [[f, prefix + f] for f in files]
    rec.skipped, rec.folders, rec.layout = [], [], ctx.layout.id
    rec.staged_size = manager.dir_size(final)
    new = rec.uid not in st.mods
    st.add(rec)
    if new:  # el cargador va el primero de la lista y activado
        st.move(rec.uid, 0)
    st.set_enabled(rec.uid, True)
    st.save()
    return rec


def deck_overrides(game_key: str) -> set[str]:
    """DLL que el perfil de Gaming Deck del juego ya hace cargar como nativas (WINEDLLOVERRIDES del ENV)."""
    if not game_key:
        return set()
    data = jsonio.load(Path.home() / ".local/share/gaming-deck/gaming/profiles.json", {})
    value = str(((data.get(game_key) or {}).get("env") or {}).get("WINEDLLOVERRIDES", ""))
    out = set()
    for part in value.split(";"):
        names, _sep, mode = part.partition("=")
        if mode.strip().lower().startswith("n"):
            out |= {n.strip().lower() for n in names.split(",") if n.strip()}
    return out


def dll_override_hint(dlls: list[str], game_key: str = "") -> str:
    """Cómo hacer que Proton cargue las DLL de un cargador (nativas antes que las de Wine).

    Con Gaming Deck se pone en el ENV del perfil del juego: cambiar las opciones de lanzamiento de Steam
    quitaría «gaming-deck run %command%». Sin Gaming Deck, en las opciones de lanzamiento de Steam."""
    import shutil
    stems = sorted({Path(d).stem.lower() for d in dlls})
    if not stems:
        return ""
    value = ",".join(stems) + "=n,b"   # formato de Wine: «a,b=n,b» (no «a=n,b,b=n,b»)
    if shutil.which("gaming-deck"):
        if set(stems) <= deck_overrides(game_key):
            return ""   # ya está en el perfil del juego
        return _("En Proton el juego debe cargar la DLL del cargador: en Gaming Deck → perfil del juego → ENV, "
                 "añade:") + f"\nWINEDLLOVERRIDES={value}"
    return _("En Proton el juego debe cargar la DLL del cargador: en Steam → Propiedades → Opciones de "
             "lanzamiento pon:") + f'\nWINEDLLOVERRIDES="{value}" %command%'


def launch_hint(key: str, game_key: str = "") -> str:
    dll = DLL_OVERRIDE.get(key)
    return dll_override_hint([dll + ".dll"], game_key) if dll else ""
