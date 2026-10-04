"""Instaladores FOMOD (fomod/ModuleConfig.xml): el formato de Nexus para mods con opciones.

Se interpreta el esquema 5.x: archivos obligatorios, pasos con visibilidad condicionada por banderas,
grupos (exactamente uno, como mucho uno, al menos uno, todos, cualquiera), tipos de opción (obligatoria,
recomendada, opcional, no disponible) e instalaciones condicionales. El resultado es una carpeta con los
archivos elegidos en su destino, que luego se coloca en el juego como cualquier otro mod.
"""
from __future__ import annotations

import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from .i18n import _


class FomodError(Exception):
    pass


@dataclass
class FileOp:
    source: str          # relativo a la raíz del mod, con «/»
    destination: str     # relativo a la raíz de instalación
    folder: bool
    priority: int = 0


@dataclass
class Plugin:
    name: str
    description: str = ""
    image: str = ""
    files: list[FileOp] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)
    type: str = "Optional"            # tipo por defecto
    patterns: list[tuple] = field(default_factory=list)  # [(condición, tipo)]

    def type_for(self, flags: dict[str, str], game_dir: Path | None) -> str:
        for cond, typ in self.patterns:
            if evaluate(cond, flags, game_dir):
                return typ
        return self.type


@dataclass
class Group:
    name: str
    type: str                          # SelectExactlyOne | SelectAtMostOne | SelectAtLeastOne | SelectAll | SelectAny
    plugins: list[Plugin]


@dataclass
class Step:
    name: str
    visible: tuple | None              # condición (o None = siempre)
    groups: list[Group]


@dataclass
class Module:
    name: str
    image: str
    required: list[FileOp]
    steps: list[Step]
    conditional: list[tuple]           # [(condición, [FileOp])]


# ---------------------------------------------------------------- lectura

def find_config(root: Path) -> Path | None:
    """fomod/ModuleConfig.xml (sin distinguir mayúsculas), en la raíz o un nivel por debajo."""
    for base in [root] + [p for p in sorted(root.iterdir()) if p.is_dir()]:
        for d in base.iterdir() if base.is_dir() else []:
            if d.is_dir() and d.name.lower() == "fomod":
                for f in d.iterdir():
                    if f.name.lower() == "moduleconfig.xml":
                        return f
    return None


def _read_xml(path: Path) -> ET.Element:
    raw = path.read_bytes()
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        pass
    # Declaración de codificación incorrecta (habitual): se decodifica según el BOM y se quita.
    for bom, enc in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"), (b"\xef\xbb\xbf", "utf-8-sig")):
        if raw.startswith(bom):
            text = raw[len(bom):].decode(enc.replace("-sig", ""), errors="replace")
            break
    else:
        text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise FomodError(_('El instalador FOMOD del mod está mal formado: {0}').format(e)) from e


def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _kids(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [c for c in el if _tag(c) == name] if el is not None else []


def _kid(el: ET.Element | None, name: str) -> ET.Element | None:
    k = _kids(el, name)
    return k[0] if k else None


def _norm(p: str) -> str:
    return "/".join(x for x in (p or "").replace("\\", "/").split("/") if x not in ("", "."))


def _files(el: ET.Element | None) -> list[FileOp]:
    out = []
    for c in el if el is not None else []:
        t = _tag(c)
        if t not in ("file", "folder"):
            continue
        src = _norm(c.get("source", ""))
        dst = c.get("destination")
        if dst is None:
            dst = src  # sin destino: misma ruta que el origen
        out.append(FileOp(src, _norm(dst), t == "folder", int(c.get("priority") or 0)))
    return out


def _cond(el: ET.Element | None) -> tuple | None:
    """Condición como árbol: ("and"|"or", [hijos]) | ("flag", nombre, valor) | ("file", ruta, estado) | ("true",)."""
    if el is None:
        return None
    op = (el.get("operator") or "And").lower()
    items = []
    for c in el:
        t = _tag(c)
        if t == "flagDependency":
            items.append(("flag", c.get("flag", ""), c.get("value", "")))
        elif t == "fileDependency":
            items.append(("file", _norm(c.get("file", "")), c.get("state", "Active")))
        elif t == "dependencies":
            items.append(_cond(c))
        else:  # gameDependency, fommDependency, foseDependency…: no se pueden comprobar; se dan por buenas
            items.append(("true",))
    return (op, items)


def parse(config: Path) -> Module:
    root = _read_xml(config)
    if _tag(root) != "config":
        raise FomodError(_('El instalador FOMOD del mod no tiene el formato esperado'))
    steps = []
    for st in _kids(_kid(root, "installSteps"), "installStep"):
        vis = _kid(st, "visible")
        visible = None
        if vis is not None:
            inner = _kid(vis, "dependencies")
            visible = _cond(inner if inner is not None else vis)
        groups = []
        for g in _kids(_kid(st, "optionalFileGroups"), "group"):
            plugins = []
            for p in _kids(_kid(g, "plugins"), "plugin"):
                td = _kid(p, "typeDescriptor")
                ptype, patterns = "Optional", []
                if _kid(td, "type") is not None:
                    ptype = _kid(td, "type").get("name", "Optional")
                elif _kid(td, "dependencyType") is not None:
                    dt = _kid(td, "dependencyType")
                    ptype = (_kid(dt, "defaultType").get("name", "Optional")
                             if _kid(dt, "defaultType") is not None else "Optional")
                    for pat in _kids(_kid(dt, "patterns"), "pattern"):
                        typ = _kid(pat, "type")
                        patterns.append((_cond(_kid(pat, "dependencies")), typ.get("name") if typ is not None
                                         else "Optional"))
                img = _kid(p, "image")
                plugins.append(Plugin(
                    name=p.get("name", ""), description=(_kid(p, "description").text or "").strip()
                    if _kid(p, "description") is not None else "",
                    image=_norm(img.get("path", "")) if img is not None else "",
                    files=_files(_kid(p, "files")),
                    flags={f.get("name", ""): (f.text or "") for f in _kids(_kid(p, "conditionFlags"), "flag")},
                    type=ptype, patterns=patterns))
            groups.append(Group(g.get("name", ""), g.get("type", "SelectAny"), plugins))
        steps.append(Step(st.get("name", ""), visible, groups))
    conditional = []
    for pat in _kids(_kid(_kid(root, "conditionalFileInstalls"), "patterns"), "pattern"):
        conditional.append((_cond(_kid(pat, "dependencies")), _files(_kid(pat, "files"))))
    img = _kid(root, "moduleImage")
    name_el = _kid(root, "moduleName")
    return Module(name=(name_el.text or "").strip() if name_el is not None else "",
                  image=_norm(img.get("path", "")) if img is not None else "",
                  required=_files(_kid(root, "requiredInstallFiles")), steps=steps, conditional=conditional)


# ---------------------------------------------------------------- lógica

def evaluate(cond: tuple | None, flags: dict[str, str], game_dir: Path | None = None) -> bool:
    if cond is None:
        return True
    kind = cond[0]
    if kind == "true":
        return True
    if kind == "flag":
        return flags.get(cond[1], "") == cond[2]
    if kind == "file":
        exists = bool(game_dir) and _ci_exists(game_dir, cond[1])
        # Active = el archivo está; Inactive = está pero desactivado (no aplica fuera de Bethesda);
        # Missing = no está.
        return exists if cond[2] in ("Active", "Inactive") else not exists
    results = [evaluate(c, flags, game_dir) for c in cond[1]]
    return all(results) if kind == "and" else any(results)


def _ci_exists(root: Path, rel: str) -> bool:
    cur = root
    for part in rel.split("/"):
        try:
            nxt = next((cur / n for n in os.listdir(cur) if n.lower() == part.lower()), None)
        except OSError:
            return False
        if nxt is None:
            return False
        cur = nxt
    return True


Choice = dict[tuple[int, int], set[int]]   # (paso, grupo) → índices de opciones elegidas


def flags_for(module: Module, choice: Choice, upto_step: int | None = None) -> dict[str, str]:
    """Banderas que dejan las opciones elegidas en los pasos visibles (hasta upto_step, sin incluirlo)."""
    flags: dict[str, str] = {}
    for si, step in enumerate(module.steps):
        if upto_step is not None and si >= upto_step:
            break
        if not evaluate(step.visible, flags):
            continue
        for gi, g in enumerate(step.groups):
            for pi in sorted(choice.get((si, gi), set())):
                flags.update(g.plugins[pi].flags)
    return flags


def visible_steps(module: Module, choice: Choice, game_dir: Path | None = None) -> list[int]:
    out = []
    for si, step in enumerate(module.steps):
        if evaluate(step.visible, flags_for(module, choice, si), game_dir):
            out.append(si)
    return out


def default_group(group: Group, flags: dict[str, str], game_dir: Path | None = None) -> set[int]:
    types = [p.type_for(flags, game_dir) for p in group.plugins]
    usable = [i for i, t in enumerate(types) if t != "NotUsable"]
    if group.type == "SelectAll":
        return set(range(len(group.plugins)))
    chosen = {i for i, t in enumerate(types) if t in ("Required", "Recommended")}
    if group.type in ("SelectExactlyOne", "SelectAtMostOne"):
        first = sorted(chosen)[:1] or (usable[:1] if group.type == "SelectExactlyOne" else [])
        return set(first)
    if group.type == "SelectAtLeastOne" and not chosen and usable:
        return {usable[0]}
    return chosen


def default_choice(module: Module, game_dir: Path | None = None) -> Choice:
    """Elección por defecto (obligatorias + recomendadas; la primera si hay que elegir una)."""
    choice: Choice = {}
    for si, step in enumerate(module.steps):
        flags = flags_for(module, choice, si)
        for gi, g in enumerate(step.groups):
            choice[(si, gi)] = default_group(g, flags, game_dir)
    return choice


def validate_group(group: Group, selected: set[int]) -> str | None:
    n = len(selected)
    if group.type == "SelectExactlyOne" and n != 1:
        return _('En «{0}» hay que elegir exactamente una opción.').format(group.name)
    if group.type == "SelectAtMostOne" and n > 1:
        return _('En «{0}» se puede elegir como mucho una opción.').format(group.name)
    if group.type == "SelectAtLeastOne" and n < 1:
        return _('En «{0}» hay que elegir al menos una opción.').format(group.name)
    return None


# ---------------------------------------------------------------- instalación

def operations(module: Module, choice: Choice, game_dir: Path | None = None) -> list[FileOp]:
    ops = list(module.required)
    steps = visible_steps(module, choice, game_dir)
    for si in steps:
        for gi, g in enumerate(module.steps[si].groups):
            for pi in sorted(choice.get((si, gi), set())):
                ops += g.plugins[pi].files
    flags = flags_for(module, {k: v for k, v in choice.items() if k[0] in steps})
    for cond, files in module.conditional:
        if evaluate(cond, flags, game_dir):
            ops += files
    return ops


def _ci_source(root: Path, rel: str) -> Path | None:
    cur = root
    for part in rel.split("/") if rel else []:
        try:
            nxt = next((cur / n for n in os.listdir(cur) if n.lower() == part.lower()), None)
        except OSError:
            return None
        if nxt is None:
            return None
        cur = nxt
    return cur


def build(module: Module, choice: Choice, mod_root: Path, out: Path, game_dir: Path | None = None) -> int:
    """Copia los archivos elegidos a out (orden por prioridad: la mayor gana). Devuelve cuántos."""
    ops = sorted(enumerate(operations(module, choice, game_dir)), key=lambda x: (x[1].priority, x[0]))
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for _u, op in ops:
        src = _ci_source(mod_root, op.source)
        if src is None:
            raise FomodError(_('El instalador FOMOD pide un archivo que no está en el mod: {0}').format(op.source))
        if op.folder or src.is_dir():
            for dirpath, _dirs, files in os.walk(src):
                rel_dir = Path(dirpath).relative_to(src)
                for f in files:
                    dst = out / op.destination / rel_dir / f
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(Path(dirpath) / f, dst)
                    count += 1
        else:
            dest = op.destination or src.name
            if dest.endswith("/") or (out / dest).is_dir():
                dest = f"{dest.rstrip('/')}/{src.name}"
            dst = out / dest
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            count += 1
    if not count:
        raise FomodError(_('Con las opciones elegidas el instalador no instala ningún archivo.'))
    return count


def choice_to_json(choice: Choice) -> list:
    return [[s, g, sorted(v)] for (s, g), v in choice.items()]


def choice_from_json(data: list) -> Choice:
    return {(s, g): set(v) for s, g, v in data or []}
