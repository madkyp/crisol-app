"""¿Hay una versión nueva de Crisol en GitHub? (releases de madkyp/crisol-app con el paquete adjunto)."""
from __future__ import annotations

import logging
import re
import time

import requests

from . import USER_AGENT, VERSION, jsonio, paths

log = logging.getLogger(__name__)
REPO = "madkyp/crisol-app"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = 24 * 3600


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def latest(force: bool = False) -> dict | None:
    """{version, url (página), asset (url del .pkg.tar.zst), notes} si hay una versión más nueva; si no, None.
    Como mucho una consulta al día (salvo force)."""
    cache = paths.CACHE_DIR / "selfupdate.json"
    data = jsonio.load(cache, {})
    if force or time.time() - data.get("checked", 0) > CHECK_EVERY:
        try:
            r = requests.get(API, timeout=15, headers={"User-Agent": USER_AGENT,
                                                      "Accept": "application/vnd.github+json"})
            if r.status_code == 404:
                data = {"checked": time.time()}  # aún no hay ninguna versión publicada
            else:
                r.raise_for_status()
                rel = r.json()
                asset = next((a["browser_download_url"] for a in rel.get("assets", [])
                              if a.get("name", "").endswith(".pkg.tar.zst")), "")
                data = {"checked": time.time(), "version": rel.get("tag_name", "").lstrip("v"),
                        "url": rel.get("html_url", ""), "asset": asset, "notes": (rel.get("body") or "")[:2000]}
            jsonio.save(cache, data)
        except (requests.RequestException, ValueError) as e:
            log.info("no se pudo comprobar si hay versión nueva: %s", e)
            return None
    v = data.get("version")
    if v and _vtuple(v) > _vtuple(VERSION):
        return data
    return None


def download(asset: str) -> str:
    """Descarga el paquete a la caché y devuelve su ruta."""
    dest = paths.CACHE_DIR / asset.rsplit("/", 1)[-1]
    with requests.get(asset, stream=True, timeout=60, headers={"User-Agent": USER_AGENT}) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return str(dest)
