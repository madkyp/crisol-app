"""Nexus Mods: API v2 (GraphQL, sin clave) para buscar y API v1 (REST, con clave) para descargar.

Sin Premium, la descarga solo se puede hacer con la clave temporal (key/expires) de un enlace nxm://
que genera el botón «Mod Manager Download» de la web.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass

import requests

from .. import APP_NAME, VERSION, jsonio, paths
from .base import (AuthError, FileInfo, ModInfo, ModProvider, PremiumRequired, ProviderError, RateLimited,
                   SearchPage)

log = logging.getLogger(__name__)

V1 = "https://api.nexusmods.com/v1"
V2 = "https://api.nexusmods.com/v2/graphql"
WEB = "https://www.nexusmods.com"
SEARCH_TTL = 3600  # s: las búsquedas se guardan una hora (la API v2 a veces tarda mucho)
GAMES_JSON = "https://data.nexusmods.com/file/nexus-data/games.json"

# Juegos cuyo nombre en Steam coincide con otro distinto en Nexus.
STEAM_DOMAINS = {
    "1501750": "lordsofthefallen2023",  # Lords of the Fallen (2023), no el de 2014
}

_MOD_FIELDS = """modId uid name version summary downloads endorsements category author adultContent
                 pictureUrl thumbnailUrl updatedAt uploader { name } game { id domainName }"""


@dataclass
class NxmLink:
    domain: str
    mod_id: int
    file_id: int
    key: str | None
    expires: int | None


def parse_nxm(url: str) -> NxmLink:
    """nxm://<dominio>/mods/<mod>/files/<archivo>?key=…&expires=…&user_id=…"""
    u = urllib.parse.urlparse(url)
    parts = [p for p in u.path.split("/") if p]
    if u.scheme.lower() != "nxm" or len(parts) != 4 or parts[0] != "mods" or parts[2] != "files":
        raise ProviderError(f"Enlace nxm no reconocido: {url}")
    q = urllib.parse.parse_qs(u.query)
    exp = q.get("expires", [None])[0]
    return NxmLink(domain=u.netloc.lower(), mod_id=int(parts[1]), file_id=int(parts[3]),
                   key=q.get("key", [None])[0], expires=int(exp) if exp else None)


class Nexus(ModProvider):
    id = "nexus"
    name = "Nexus Mods"

    def __init__(self, api_key: str | None = None, show_adult: bool = False):
        self.api_key = api_key
        self.show_adult = show_adult
        self.rate: dict[str, str] = {}
        self._game_ids: dict[str, int] = {}
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update({"Application-Name": APP_NAME, "Application-Version": VERSION,
                                     "User-Agent": f"{APP_NAME}/{VERSION}"})

    # ---------- transporte ----------
    def _gql(self, query: str, variables: dict | None = None) -> dict:
        """Consulta de solo lectura: si Nexus se queda colgado (pasa a ratos), se reintenta."""
        last: Exception | None = None
        for attempt, timeout in enumerate((10, 12, 20)):
            try:
                r = self.session.post(V2, json={"query": query, "variables": variables or {}}, timeout=timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                log.info("Nexus v2 sin respuesta (intento %d): %s", attempt + 1, e)
                continue
            except requests.RequestException as e:
                raise ProviderError(f"No se pudo conectar con Nexus Mods: {e}") from e
            if r.status_code == 429:
                raise RateLimited("Nexus Mods ha limitado las peticiones. Espera unos minutos.")
            if r.status_code >= 500:
                last = ProviderError(f"Nexus Mods no responde (error {r.status_code}). Prueba más tarde.")
                time.sleep(1 + attempt)
                continue
            try:
                body = r.json()
            except ValueError as e:
                raise ProviderError(f"Respuesta no válida de Nexus Mods (HTTP {r.status_code})") from e
            if body.get("errors"):
                raise ProviderError("Nexus Mods: " + "; ".join(e.get("message", "?") for e in body["errors"]))
            return body.get("data") or {}
        if isinstance(last, ProviderError):
            raise last
        raise ProviderError("Nexus Mods no responde ahora mismo (se ha intentado 3 veces). Prueba en un rato.")

    def _v1(self, path: str, key_required: bool = True) -> object:
        if key_required and not self.api_key:
            raise AuthError("Falta la API key de Nexus Mods (Preferencias → Nexus Mods).")
        headers = {"APIKEY": self.api_key} if self.api_key else {}
        try:
            r = self.session.get(V1 + path, headers=headers, timeout=20)
        except requests.RequestException as e:
            raise ProviderError(f"No se pudo conectar con Nexus Mods: {e}") from e
        with self._lock:
            self.rate = {k.lower(): v for k, v in r.headers.items() if k.lower().startswith("x-rl-")}
        if r.status_code == 401:
            raise AuthError("La API key de Nexus Mods no es válida o ha caducado.")
        if r.status_code == 429:
            raise RateLimited("Has llegado al límite de peticiones de Nexus Mods "
                              f"(quedan {self.rate.get('x-rl-hourly-remaining', '0')} esta hora). "
                              "Espera a que se renueve.")
        if r.status_code == 403:
            msg = _message(r)
            if "premium" in msg.lower():
                raise PremiumRequired("Esta descarga necesita Nexus Premium o un enlace «Mod Manager Download» "
                                      "de la web.")
            raise ProviderError(f"Nexus Mods ha denegado la petición: {msg}")
        if r.status_code == 404:
            raise ProviderError(f"No encontrado en Nexus Mods: {_message(r)}")
        if r.status_code == 410:
            raise ProviderError("El enlace de descarga ha caducado. Vuelve a pulsar «Mod Manager Download».")
        if r.status_code >= 400:
            raise ProviderError(f"Nexus Mods devolvió error {r.status_code}: {_message(r)}")
        return r.json()

    # ---------- cuenta ----------
    def validate_key(self) -> dict:
        """{name, is_premium, …}. Lanza AuthError si la clave no vale."""
        return self._v1("/users/validate.json")  # type: ignore[return-value]

    # ---------- juegos ----------
    def games(self) -> list[dict]:
        """Tabla pública de juegos de Nexus (data.nexusmods.com), en caché una semana."""
        cache = paths.CACHE_DIR / "nexus-games.json"
        try:
            fresh = time.time() - cache.stat().st_mtime < 7 * 86400
        except OSError:
            fresh = False
        if not fresh:
            try:
                r = self.session.get(GAMES_JSON, timeout=60)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list) and data:
                    jsonio.save(cache, data)
            except (requests.RequestException, ValueError) as e:
                log.warning("no se pudo actualizar la lista de juegos de Nexus: %s", e)
        data = jsonio.load(cache, [])
        for g in data:
            self._game_ids.setdefault(g["domain_name"], int(g["id"]))
        return data

    def find_game(self, name: str, steam_appid: str | None = None) -> tuple[str, int, str] | None:
        """Empareja un juego por nombre: (dominio, id, nombre en Nexus) o None."""
        games = self.games()
        by_domain = {g["domain_name"]: g for g in games}
        if steam_appid and STEAM_DOMAINS.get(steam_appid) in by_domain:
            g = by_domain[STEAM_DOMAINS[steam_appid]]
            return g["domain_name"], int(g["id"]), g["name"]
        by_norm: dict[str, dict] = {}
        for g in games:
            by_norm.setdefault(_norm(g["name"]), g)
        # Nombre completo; luego sin paréntesis ("(beta)") ni subtítulo tras «:».
        base = re.sub(r"\s*[(\[].*?[)\]]", "", name).strip()
        for cand in (name, base, base.split(":")[0]):
            g = by_norm.get(_norm(cand))
            if g:
                return g["domain_name"], int(g["id"]), g["name"]
        return None

    def game_id(self, domain: str) -> int:
        if domain not in self._game_ids:
            data = self._gql("query($d:String){game(domainName:$d){id}}", {"d": domain})
            g = data.get("game")
            if not g:
                raise ProviderError(f"Nexus Mods no conoce el juego «{domain}»")
            self._game_ids[domain] = int(g["id"])
        return self._game_ids[domain]

    # ---------- mods ----------
    def search(self, game_domain, query, sort="relevance", offset=0, count=20) -> SearchPage:
        key = hashlib.sha1(json.dumps([game_domain, query.strip().lower(), sort, offset, count,
                                       self.show_adult]).encode()).hexdigest()
        cache = paths.CACHE_DIR / "search" / f"{key}.json"
        try:
            if time.time() - cache.stat().st_mtime < SEARCH_TTL:
                raw = jsonio.load(cache, None)
                if raw:
                    return SearchPage([ModInfo(**m) for m in raw["mods"]], raw["total"])
        except OSError:
            pass
        page = self._search(game_domain, query, sort, offset, count)
        jsonio.save(cache, {"mods": [asdict(m) for m in page.mods], "total": page.total})
        return page

    def _search(self, game_domain, query, sort, offset, count) -> SearchPage:
        f: dict = {"gameDomainName": [{"value": game_domain, "op": "EQUALS"}]}
        if not self.show_adult:  # como la web sin sesión: el contenido adulto, oculto
            f["adultContent"] = [{"value": False, "op": "EQUALS"}]
        if len(query.strip()) >= 2:  # Nexus rechaza comodines de menos de 2 letras
            f["name"] = [{"value": query.strip(), "op": "WILDCARD"}]
        if sort == "relevance" and not query.strip():
            sort = "downloads"
        s = [{sort: {"direction": "DESC"}}]
        data = self._gql(f"query($f:ModsFilter,$s:[ModsSort!],$o:Int,$c:Int){{mods(filter:$f,sort:$s,offset:$o,"
                         f"count:$c){{totalCount nodes{{{_MOD_FIELDS}}}}}}}",
                         {"f": f, "s": s, "o": offset, "c": count})
        m = data.get("mods") or {}
        return SearchPage([self._mod(n) for n in m.get("nodes") or []], int(m.get("totalCount") or 0))

    def find_loaders(self, game_domain: str, terms: tuple) -> list[ModInfo]:
        """Mods de cargadores publicados para el juego (p. ej. «BepInEx», «UE4SS»), el más descargado de cada uno."""
        out: list[ModInfo] = []
        for term in terms:
            try:
                page = self.search(game_domain, term, "downloads", 0, 5)
            except ProviderError as e:
                log.info("no se pudo buscar el cargador %s: %s", term, e)
                continue
            hits = [m for m in page.mods if term.lower() in m.name.lower()]
            if hits and all(h.mod_id != o.mod_id for h in hits[:1] for o in out):
                out.append(hits[0])
        return out

    def mod(self, game_domain, mod_id) -> ModInfo:
        data = self._gql(f"""query($m:ID!,$g:ID!){{mod(modId:$m,gameId:$g){{ {_MOD_FIELDS}
            modRequirements {{ nexusRequirements {{ nodes {{ modId modName gameId url externalRequirement notes }} }} }}
        }}}}""", {"m": mod_id, "g": self.game_id(game_domain)})
        n = data.get("mod")
        if not n:
            raise ProviderError(f"El mod {mod_id} no existe o no está disponible")
        info = self._mod(n)
        reqs = (((n.get("modRequirements") or {}).get("nexusRequirements") or {}).get("nodes")) or []
        info.requirements = [{"mod_id": int(r["modId"]) if r.get("modId") else None, "name": r.get("modName") or "",
                              "url": r.get("url") or "", "external": bool(r.get("externalRequirement")),
                              "notes": r.get("notes") or ""} for r in reqs]
        return info

    def files(self, game_domain, mod_id) -> list[FileInfo]:
        data = self._gql("query($m:ID!,$g:ID!){modFiles(modId:$m,gameId:$g){fileId name version category "
                         "sizeInBytes uri description date}}", {"m": mod_id, "g": self.game_id(game_domain)})
        out = [FileInfo(file_id=int(f["fileId"]), name=f.get("name") or "", version=f.get("version") or "",
                        category=f.get("category") or "", size=int(f.get("sizeInBytes") or 0),
                        file_name=f.get("uri") or "", description=f.get("description") or "",
                        date=int(f.get("date") or 0)) for f in data.get("modFiles") or []]
        rank = {"MAIN": 0, "UPDATE": 1, "OPTIONAL": 2, "MISCELLANEOUS": 3}  # el resto (antiguos, retirados) al final
        out.sort(key=lambda f: (rank.get(f.category, 9), -f.date))
        return out

    def latest_versions(self, game_domain, mod_ids) -> dict[int, str]:
        if not mod_ids:
            return {}
        data = self._gql("query($ids:[CompositeDomainWithIdInput!]!){legacyModsByDomain(ids:$ids,count:100)"
                         "{nodes{modId version}}}",
                         {"ids": [{"gameDomain": game_domain, "modId": m} for m in mod_ids]})
        return {int(n["modId"]): n.get("version") or "" for n in (data.get("legacyModsByDomain") or {}).get("nodes") or []}

    def latest_file_ids(self, game_domain: str, mod_id: int) -> list[FileInfo]:
        return [f for f in self.files(game_domain, mod_id) if f.category in ("MAIN", "OPTIONAL", "MISCELLANEOUS")]

    # ---------- descargas ----------
    def download_urls(self, game_domain: str, mod_id: int, file_id: int,
                      key: str | None = None, expires: int | None = None) -> list[str]:
        path = f"/games/{game_domain}/mods/{mod_id}/files/{file_id}/download_link.json"
        if key and expires:
            path += "?" + urllib.parse.urlencode({"key": key, "expires": expires})
        links = self._v1(path)
        return [x["URI"] for x in links if isinstance(x, dict) and x.get("URI")]  # type: ignore[union-attr]

    def md5_search(self, game_domain: str, md5: str) -> list[dict]:
        try:
            res = self._v1(f"/games/{game_domain}/mods/md5_search/{md5}.json")
        except ProviderError as e:
            if "No encontrado" in str(e):
                return []
            raise
        return res if isinstance(res, list) else []

    @staticmethod
    def file_page(game_domain: str, mod_id: int, file_id: int | None = None) -> str:
        url = f"{WEB}/{game_domain}/mods/{mod_id}?tab=files"
        return url + (f"&file_id={file_id}" if file_id else "")

    def _mod(self, n: dict) -> ModInfo:
        g = n.get("game") or {}
        if g.get("domainName") and g.get("id"):
            self._game_ids[g["domainName"]] = int(g["id"])
        return ModInfo(provider=self.id, mod_id=int(n["modId"]), name=n.get("name") or "",
                       author=n.get("author") or (n.get("uploader") or {}).get("name", ""),
                       version=n.get("version") or "", summary=n.get("summary") or "",
                       downloads=int(n.get("downloads") or 0), endorsements=int(n.get("endorsements") or 0),
                       category=n.get("category") or "", thumbnail=n.get("thumbnailUrl") or "",
                       picture=n.get("pictureUrl") or "", updated=n.get("updatedAt") or "",
                       adult=bool(n.get("adultContent")), uid=str(n.get("uid") or ""),
                       page_url=f"{WEB}/{g.get('domainName', '')}/mods/{n['modId']}")


def _message(r: requests.Response) -> str:
    try:
        return str(r.json().get("message") or r.text[:200])
    except ValueError:
        return r.text[:200]


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())
