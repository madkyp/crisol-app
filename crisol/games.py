"""Detección de juegos instalados: Steam (appmanifest) y Umbral (config.json)."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import jsonio, paths

log = logging.getLogger(__name__)

# Herramientas de Steam que aparecen como "juegos" en los manifiestos.
_STEAM_TOOLS = re.compile(r"^(Proton|Steam Linux Runtime|Steamworks Common|SteamVR)", re.I)
# Tipos de Umbral sin archivos de juego modificables (clientes, emuladores, ScummVM).
_UMBRAL_SKIP_KINDS = {"battlenet", "scummvm", "emulator", "vm"}


@dataclass
class Game:
    key: str              # "steam:1771300" / "umbral:<id>": id estable dentro de Crisol
    source: str           # "steam" | "umbral"
    source_id: str
    name: str
    install_dir: Path
    cover: Path | None = None   # carátula vertical
    hero: Path | None = None    # imagen ancha de cabecera
    icon: Path | None = None    # icono pequeño
    logo: Path | None = None
    prefix: Path | None = None  # prefijo de Wine/Proton (drive_c está dentro), si lo hay

    @property
    def safe_key(self) -> str:
        """Nombre de carpeta para staging/backups."""
        return re.sub(r"[^A-Za-z0-9._-]", "_", self.key)


def _parse_vdf(text: str) -> dict:
    """Parser mínimo de KeyValues de Valve (texto): suficiente para libraryfolders.vdf y appmanifest."""
    tokens = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    stack: list[dict] = [{}]
    key: str | None = None
    for s, brace in tokens:
        if brace == "{":
            new: dict = {}
            stack[-1][key or ""] = new
            stack.append(new)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = s
        else:
            stack[-1][key] = s.replace("\\\\", "\\")
            key = None
    return stack[0]


def steam_root() -> Path | None:
    for p in paths.STEAM_ROOTS:
        if (p / "steamapps").is_dir():
            return p.resolve()
    return None


def steam_libraries(root: Path) -> list[Path]:
    libs = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        data = _parse_vdf(vdf.read_text(errors="replace"))
    except OSError:
        return libs
    for entry in (data.get("libraryfolders") or {}).values():
        if isinstance(entry, dict) and entry.get("path"):
            p = Path(entry["path"]).resolve()
            if p not in libs and (p / "steamapps").is_dir():
                libs.append(p)
    return libs


def _steam_art(root: Path, appid: str) -> dict[str, Path | None]:
    base = root / "appcache" / "librarycache" / appid
    art: dict[str, Path | None] = {"cover": None, "hero": None, "icon": None, "logo": None}
    if not base.is_dir():
        return art
    # Steam guarda unas imágenes en la raíz y otras en subcarpetas con hash.
    files = [p for p in base.rglob("*") if p.is_file()]
    by_name = {p.name: p for p in files}
    art["cover"] = by_name.get("library_600x900.jpg")
    art["hero"] = by_name.get("library_hero.jpg") or by_name.get("library_header.jpg") or by_name.get("header.jpg")
    art["logo"] = by_name.get("logo.png")
    # El icono es el .jpg con nombre de hash (40 hex) en la raíz.
    icons = [p for p in base.glob("*.jpg") if re.fullmatch(r"[0-9a-f]{40}\.jpg", p.name)]
    art["icon"] = icons[0] if icons else None
    return art


def scan_steam() -> list[Game]:
    root = steam_root()
    if not root:
        return []
    games: list[Game] = []
    for lib in steam_libraries(root):
        for acf in sorted((lib / "steamapps").glob("appmanifest_*.acf")):
            try:
                st = _parse_vdf(acf.read_text(errors="replace")).get("AppState", {})
            except OSError:
                continue
            appid, name, idir = st.get("appid"), st.get("name", ""), st.get("installdir")
            if not appid or not idir or _STEAM_TOOLS.match(name):
                continue
            d = lib / "steamapps" / "common" / idir
            if not d.is_dir():
                continue
            pfx = lib / "steamapps" / "compatdata" / appid / "pfx"
            games.append(Game(key=f"steam:{appid}", source="steam", source_id=appid, name=name,
                              install_dir=d, prefix=pfx if pfx.is_dir() else None, **_steam_art(root, appid)))
    return games


def _umbral_install_dir(g: dict) -> Path | None:
    """Carpeta del juego: la del .exe. En instalaciones de Blizzard, la raíz del producto (_classic_beta_…)."""
    exe = g.get("exe") or ""
    if not exe:
        return None
    p = Path(exe)
    d = p if p.is_dir() else p.parent
    return d if d.is_dir() else None


def scan_umbral() -> list[Game]:
    cfg = jsonio.load(paths.UMBRAL_CONFIG, None)
    if not isinstance(cfg, dict):
        return []
    games: list[Game] = []
    prefixes = {p.get("id"): p.get("path") for p in cfg.get("prefixes", []) if isinstance(p, dict)}
    for g in cfg.get("games", []):
        if g.get("kind") in _UMBRAL_SKIP_KINDS or g.get("hidden"):
            continue
        d = _umbral_install_dir(g)
        if not d:
            continue
        cover = Path(g["cover"]) if g.get("cover") and Path(g["cover"]).is_file() else None
        icon = Path(g["icon"]) if g.get("icon") and Path(g["icon"]).is_file() else None
        pfx = Path(prefixes[g["prefix_id"]]) if prefixes.get(g.get("prefix_id")) else None
        games.append(Game(key=f"umbral:{g['id']}", source="umbral", source_id=g["id"], name=g.get("name") or g["id"],
                          install_dir=d, cover=cover, icon=icon,
                          prefix=pfx if pfx and (pfx / "drive_c").is_dir() else None))
    return games


STEAM_CDN_COVER = "https://shared.steamstatic.com/store_item_assets/steam/apps/{appid}/library_600x900.jpg"


def fetch_missing_covers(games: list[Game]) -> None:
    """Portada vertical oficial de la CDN de Steam para los juegos que no la tienen en la caché de Steam
    (se baja una vez a ~/.cache/crisol/covers; si Steam no la tiene, se recuerda y no se reintenta)."""
    import requests
    d = paths.CACHE_DIR / "covers"
    for g in games:
        if g.source != "steam" or g.cover:
            continue
        f, miss = d / f"{g.source_id}.jpg", d / f"{g.source_id}.none"
        if not f.exists() and not miss.exists():
            d.mkdir(parents=True, exist_ok=True)
            try:
                r = requests.get(STEAM_CDN_COVER.format(appid=g.source_id), timeout=15)
                if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                    f.write_bytes(r.content)
                elif r.status_code == 404:
                    miss.touch()
            except requests.RequestException as e:
                log.info("sin portada de Steam para %s: %s", g.name, e)
        if f.exists():
            g.cover = f


def scan_all() -> list[Game]:
    out = scan_steam() + scan_umbral()
    out.sort(key=lambda g: g.name.lower())
    log.info("juegos detectados: %d", len(out))
    return out
