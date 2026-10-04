"""Estado por juego: mods instalados, perfiles y orden de carga (~/.local/share/crisol/games/<juego>.json)."""
from __future__ import annotations

import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import jsonio, paths

DEFAULT_PROFILE = "Predeterminado"


@dataclass
class ModRecord:
    uid: str                     # id local estable: se conserva al actualizar el mod
    name: str
    provider: str = "manual"     # "nexus" | "manual"
    game_domain: str = ""
    mod_id: int = 0
    file_id: int = 0
    file_name: str = ""
    version: str = ""
    author: str = ""
    thumbnail: str = ""
    archive: str = ""            # ruta del archivo descargado
    md5: str = ""
    size: int = 0
    installed: float = 0.0
    layout: str = ""
    files: list[list[str]] = field(default_factory=list)   # [[origen en staging, destino en el juego]]
    skipped: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)       # carpetas en mods/ (kcd2)
    packages: list[str] = field(default_factory=list)      # ME3: carpetas de paquete (en staging)
    natives: list[str] = field(default_factory=list)       # ME3: DLL nativas (en staging)
    savefile: str = ""                                     # ME3: partida aparte que pide el mod
    requirements: list[dict] = field(default_factory=list)
    latest_version: str = ""
    verified: str = ""           # "md5" | "size" | "" (cómo se comprobó la descarga)

    @property
    def update_available(self) -> bool:
        return bool(self.latest_version) and bool(self.version) and _vkey(self.latest_version) != _vkey(self.version)


def _vkey(v: str) -> str:
    return v.strip().lower().lstrip("v")


@dataclass
class Profile:
    order: list[str] = field(default_factory=list)     # uids, el primero se carga antes
    enabled: list[str] = field(default_factory=list)


class GameState:
    def __init__(self, game_key: str, safe_key: str):
        self.game_key = game_key
        self.safe_key = safe_key
        self.path = paths.GAMES_DIR / f"{safe_key}.json"
        raw = jsonio.load(self.path, {})
        self.layout: str | None = raw.get("layout")
        self.nexus_domain: str | None = raw.get("nexus_domain")
        self.mods: dict[str, ModRecord] = {}
        for uid, m in (raw.get("mods") or {}).items():
            known = {k: v for k, v in m.items() if k in ModRecord.__dataclass_fields__}
            self.mods[uid] = ModRecord(**known)
        self.profiles: dict[str, Profile] = {n: Profile(**p) for n, p in (raw.get("profiles") or {}).items()}
        if not self.profiles:
            self.profiles[DEFAULT_PROFILE] = Profile()
        self.active: str = raw.get("active") if raw.get("active") in self.profiles else next(iter(self.profiles))
        self.dirty_deploy: bool = bool(raw.get("dirty_deploy"))
        self._repair()

    def _repair(self) -> None:
        """Cada perfil contiene cada mod exactamente una vez."""
        for p in self.profiles.values():
            seen: set[str] = set()
            p.order = [u for u in p.order if u in self.mods and not (u in seen or seen.add(u))]
            p.order += [u for u in self.mods if u not in seen]
            p.enabled = [u for u in p.enabled if u in self.mods]

    def save(self) -> None:
        jsonio.save(self.path, {
            "game": self.game_key, "layout": self.layout, "nexus_domain": self.nexus_domain,
            "mods": {u: asdict(m) for u, m in self.mods.items()},
            "profiles": {n: asdict(p) for n, p in self.profiles.items()},
            "active": self.active, "dirty_deploy": self.dirty_deploy,
        })

    # ---------- mods ----------
    @property
    def profile(self) -> Profile:
        return self.profiles[self.active]

    def ordered(self) -> list[ModRecord]:
        return [self.mods[u] for u in self.profile.order]

    def enabled_ordered(self) -> list[ModRecord]:
        en = set(self.profile.enabled)
        return [self.mods[u] for u in self.profile.order if u in en]

    def staging(self, uid: str) -> Path:
        return paths.STAGING_DIR / self.safe_key / uid

    def new_uid(self) -> str:
        while True:
            uid = secrets.token_hex(4)
            if uid not in self.mods:
                return uid

    def find(self, provider: str, mod_id: int, file_id: int | None = None) -> ModRecord | None:
        for m in self.mods.values():
            if m.provider == provider and m.mod_id == mod_id and (file_id is None or m.file_id == file_id):
                return m
        return None

    def add(self, rec: ModRecord, enable: bool = True) -> None:
        new = rec.uid not in self.mods
        rec.installed = rec.installed or time.time()
        self.mods[rec.uid] = rec
        if new:
            for name, p in self.profiles.items():
                p.order.append(rec.uid)
                if enable and name == self.active:
                    p.enabled.append(rec.uid)
        self.dirty_deploy = True

    def remove(self, uid: str) -> None:
        self.mods.pop(uid, None)
        for p in self.profiles.values():
            p.order = [u for u in p.order if u != uid]
            p.enabled = [u for u in p.enabled if u != uid]
        shutil.rmtree(self.staging(uid), ignore_errors=True)
        self.dirty_deploy = True

    def set_enabled(self, uid: str, on: bool) -> None:
        en = self.profile.enabled
        if on and uid not in en:
            en.append(uid)
        elif not on and uid in en:
            en.remove(uid)
        self.dirty_deploy = True

    def is_enabled(self, uid: str) -> bool:
        return uid in self.profile.enabled

    def move(self, uid: str, new_index: int) -> None:
        o = self.profile.order
        o.remove(uid)
        o.insert(max(0, min(new_index, len(o))), uid)
        self.dirty_deploy = True

    # ---------- perfiles ----------
    def add_profile(self, name: str, copy_from: str | None = None) -> None:
        src = self.profiles.get(copy_from or "")
        self.profiles[name] = Profile(list(src.order), list(src.enabled)) if src else Profile(list(self.mods), [])
        self._repair()

    def rename_profile(self, old: str, new: str) -> None:
        self.profiles = {(new if n == old else n): p for n, p in self.profiles.items()}
        if self.active == old:
            self.active = new

    def delete_profile(self, name: str) -> None:
        if len(self.profiles) > 1:
            self.profiles.pop(name, None)
            if self.active == name:
                self.active = next(iter(self.profiles))
                self.dirty_deploy = True

    def switch(self, name: str) -> None:
        if name in self.profiles and name != self.active:
            self.active = name
            self.dirty_deploy = True
