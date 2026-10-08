"""Colecciones de Nexus completas: el manifiesto (collection.json) con el orden, las reglas y las opciones
FOMOD que eligió el autor.

El archivo de la colección (un .7z pequeño) se baja con la API key del usuario, también sin Premium. Trae:
- mods: de Nexus (mod y archivo exactos), «bundle» (archivos dentro del propio .7z) o externos
  («browse»/«direct»: una web donde descargarlos a mano);
- choices: las opciones FOMOD de cada mod, por paso/grupo/opción;
- modRules: «A después de B» / «A antes de B» (por md5, nombre de archivo o etiqueta).
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import APP_NAME, VERSION, paths
from .i18n import _
from .providers.base import ProviderError

log = logging.getLogger(__name__)


@dataclass
class Item:
    name: str
    kind: str                 # "nexus" | "bundle" | "browse" | "direct" | otro
    mod_id: int = 0
    file_id: int = 0
    md5: str = ""
    logical: str = ""         # nombre lógico del archivo (para las reglas)
    version: str = ""
    optional: bool = False
    choices: list | None = None   # opciones FOMOD (formato de la colección)
    url: str = ""
    instructions: str = ""
    tag: str = ""
    bundled: str = ""         # ruta dentro del .7z (kind == "bundle")
    keys: set = field(default_factory=set)


@dataclass
class Manifest:
    name: str
    items: list[Item]
    archive: Path
    rules: list = field(default_factory=list)


def download(nexus, domain: str, slug: str) -> Path:
    """Baja el .7z de la última revisión publicada (con la API key; vale sin Premium)."""
    if not nexus.api_key:
        raise ProviderError(_("Hace falta tu API key de Nexus Mods para leer la colección completa."))
    d = nexus._gql("query($s:String!,$d:String){collection(slug:$s,domainName:$d){"
                   "latestPublishedRevision{revisionNumber downloadLink}}}", {"s": slug, "d": domain})
    rev = ((d.get("collection") or {}).get("latestPublishedRevision")) or {}
    if not rev.get("downloadLink"):
        raise ProviderError(_("Nexus Mods no da el archivo de esta colección"))
    dest = paths.CACHE_DIR / "collections" / f"{slug}-{rev.get('revisionNumber', 0)}.7z"
    if dest.exists():
        return dest
    headers = {"APIKEY": nexus.api_key, "Application-Name": APP_NAME, "Application-Version": VERSION}
    try:
        r = nexus.session.get("https://api.nexusmods.com" + rev["downloadLink"], headers=headers, timeout=30)
        r.raise_for_status()
        uri = (r.json().get("download_links") or [{}])[0].get("URI")
        if not uri:
            raise ProviderError(_("Nexus Mods no da el archivo de esta colección"))
        data = nexus.session.get(uri, timeout=120)
        data.raise_for_status()
    except (requests.RequestException, ValueError) as e:
        raise ProviderError(_("No se pudo bajar la colección: {0}").format(e)) from e
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data.content)
    return dest


def parse(archive: Path) -> Manifest:
    r = subprocess.run(["bsdtar", "-xOf", str(archive), "collection.json"], capture_output=True)
    if r.returncode != 0:
        raise ProviderError(_("La colección no trae collection.json"))
    return parse_data(json.loads(r.stdout.decode("utf-8-sig", errors="replace")), archive)


def parse_data(data: dict, archive: Path) -> Manifest:
    items = []
    for m in data.get("mods") or []:
        s = m.get("source") or {}
        it = Item(name=m.get("name") or s.get("logicalFilename") or "?", kind=s.get("type") or "?",
                  mod_id=int(s.get("modId") or 0), file_id=int(s.get("fileId") or 0), md5=(s.get("md5") or "").lower(),
                  logical=s.get("logicalFilename") or "", version=m.get("version") or "",
                  optional=bool(m.get("optional")), url=s.get("url") or "",
                  instructions=m.get("instructions") or s.get("instructions") or "", tag=s.get("tag") or "",
                  choices=(m.get("choices") or {}).get("options") if (m.get("choices") or {}).get("type") == "fomod"
                  else None)
        if it.kind == "bundle":
            it.bundled = s.get("fileExpression") or it.name
        it.keys = {k for k in (it.md5, it.logical.lower(), it.tag, it.name.lower()) if k}
        items.append(it)
    return Manifest((data.get("info") or {}).get("name") or archive.stem, items, archive, data.get("modRules") or [])


def _ref_keys(ref: dict) -> set:
    keys = {(ref.get("fileMD5") or "").lower(), (ref.get("logicalFileName") or "").lower(), ref.get("tag") or ""}
    expr = (ref.get("fileExpression") or "").lower()
    if expr:
        keys.add(expr)
    return {k for k in keys if k}


def _find(items: list[Item], ref: dict) -> int | None:
    keys = _ref_keys(ref)
    for i, it in enumerate(items):
        if it.keys & keys:
            return i
    # «fileExpression» suele ser el nombre del archivo sin extensión: se compara por prefijo con el lógico.
    expr = (ref.get("fileExpression") or "").lower()
    for i, it in enumerate(items):
        if expr and it.logical and expr.startswith(it.logical.lower()):
            return i
    return None


def ordered(man: Manifest) -> list[Item]:
    """Mods en el orden de instalación: el de la colección, respetando sus reglas «después de/antes de»."""
    n = len(man.items)
    after: dict[int, set[int]] = {i: set() for i in range(n)}   # i va después de los de after[i]
    for rule in man.rules:
        a, b = _find(man.items, rule.get("source") or {}), _find(man.items, rule.get("reference") or {})
        if a is None or b is None or a == b:
            continue
        if rule.get("type") == "after":
            after[a].add(b)
        elif rule.get("type") == "before":
            after[b].add(a)
    out, done, visiting = [], set(), set()

    def visit(i: int) -> None:
        if i in done or i in visiting:      # un ciclo en las reglas: se rompe sin bloquear
            return
        visiting.add(i)
        for j in sorted(after[i]):
            visit(j)
        visiting.discard(i)
        done.add(i)
        out.append(man.items[i])
    for i in range(n):
        visit(i)
    return out


def extract_bundled(man: Manifest, item: Item, dest_dir: Path) -> Path:
    """Saca del .7z de la colección un archivo incluido («bundle») para instalarlo como un mod más."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["bsdtar", "-xf", str(man.archive), "-C", str(dest_dir), f"bundled/{item.bundled}"],
                       capture_output=True, text=True)
    path = dest_dir / "bundled" / item.bundled
    if r.returncode != 0 or not path.exists():
        raise ProviderError(_("La colección no trae el archivo incluido «{0}»").format(item.bundled))
    return path
