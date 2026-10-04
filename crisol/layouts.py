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

# Archivos que no se despliegan nunca (instaladores FOMOD, basura de macOS…).
_JUNK_DIRS = {"fomod", "__macosx"}
_DOC_EXTS = {".txt", ".md", ".pdf", ".url", ".html", ".htm", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".rtf"}
_PAK_EXTS = {".pak", ".ucas", ".utoc", ".sig"}
# DLL de proxy con las que se cargan los cargadores de mods (BepInEx, UE4SS, ASI…).
_PROXY_DLLS = {"winhttp.dll", "version.dll", "dinput8.dll", "dxgi.dll", "d3d11.dll", "dsound.dll", "winmm.dll"}


class LayoutError(Exception):
    pass


@dataclass
class Mapping:
    files: list[tuple[str, str]]               # (ruta en staging, ruta en el juego), POSIX relativas
    skipped: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)  # carpetas de mod en mods/ (kcd2)


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


def _anchor(staged: Path, files: list[str], game_dir: Path, anchors: list[str]) -> tuple[str, str] | None:
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


class Layout:
    id = ""
    label = ""
    description = ""
    anchors: list[str] = [""]
    supports_internal_conflicts = False

    def __init__(self, game_dir: Path):
        self.game_dir = game_dir

    @classmethod
    def detect(cls, game_dir: Path, nexus_domain: str | None) -> bool:
        return False

    def map_files(self, staged: Path, mod_name: str) -> Mapping:
        files = staged_files(staged)
        if not files:
            raise LayoutError("El archivo del mod está vacío")
        found = _anchor(staged, files, self.game_dir, self.anchors)
        if found:
            src_dir, game_rel = found
        else:
            src_dir, game_rel = _strip_wrappers(staged), self.default_dir()
        mapped, skipped = [], []
        for f in files:
            rel = _under(f, src_dir)
            if rel is None or (_is_doc(f) and "/" not in rel):
                skipped.append(f)  # fuera de la carpeta elegida, o un léeme suelto
                continue
            mapped.append((f, _join(game_rel, rel)))
        if not mapped:
            raise LayoutError("No se ha encontrado nada que instalar en el archivo del mod")
        return Mapping(mapped, skipped)

    def default_dir(self) -> str:
        return ""

    def target(self, dst: str, position: int) -> str:
        """Ruta final de un archivo según la posición del mod en el orden (1 = primero)."""
        return dst

    def generated(self, ordered_folders: list[str], managed_folders: set[str]) -> dict[str, bytes]:
        """Archivos que Crisol genera para expresar el orden (p. ej. mod_order.txt)."""
        return {}

    def internal_entries(self, path: Path) -> list[str]:
        """Rutas internas de un paquete (para conflictos dentro de .pak), si el formato lo permite."""
        return []

    def notes(self, deployed_targets: list[str]) -> list[str]:
        names = {PurePosixPath(t).name.lower() for t in deployed_targets if "/" not in t}
        proxies = sorted(names & _PROXY_DLLS)
        if proxies:
            ov = ",".join(f"{Path(p).stem}=n,b" for p in proxies)
            return [f"Hay un cargador de mods ({', '.join(proxies)}). En Proton debe cargarse la DLL nativa: "
                    f"en Steam → Propiedades → Opciones de lanzamiento pon "
                    f"WINEDLLOVERRIDES=\"{ov}\" %command%"]
        return []

    # Orden: qué gana en un conflicto de archivos (texto para la interfaz).
    order_hint = "Si dos mods traen el mismo archivo, gana el que está más abajo en la lista."


class LooseLayout(Layout):
    id = "loose"
    label = "Archivos sueltos"
    description = "Los archivos del mod se colocan sobre la carpeta del juego (Unity, BepInEx, genéricos)."

    @classmethod
    def detect(cls, game_dir, nexus_domain):
        return True


class UnrealLayout(Layout):
    id = "unreal"
    label = "Unreal Engine (.pak)"
    description = "Los .pak van a Content/Paks/~mods con prefijo según el orden; el resto, sobre el juego."
    order_hint = ("Los .pak se renombran 001_, 002_… según la lista; por convención en Unreal gana el que se "
                  "carga último (el de más abajo).")

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
        if rest:
            try:
                tmp = Layout.map_files(self, staged, mod_name)
                pakset = set(paks)
                mapped += [m for m in tmp.files if m[0] not in pakset]
                skipped += [s for s in tmp.skipped if s not in pakset]
            except LayoutError:
                if not paks:
                    raise
                skipped += rest
        if not mapped:
            raise LayoutError("No se ha encontrado ningún .pak ni archivos reconocibles en el mod")
        return Mapping(mapped, skipped)

    def target(self, dst, position):
        p = PurePosixPath(dst)
        if p.parent.name == "~mods" and p.suffix.lower() in _PAK_EXTS:
            return str(p.parent / f"{position:03d}_{p.name}")
        return dst


class KCD2Layout(Layout):
    id = "kcd2"
    label = "Kingdom Come: Deliverance II"
    description = "Cada mod es una carpeta en mods/; el orden se escribe en mods/mod_order.txt."
    order_hint = ("El juego carga los mods en el orden de la lista (mods/mod_order.txt). Los mods "
                  "desactivados no se cargan.")
    supports_internal_conflicts = True
    MODS = "mods"

    @classmethod
    def detect(cls, game_dir, nexus_domain):
        return nexus_domain == "kingdomcomedeliverance2" or (game_dir / "Bin" / "Win64MasterMasterSteamPGO").is_dir()

    def map_files(self, staged, mod_name):
        files = staged_files(staged)
        if not files:
            raise LayoutError("El archivo del mod está vacío")
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
            raise LayoutError("No parece un mod de KCD2: no tiene mod.manifest ni carpeta Data con .pak. "
                              "Puedes instalarlo como «Archivos sueltos».")
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

    def internal_entries(self, path):
        if path.suffix.lower() != ".pak":
            return []
        try:
            with zipfile.ZipFile(path) as z:  # los .pak de KCD2 son zip sin comprimir
                return [n.lower() for n in z.namelist() if not n.endswith("/")]
        except (zipfile.BadZipFile, OSError):
            return []


def _safe_folder(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_").lower()
    return s or "mod"


LAYOUTS: dict[str, type[Layout]] = {c.id: c for c in (KCD2Layout, UnrealLayout, LooseLayout)}


def detect(game_dir: Path, nexus_domain: str | None) -> type[Layout]:
    for cls in (KCD2Layout, UnrealLayout, LooseLayout):
        if cls.detect(game_dir, nexus_domain):
            return cls
    return LooseLayout
