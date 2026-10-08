"""Tipos de juego: dónde van los archivos de un mod y cómo se expresa el orden de carga.

Cada motor organiza los mods de forma distinta, así que el reparto de archivos y el orden se
delegan aquí:

- kcd2:   Kingdom Come: Deliverance II. Cada mod es una carpeta en mods/ (con mod.manifest);
          el orden lo fija mods/mod_order.txt, que además es lista blanca.
- unreal: juegos Unreal. Los .pak/.ucas/.utoc van a <Proyecto>/Content/Paks/~mods y se renombran
          con prefijo NNN_ según el orden (la convención de la comunidad: el último en orden
          alfabético gana). El resto de archivos se colocan respecto a la raíz del juego.
- loose:  genérico (Unity/BepInEx/MelonLoader y otros). Archivos sueltos sobre la raíz del juego;
          si dos mods traen el mismo archivo gana el de más abajo en la lista.
"""
from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from .i18n import _

# Archivos que no se despliegan nunca (instaladores FOMOD, basura de macOS…).
_JUNK_DIRS = {"fomod", "__macosx"}
_DOC_EXTS = {".txt", ".md", ".pdf", ".url", ".html", ".htm", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".rtf"}
_PAK_EXTS = {".pak", ".ucas", ".utoc", ".sig"}
# DLL de proxy con las que se cargan los cargadores de mods (BepInEx, UE4SS, ASI…).
_PROXY_DLLS = {"winhttp.dll", "version.dll", "dinput8.dll", "dxgi.dll", "d3d11.dll", "dsound.dll", "winmm.dll",
                "dwmapi.dll", "xinput1_3.dll"}


class LayoutError(Exception):
    pass


class NotNeeded(LayoutError):
    """El mod es algo que este tipo de juego ya resuelve por sí mismo (p. ej. otro cargador con ME3)."""


# Lo que Mod Engine 3 ya hace por sí mismo: otros cargadores y el arranque sin anticheat.
ME3_REDUNDANT_FILES = {"dinput8.dll", "modengine2_launcher.exe", "modengine2.dll", "toggle_anti_cheat.exe",
                       "start_game_in_offline_mode.exe", "launchmod_eldenring.bat"}
ME3_REDUNDANT_NAMES = ("mod loader", "mod engine 2", "modengine2", "anti-cheat toggler", "anticheat toggler",
                       "toggle anti-cheat", "toggleanticheat", "offline launcher")


def me3_redundant(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in ME3_REDUNDANT_NAMES)


@dataclass
class Mapping:
    files: list[tuple[str, str]]               # (ruta en staging, ruta en el juego), POSIX relativas
    skipped: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)  # carpetas de mod en mods/ (kcd2)
    packages: list[str] = field(default_factory=list)  # ME3
    natives: list[str] = field(default_factory=list)   # ME3
    savefile: str = ""
    me3_variants: list[str] = field(default_factory=list)
    me3_variant: str = ""


def staged_files(staged: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(staged):
        dirnames[:] = [d for d in dirnames if d.lower() not in _JUNK_DIRS]
        for f in filenames:
            out.append(Path(dirpath, f).relative_to(staged).as_posix())
    return sorted(out)


def _children(d: Path) -> set[str]:
    try:
        return {e.name.lower() for e in list(os.scandir(d))}
    except OSError:
        return set()


def _is_doc(rel: str) -> bool:
    return PurePosixPath(rel).suffix.lower() in _DOC_EXTS


def _anchor(staged: Path, files: list[str], game_dir: Path, anchors: list[str],
            ignore: set | None = None) -> tuple[str, str] | None:
    """Busca qué carpeta del archivo corresponde a qué carpeta del juego.

    Se elige la pareja (carpeta del archivo, carpeta ancla del juego) con más nombres en común en
    su primer nivel; así un zip «MiMod/BepInEx/plugins/x.dll» se coloca sobre la raíz del juego.
    """
    dirs = {""} | {str(PurePosixPath(f).parent) for f in files}
    for f in list(dirs):  # todas las carpetas intermedias
        p = PurePosixPath(f)
        dirs.update(str(a) for a in p.parents)
    dirs = {"" if d == "." else d for d in dirs}
    best: tuple[int, int, str, str] | None = None
    for a in anchors:
        game_names = _children(game_dir / a) if a else _children(game_dir)
        if ignore:  # lo que puso Crisol en esa carpeta no cuenta como del juego
            game_names -= {PurePosixPath(i).name for i in ignore if str(PurePosixPath(i).parent).lower()
                           in ((a.lower() if a else "."), )}
        if not game_names:
            continue
        for d in dirs:
            names = _children(staged / d) if d else _children(staged)
            score = len(names & game_names)
            if score and (best is None or (score, -d.count("/")) > (best[0], best[1])):
                best = (score, -d.count("/"), d, a)
    return (best[2], best[3]) if best else None


def _strip_wrappers(staged: Path) -> str:
    """Baja por carpetas envoltorio (una sola carpeta y ningún archivo)."""
    rel = ""
    while True:
        d = staged / rel if rel else staged
        entries = [e for e in list(os.scandir(d)) if e.name.lower() not in _JUNK_DIRS]
        if len(entries) == 1 and entries[0].is_dir():
            rel = f"{rel}/{entries[0].name}" if rel else entries[0].name
        else:
            return rel


def _under(f: str, d: str) -> str | None:
    if not d:
        return f
    return f[len(d) + 1:] if f.startswith(d + "/") else None


def _join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


@dataclass
class Loader:
    """Cargador de mods del juego: si hace falta, si está instalado y dónde conseguirlo."""
    name: str
    level: str              # "required" (obligatorio) | "optional" (depende del mod) | "none" (no usa)
    installed: bool
    detail: str = ""
    url: str = ""
    engine: str = ""        # motor detectado, para mostrarlo
    search: tuple = ()      # nombres con los que buscarlo en Nexus
    install_key: str = ""   # cargador oficial que Crisol puede instalar (loaders.SPECS)

    @property
    def needed(self) -> bool:
        return self.level == "required"


# Nombres de cargadores en los requisitos de un mod de Nexus → (nombre, términos).
LOADER_NAMES = {"bepinex": "BepInEx", "melonloader": "MelonLoader", "ue4ss": "UE4SS", "mod engine": "Mod Engine",
                "me3": "Mod Engine 3", "script extender": "Script Extender", "asi loader": "ASI Loader",
                "modloader": "Mod Loader", "mod loader": "Mod Loader"}


def loader_in_text(text: str) -> str | None:
    t = text.lower()
    return next((v for k, v in LOADER_NAMES.items() if k in t), None)


def unity_engine(game_dir: Path) -> str | None:
    names = _children(game_dir)
    if "unityplayer.dll" not in names:
        return None
    return "Unity (IL2CPP)" if "gameassembly.dll" in names else "Unity (Mono)"


class Layout:
    id = ""
    label = ""
    description = ""
    anchors: list[str] = [""]
    supports_internal_conflicts = False
    keep_docs = False  # True tras un FOMOD: lo que instala es intencionado, no se descartan «léemes»
    external = False   # True: los mods no se copian al juego (los carga un lanzador, p. ej. ME3)

    def __init__(self, game_dir: Path):
        self.game_dir = game_dir

    @classmethod
    def detect(cls, game_dir: Path, nexus_domain: str | None) -> bool:
        return False

    def map_files(self, staged: Path, mod_name: str) -> Mapping:
        files = staged_files(staged)
        if not files:
            raise LayoutError(_('El archivo del mod está vacío'))
        found = _anchor(staged, files, self.game_dir, self.anchors, self.ignore_paths)
        if found:
            src_dir, game_rel = found
        else:
            src_dir, game_rel = _strip_wrappers(staged), self.default_dir()
        # Sin carpeta que indique el destino: un archivo suelto que se llama igual que uno (y solo uno) del
        # juego lo sustituye allí (p. ej. vídeos de intro en Content/Movies/Splash).
        by_name = self._name_index() if not found else {}
        mapped, skipped = [], []
        for f in files:
            rel = _under(f, src_dir)
            if rel is None or (_is_doc(f) and "/" not in rel and not self.keep_docs):
                skipped.append(f)  # fuera de la carpeta elegida, o un léeme suelto
                continue
            same = by_name.get(PurePosixPath(rel).name.lower(), []) if "/" not in rel else []
            mapped.append((f, same[0] if len(same) == 1 else _join(game_rel, rel)))
        if not mapped:
            raise LayoutError(_('No se ha encontrado nada que instalar en el archivo del mod'))
        return Mapping(mapped, skipped)

    def default_dir(self) -> str:
        return ""

    ignore_paths: set = set()   # archivos que puso Crisol (no son del juego): los rellena el gestor

    def _name_index(self) -> dict[str, list[str]]:
        """Nombre de archivo (minúsculas) → rutas en el juego, sin contar lo que puso Crisol."""
        out: dict[str, list[str]] = {}
        for dirpath, dirnames, filenames in os.walk(self.game_dir):
            dirnames[:] = [d for d in dirnames if d.lower() not in ("saved", "logs", "crashes")]
            rel_dir = Path(dirpath).relative_to(self.game_dir).as_posix()
            for fname in filenames:
                rel = fname if rel_dir == "." else f"{rel_dir}/{fname}"
                if rel.lower() not in self.ignore_paths:
                    out.setdefault(fname.lower(), []).append(rel)
        return out

    def target(self, dst: str, position: int) -> str:
        """Ruta final de un archivo según la posición del mod en el orden (1 = primero)."""
        return dst

    def generated(self, ordered_folders: list[str], managed_folders: set[str]) -> dict[str, bytes]:
        """Archivos que Crisol genera para expresar el orden (p. ej. mod_order.txt)."""
        return {}

    def internal_entries(self, path: Path) -> list[str]:
        """Rutas internas de un paquete (para conflictos dentro de .pak), si el formato lo permite."""
        return []

    game_key = ""   # «steam:<appid>»: para saber si Gaming Deck ya tiene lo que hace falta

    def notes(self, deployed_targets: list[str]) -> list[str]:
        # DLL de proxy junto al ejecutable: en la raíz o en <Proyecto>/Binaries/Win64 (Unreal)
        names = {PurePosixPath(t).name.lower() for t in deployed_targets
                 if "/" not in t or str(PurePosixPath(t).parent).lower().endswith("binaries/win64")}
        proxies = sorted(names & _PROXY_DLLS)
        if proxies:
            from .loaders import dll_override_hint
            hint = dll_override_hint(proxies, self.game_key)
            return [_('Hay un cargador de mods ({0}).').format(', '.join(proxies)) + " " + hint] if hint else []
        return []

    def loader(self, mods: list, staging) -> Loader | None:
        """Cargador que necesitan los mods activos o, si ninguno lo pide, lo que se sabe del juego."""
        return None

    # Orden: qué gana en un conflicto de archivos (texto para la interfaz).
    order_hint = _('Si dos mods traen el mismo archivo, gana el que está más abajo en la lista.')


class LooseLayout(Layout):
    id = "loose"
    label = _('Archivos sueltos')
    description = _('Los archivos del mod se colocan sobre la carpeta del juego (Unity, BepInEx, genéricos).')

    @classmethod
    def detect(cls, game_dir, nexus_domain):
        return True

    def loader(self, mods, staging):
        # Mods que van dentro de BepInEx/plugins o Mods/ (MelonLoader) necesitan su cargador; el
        # cargador puede venir del juego o de otro mod instalado.
        dsts = [d.lower() for m in mods for _u, d in m.files]
        game = _children(self.game_dir)
        engine = unity_engine(self.game_dir) or ""
        il2cpp = "IL2CPP" in engine
        have_bep = ("bepinex" in game and (ci_dir(self.game_dir, "BepInEx/core") is not None)) or \
            any(d.startswith("bepinex/core/") for d in dsts)
        have_melon = "melonloader" in game or any(d.startswith("melonloader/") for d in dsts)
        bep = ("BepInEx" + (" 6 (IL2CPP)" if il2cpp else ""), "https://github.com/BepInEx/BepInEx/releases")
        if any(d.startswith("bepinex/plugins/") for d in dsts):
            return Loader(bep[0], "required", have_bep, _('Algún mod activo es un plugin de BepInEx.') + ("" if have_bep
                          else _(' Instálalo como un mod más y ponlo el primero.')), bep[1], engine, ("BepInEx",),
                          "bepinex6-il2cpp" if il2cpp else "bepinex5")
        if any(d.startswith("mods/") and d.endswith(".dll") for d in dsts):
            return Loader("MelonLoader", "required", have_melon, _('Algún mod activo es de MelonLoader.') + (
                          "" if have_melon else _(' Instálalo como un mod más y ponlo el primero.')),
                          "https://github.com/LavaGang/MelonLoader/releases", engine, ("MelonLoader",), "melonloader")
        if engine:
            name = "BepInEx" if have_bep else "MelonLoader" if have_melon else _('BepInEx o MelonLoader')
            return Loader(name, "optional", have_bep or have_melon,
                          _('Juego {0}. Los mods de archivos (texturas, ajustes) no necesitan cargador; los plugins (.dll) necesitan BepInEx o MelonLoader según el mod: míralo en sus requisitos.').format(engine),
                          bep[1], engine, ("BepInEx", "MelonLoader"),
                          "" if (have_bep or have_melon) else ("bepinex6-il2cpp" if il2cpp else "bepinex5"))
        return Loader(_('Ninguno conocido'), "none", False,
                      _('No se ha detectado un motor con cargador habitual: los mods se copian tal cual. Si un mod pide un cargador, aparecerá en sus requisitos.'), "", "", ("Mod Loader", "Script Extender"))


class UnrealLayout(Layout):
    id = "unreal"
    label = _('Unreal Engine (.pak)')
    description = _('Los .pak van a Content/Paks/~mods con prefijo según el orden; el resto, sobre el juego.')
    order_hint = (_('Los .pak se renombran 001_, 002_… según la lista; por convención en Unreal gana el que se carga último (el de más abajo).'))

    def __init__(self, game_dir):
        super().__init__(game_dir)
        self.project = self.find_project(game_dir) or ""
        self.anchors = ["", self.project, _join(self.project, "Binaries/Win64")] if self.project else [""]

    @staticmethod
    def find_project(game_dir: Path) -> str | None:
        try:
            for e in list(os.scandir(game_dir)):
                if e.is_dir() and e.name != "Engine" and (Path(e.path) / "Content" / "Paks").is_dir():
                    return e.name
        except OSError:
            pass
        return None

    @classmethod
    def detect(cls, game_dir, nexus_domain):
        return cls.find_project(game_dir) is not None

    def paks_dir(self) -> str:
        return _join(self.project, "Content/Paks")

    def map_files(self, staged, mod_name):
        files = staged_files(staged)
        paks = [f for f in files if PurePosixPath(f).suffix.lower() in _PAK_EXTS]
        rest = [f for f in files if f not in paks]
        mapped: list[tuple[str, str]] = []
        for f in paks:
            # LogicMods (mods de blueprints de UE4SS) tienen su propia carpeta junto a ~mods.
            sub = "LogicMods" if "/logicmods/" in f"/{f.lower()}" else "~mods"
            mapped.append((f, _join(self.paks_dir(), sub, PurePosixPath(f).name)))
        skipped: list[str] = []
        # UE4SS (dwmapi.dll/UE4SS.dll/xinput1_3.dll o carpeta ue4ss/): todo lo de su carpeta va a Binaries/Win64,
        # salvo que el mod ya traiga la ruta «…/Binaries/Win64» (entonces lo coloca el ancla de abajo).
        loader_dir = None
        for f in rest:
            parts = PurePosixPath(f).parts
            if parts[-1].lower() in ("dwmapi.dll", "ue4ss.dll", "xinput1_3.dll"):
                loader_dir = "/".join(parts[:-1])
                break
            low = [x.lower() for x in parts[:-1]]
            if "ue4ss" in low:
                loader_dir = "/".join(parts[:low.index("ue4ss")])
                break
        if loader_dir is not None and self.project and not loader_dir.lower().endswith("binaries/win64"):
            win64 = _join(self.project, "Binaries/Win64")
            for f in list(rest):
                rel = _under(f, loader_dir)
                if rel is not None and not (_is_doc(f) and "/" not in rel):
                    mapped.append((f, _join(win64, rel)))
                    rest.remove(f)
        if rest:
            try:
                tmp = Layout.map_files(self, staged, mod_name)
                done = set(paks) | {m[0] for m in mapped}
                mapped += [m for m in tmp.files if m[0] not in done]
                skipped += [s for s in tmp.skipped if s not in done]
            except LayoutError:
                if not paks:
                    raise
                skipped += rest
        if not mapped:
            raise LayoutError(_('No se ha encontrado ningún .pak ni archivos reconocibles en el mod'))
        return Mapping(mapped, skipped)

    def loader(self, mods, staging):
        dsts = [d.lower() for m in mods for _u, d in m.files]
        needs = any("/logicmods/" in f"/{d}" or "/ue4ss/mods/" in f"/{d}" or d.endswith("/scripts/main.lua")
                    for d in dsts)
        win64 = self.game_dir / self.project / "Binaries" / "Win64"
        have = _children(win64)
        ok = bool({"ue4ss.dll", "ue4ss"} & have) or any(d.endswith(("/ue4ss.dll", "/dwmapi.dll")) for d in dsts)
        url = "https://github.com/UE4SS-RE/RE-UE4SS/releases"
        failed = self.ue4ss_failed(win64)
        if ok and failed:
            # Instalado pero no arranca (p. ej. le faltan las firmas del juego): no cuenta como instalado.
            return Loader("UE4SS", "required" if needs else "optional", False,
                          _("UE4SS está instalado pero no ha podido arrancar en este juego ({0}). Suele faltar una "
                            "configuración propia del juego: desinstálalo y usa la versión de Nexus hecha para "
                            "este juego.").format(failed), url, "Unreal Engine", ("UE4SS",))
        if needs:
            return Loader("UE4SS", "required", ok, _('Algún mod activo usa scripts o LogicMods de UE4SS.') + ("" if ok
                          else _(' Instálalo como un mod más (suele estar en la página del juego en Nexus).')),
                          url, "Unreal Engine", ("UE4SS",), "ue4ss")
        return Loader("UE4SS", "optional", ok, _('Unreal Engine: los mods .pak no necesitan cargador; los de scripts (Lua) o LogicMods necesitan UE4SS.'), url, "Unreal Engine", ("UE4SS",), "ue4ss")

    @staticmethod
    def ue4ss_failed(win64: Path) -> str:
        """El último error fatal del registro de UE4SS de la última partida, o «»."""
        # UE4SS 3.x escribe en Win64/ o en Win64/ue4ss/ según la versión: vale el de la última partida.
        logs = [p for d in (win64, win64 / "ue4ss") if d.is_dir() for p in d.glob("*")
                if p.name.lower() == "ue4ss.log"] if win64.is_dir() else []
        log = max(logs, key=lambda p: p.stat().st_mtime) if logs else None
        try:
            lines = log.read_text(errors="replace").splitlines()[-40:] if log else []
        except OSError:
            return ""
        fatal = [ln.split("Fatal Error:", 1)[1].strip() for ln in lines if "Fatal Error:" in ln]
        return fatal[-1] if fatal else ""

    def target(self, dst, position):
        p = PurePosixPath(dst)
        if p.parent.name == "~mods" and p.suffix.lower() in _PAK_EXTS:
            return str(p.parent / f"{position:03d}_{p.name}")
        return dst


class KCD2Layout(Layout):
    id = "kcd2"
    label = "Kingdom Come: Deliverance II"
    description = _('Cada mod es una carpeta en mods/; el orden se escribe en mods/mod_order.txt.')
    order_hint = (_('El juego carga los mods en el orden de la lista (mods/mod_order.txt). Los mods desactivados no se cargan.'))
    supports_internal_conflicts = True
    MODS = "mods"

    @classmethod
    def detect(cls, game_dir, nexus_domain):
        return nexus_domain == "kingdomcomedeliverance2" or (game_dir / "Bin" / "Win64MasterMasterSteamPGO").is_dir()

    def map_files(self, staged, mod_name):
        files = staged_files(staged)
        if not files:
            raise LayoutError(_('El archivo del mod está vacío'))
        manifests = [f for f in files if PurePosixPath(f).name.lower() == "mod.manifest"]
        roots = sorted({str(PurePosixPath(m).parent) for m in manifests})
        roots = ["" if r == "." else r for r in roots]
        if not roots:
            # Sin manifiesto: una carpeta que contenga Data/ con .pak es la carpeta del mod.
            datas = {str(PurePosixPath(f).parents[1]) for f in files
                     if f.lower().endswith(".pak") and len(PurePosixPath(f).parts) >= 2
                     and PurePosixPath(f).parent.name.lower() == "data"}
            roots = sorted("" if r == "." else r for r in datas)
        if not roots:
            raise LayoutError(_('No parece un mod de KCD2: no tiene mod.manifest ni carpeta Data con .pak. Puedes instalarlo como «Archivos sueltos».'))
        mapped, used = [], set()
        folders = []
        for r in roots:
            folder = PurePosixPath(r).name if r else _safe_folder(mod_name)
            folders.append(folder)
            for f in files:
                rel = _under(f, r)
                if rel is None or f in used:
                    continue
                used.add(f)
                mapped.append((f, _join(self.MODS, folder, rel)))
        skipped = [f for f in files if f not in used]
        return Mapping(mapped, skipped, folders=folders)

    def generated(self, ordered_folders, managed_folders):
        if not ordered_folders:
            return {}
        lines = ["# Generado por Crisol: orden de carga de los mods (el primero se carga antes).",
                 "# Edítalo desde Crisol; los cambios hechos a mano se perderán al aplicar."]
        # mod_order.txt es lista blanca: los mods puestos a mano (no gestionados por Crisol) se
        # añaden al final para no desactivarlos sin querer.
        managed = {f.lower() for f in managed_folders}
        ci = {f.lower() for f in ordered_folders}
        try:
            mods_dir = next((self.game_dir / e.name for e in list(os.scandir(self.game_dir))
                             if e.is_dir() and e.name.lower() == self.MODS), None)
            manual = sorted(e.name for e in list(os.scandir(mods_dir)) if e.is_dir()
                            and e.name.lower() not in managed and e.name.lower() not in ci) if mods_dir else []
        except OSError:
            manual = []
        if manual:
            lines.append("# (orden de Crisol)")
        body = ordered_folders + (["# Mods instalados a mano, fuera de Crisol:"] + manual if manual else [])
        return {_join(self.MODS, "mod_order.txt"): ("\n".join(lines + body) + "\n").encode()}

    def loader(self, mods, staging):
        return Loader(_('Ninguno'), "none", True, _('KCD2 carga los mods de la carpeta mods/ por sí mismo.'),
                      engine="CryEngine")

    def internal_entries(self, path):
        if path.suffix.lower() != ".pak":
            return []
        try:
            with zipfile.ZipFile(path) as z:  # los .pak de KCD2 son zip sin comprimir
                return [n.lower() for n in z.namelist() if not n.endswith("/")]
        except (zipfile.BadZipFile, OSError):
            return []


# Carpetas de recursos de los juegos de FromSoftware: una carpeta que contiene alguna es un paquete ME3.
_FS_ASSETS = {"action", "asset", "chr", "cutscene", "event", "font", "map", "material", "menu", "movie", "msg",
              "obj", "other", "param", "parts", "script", "sd", "sfx", "shader", "sound", "regulation.bin"}
# Juego de ME3 según el ejecutable que hay en la carpeta del juego.
_ME3_GAMES = {"eldenring.exe": "eldenring", "nightreign.exe": "nightreign", "darksoulsiii.exe": "darksouls3",
              "sekiro.exe": "sekiro", "armoredcore6.exe": "armoredcore6"}


class ME3Layout(Layout):
    id = "me3"
    label = _('FromSoftware (ME3)')
    description = (_('Los mods no se copian al juego: Crisol escribe un perfil de Mod Engine 3 con los mods activos en orden y el juego se lanza con ME3 (Elden Ring, Nightreign, DS3, Sekiro, AC6).'))
    order_hint = (_('Los mods se escriben en el perfil de ME3 en el orden de la lista. El juego no se modifica: «Aplicar» solo guarda el perfil.'))
    external = True
    preferred_profile = ""  # variante .me3 elegida para el mod que se está colocando

    def __init__(self, game_dir):
        super().__init__(game_dir)
        self.exe_dir, self.me3_game = self.find_game(game_dir)

    @staticmethod
    def find_game(game_dir: Path) -> tuple[Path | None, str]:
        for d in (game_dir / "Game", game_dir):
            names = _children(d)
            for exe, g in _ME3_GAMES.items():
                if exe in names:
                    return d, g
        return None, ""

    @classmethod
    def detect(cls, game_dir, nexus_domain):
        return bool(cls.find_game(game_dir)[1])

    def map_files(self, staged, mod_name):
        files = staged_files(staged)
        if not files:
            raise LayoutError(_('El archivo del mod está vacío'))
        packages, natives, savefile, variants, variant = self._from_profile(staged, files, self.preferred_profile)
        if not packages and not natives:
            dirs = sorted({str(PurePosixPath(f).parent) for f in files} | {str(a) for f in files
                          for a in PurePosixPath(f).parents}, key=lambda d: d.count("/"))
            for d in dirs:
                d = "" if d == "." else d
                if any(d == p or d.startswith(p + "/") for p in packages):
                    continue
                if _children(staged / d if d else staged) & _FS_ASSETS:
                    packages.append(d)
            natives = [f for f in files if f.lower().endswith(".dll")
                       and not any(f.startswith(p + "/") or not p for p in packages)
                       and "/me3/" not in f"/{f.lower()}"
                       # DLL de proxy (p. ej. _winhttp.dll de otros cargadores): no son mods de ME3
                       and PurePosixPath(f).name.lower().lstrip("_") not in _PROXY_DLLS]
        if not packages and not natives and (
                {PurePosixPath(f).name.lower() for f in files} & ME3_REDUNDANT_FILES or me3_redundant(mod_name)):
            raise NotNeeded(_("Es otro cargador o un lanzador sin anticheat y con Mod Engine 3 no hace falta: ME3 ya "
                              "carga los mods (también las DLL de Elden Mod Loader) y arranca el juego sin anticheat."))
        if not packages and not natives:
            raise LayoutError(_('No parece un mod para Mod Engine 3: no trae perfil .me3, ni carpetas de recursos (param, map, chr…), ni DLL.'))
        mapped = []
        for f in files:
            for p in packages:
                rel = _under(f, p)
                if rel is not None:
                    mapped.append((f, rel))  # destino = ruta dentro del paquete (para ver conflictos)
                    break
        return Mapping(mapped, [], packages=packages, natives=natives, savefile=savefile,
                       me3_variants=variants, me3_variant=variant)

    @staticmethod
    def _from_profile(staged: Path, files: list[str], preferred: str = ""):
        """Si el mod trae sus perfiles .me3 (p. ej. The Convergence normal y Seamless Co-op), se usan sus
        rutas. Solo cuentan los perfiles cuyos archivos existen; se usa el preferido si vale.
        Devuelve (paquetes, nativas, partida, variantes, variante elegida)."""
        import tomllib
        staged_res = staged.resolve()
        usable: dict[str, tuple] = {}
        for f in sorted((f for f in files if f.lower().endswith(".me3")), key=lambda f: ("seamless" in f.lower(), f)):
            prof = staged / f
            try:
                data = tomllib.loads(prof.read_text())
            except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
                continue

            def rel(path: str, prof=prof):
                p = (prof.parent / path).resolve()
                try:
                    return p.relative_to(staged_res).as_posix(), p.exists()
                except ValueError:
                    return None, False
            pk = [rel(x.get("path", "")) for x in data.get("package", [])]
            nt = [rel(x.get("path", "")) for x in data.get("natives", [])]
            if not pk and not nt or not all(ok for _u, ok in pk + nt):
                continue  # el perfil apunta a archivos que no están (p. ej. Seamless Co-op sin instalar)
            usable[PurePosixPath(f).stem] = (["" if r == "." else r for r, _u in pk], [r for r, _u in nt],
                                              str(data.get("savefile") or ""))
        if not usable:
            return [], [], "", [], ""
        name = preferred if preferred in usable else next(iter(usable))
        packages, natives, save = usable[name]
        return packages, natives, save, list(usable), name

    def find_me3(self, staging_dirs: list[Path]) -> Path | None:
        """me3 del sistema o el que trae algún mod instalado (The Convergence lo incluye)."""
        import shutil as _sh
        found = _sh.which("me3")
        if found:
            return Path(found)
        from .loaders import me3_tool
        tool = me3_tool()  # instalado desde GitHub con «Instalar»
        if tool:
            return tool
        for d in staging_dirs:
            for cand in d.glob("**/me3/Linux/me3") if d.is_dir() else []:
                if (cand.parent / "win64").is_dir():
                    cand.chmod(cand.stat().st_mode | 0o111)
                    return cand
        return None

    def profile_text(self, mods: list, staging) -> str:
        lines = ['profileVersion = "v1"']
        save = next((m.savefile for m in mods if m.savefile), "")
        if save:
            lines.append(f"savefile = {_toml_str(save)}")
        lines += ["", "[[supports]]", f"game = {_toml_str(self.me3_game)}"]
        for m in mods:
            for i, pkg in enumerate(m.packages):
                path = staging(m.uid) / pkg if pkg else staging(m.uid)
                lines += ["", f"# {m.name}", "[[package]]", f"id = {_toml_str(f'{m.uid}-{i}')}",
                          f"path = {_toml_str(str(path))}"]
        for m in mods:
            for n in m.natives:
                lines += ["", f"# {m.name}", "[[natives]]", f"path = {_toml_str(str(staging(m.uid) / n))}"]
        return "\n".join(lines) + "\n"

    def loader(self, mods, staging):
        me3 = self.find_me3([staging(m.uid) for m in mods])
        source = next((m.name for m in mods if me3 and str(me3).startswith(str(staging(m.uid)) + "/")), "")
        detail = (_('Se usa {0}').format(me3) + (" " + _("(viene con «{0}»)").format(source) if source else "")
                  if me3 else _('Hace falta ME3 para cargar los mods. Algunos mods lo traen (The Convergence); si no, descárgalo de su página de versiones en GitHub (incluye la de Linux).'))
        return Loader("Mod Engine 3 (ME3)", "required", me3 is not None, detail,
                      "https://github.com/garyttierney/me3/releases", "FromSoftware", ("Mod Engine 3", "ME3"), "me3")


def ci_dir(root: Path, rel: str) -> Path | None:
    """Carpeta rel dentro de root sin distinguir mayúsculas (como Wine), o None."""
    cur = root
    for part in rel.split("/"):
        nxt = next((cur / n for n in (list(os.listdir(cur)) if cur.is_dir() else []) if n.lower() == part.lower()), None)
        if nxt is None:
            return None
        cur = nxt
    return cur if cur.is_dir() else None


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _safe_folder(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_").lower()
    return s or "mod"


LAYOUTS: dict[str, type[Layout]] = {c.id: c for c in (KCD2Layout, ME3Layout, UnrealLayout, LooseLayout)}


def detect(game_dir: Path, nexus_domain: str | None) -> type[Layout]:
    for cls in (KCD2Layout, ME3Layout, UnrealLayout, LooseLayout):
        if cls.detect(game_dir, nexus_domain):
            return cls
    return LooseLayout
