"""Lista de mods para llevarla a otro PC: qué mods, qué archivo exacto, en qué orden, cuáles activos y con qué
opciones de instalador. No lleva los archivos (se vuelven a bajar de Nexus), así que ocupa unos pocos KB.

Al importar, lo que ya está instalado se reutiliza, lo que falta se baja con la cola de instalación (con las
opciones FOMOD guardadas, sin asistente) y el orden se guarda en un perfil nuevo, sin tocar los que ya hay.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import VERSION
from .i18n import _
from .providers.base import CollectionMod

FORMAT = "crisol-modlist"


class ModListError(Exception):
    pass


def export(ctx) -> dict:
    st = ctx.state
    mods = []
    for m in st.ordered():
        e = {"name": m.name, "provider": m.provider, "version": m.version, "enabled": st.is_enabled(m.uid),
             "uid": m.uid}   # el uid solo vale en este PC (para --enable/--disable); al importar no se usa
        if m.provider == "nexus":
            e.update(domain=m.game_domain, mod_id=m.mod_id, file_id=m.file_id, file_title=m.file_title)
        elif m.provider == "github":
            e["loader"] = m.file_name.split(":", 1)[0]
        else:
            e["file_name"] = m.file_name
        if m.fomod_choice:
            e["fomod"] = {"name": m.fomod_name, "choice": m.fomod_choice}
        if m.me3_variant:
            e["me3_variant"] = m.me3_variant
        if m.note:
            e["note"] = m.note
        wins = sorted(t for t, u in st.profile.overrides.items() if u == m.uid)
        if wins:
            e["wins"] = wins   # archivos en conflicto que este mod gana por elección del usuario
        mods.append(e)
    g = ctx.game
    return {"format": FORMAT, "version": 1, "app": VERSION, "exported": int(time.time()),
            "game": {"key": g.key, "name": g.name, "nexus": st.nexus_domain, "layout": ctx.layout.id},
            "profile": st.active, "me3_opts": dict(getattr(st, "me3_opts", {}) or {}), "mods": mods}


def save(ctx, path: Path) -> int:
    data = export(ctx)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(data["mods"])


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ModListError(_("No se pudo leer la lista: {0}").format(e)) from e
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("mods"), list):
        raise ModListError(_("Ese archivo no es una lista de mods de Crisol"))
    return data


@dataclass
class Entry:
    data: dict
    status: str               # "installed" | "other-version" | "missing" | "manual"
    uid: str = ""             # el mod instalado que le corresponde (si lo hay)
    item: CollectionMod | None = None   # qué poner en la cola si hay que instalarlo


@dataclass
class ImportPlan:
    entries: list[Entry] = field(default_factory=list)
    other_game: str = ""      # la lista es de otro juego (nombre)


def plan(ctx, data: dict) -> ImportPlan:
    st = ctx.state
    out = ImportPlan()
    game = data.get("game") or {}
    if game.get("nexus") and st.nexus_domain and game["nexus"] != st.nexus_domain:
        out.other_game = game.get("name") or game["nexus"]
    for e in data["mods"]:
        prov = e.get("provider")
        fomod = (e.get("fomod") or {}).get("choice") or None
        if prov == "nexus" and e.get("mod_id"):
            exact = st.find("nexus", int(e["mod_id"]), int(e.get("file_id") or 0))
            other = None if exact else st.find("nexus", int(e["mod_id"]))
            item = CollectionMod(int(e["mod_id"]), int(e.get("file_id") or 0), e.get("name", "?"),
                                 e.get("file_title", ""), e.get("version", ""), 0, False, fomod_choice=fomod)
            if exact:
                out.entries.append(Entry(e, "installed", exact.uid, item))
            elif other:
                out.entries.append(Entry(e, "other-version", other.uid, item))
            else:
                out.entries.append(Entry(e, "missing", "", item))
        elif prov == "github" and e.get("loader"):
            rec = next((m for m in st.mods.values() if m.provider == "github"
                        and m.file_name.startswith(e["loader"] + ":")), None)
            item = CollectionMod(0, 0, e.get("name", "?"), e["loader"], e.get("version", ""), 0, False,
                                 kind="loader", loader=e["loader"])
            out.entries.append(Entry(e, "installed" if rec else "missing", rec.uid if rec else "", item))
        else:
            # Importado a mano o incluido en una colección: no se puede bajar; se busca por nombre.
            same = [m for m in st.mods.values() if m.provider == prov]
            fname = e.get("file_name") or ""
            rec = (next((m for m in same if fname and m.file_name == fname and m.name == e.get("name")), None)
                   or next((m for m in same if fname and m.file_name == fname), None)
                   or next((m for m in same if m.name == e.get("name")), None))
            out.entries.append(Entry(e, "installed" if rec else "manual", rec.uid if rec else ""))
    return out


def finish(ctx, data: dict, profile_name: str = "") -> tuple[str, list[str]]:
    """Crea un perfil con el orden y los activos de la lista (con lo que haya instalado ya). Devuelve
    (nombre del perfil, nombres que no están instalados)."""
    st = ctx.state
    p = plan(ctx, data)
    name = profile_name or _("{0} (importado)").format(data.get("profile") or _("Lista"))
    base, n = name, 2
    while name in st.profiles:
        name, n = f"{base} {n}", n + 1
    st.add_profile(name)
    prof = st.profiles[name]
    order, enabled, missing, remap = [], [], [], False
    for e in p.entries:
        if not e.uid or e.uid in order:
            if not e.uid:
                missing.append(e.data.get("name", "?"))
            continue
        order.append(e.uid)
        if e.data.get("enabled", True):
            enabled.append(e.uid)
        for t in e.data.get("wins") or []:
            prof.overrides[str(t).lower()] = e.uid
        rec = st.mods[e.uid]
        if e.data.get("note") and not rec.note:
            rec.note = e.data["note"]
        want = e.data.get("me3_variant")
        if want and want in rec.me3_variants and want != rec.me3_variant:
            rec.me3_variant, remap = want, True
    # Lo que ya tenías y no está en la lista va al final, desactivado.
    order += [u for u in st.profile.order if u not in order]
    prof.order, prof.enabled = order, enabled
    prof.overrides = {t: u for t, u in prof.overrides.items() if u in order}
    st.switch(name)
    if data.get("me3_opts") and hasattr(st, "me3_opts"):
        st.me3_opts.update(data["me3_opts"])
    st.dirty_deploy = True
    st.save()
    if remap:
        from . import manager
        manager.remap(ctx)
    return name, missing
