"""Asistente de instalación FOMOD: pasos, grupos y opciones con su descripción e imagen."""
from __future__ import annotations

import threading
from pathlib import Path

from gi.repository import Adw, GLib, Gtk

from .. import fomod
from .util import local_texture
from ..i18n import _

_HINT = {"SelectExactlyOne": _('Elige una'), "SelectAtMostOne": _('Elige una o ninguna'),
         "SelectAtLeastOne": _('Elige al menos una'), "SelectAll": _('Se instalan todas'), "SelectAny": _('Elige las que quieras')}
_TYPE = {"Required": _('Obligatoria'), "Recommended": _('Recomendada'), "NotUsable": _('No disponible'),
         "CouldBeUsable": _('Puede funcionar')}


def make_chooser(win, game_dir: Path):
    """Selector para manager.install_*: se llama desde el hilo de instalación y espera al asistente."""
    def chooser(module, mod_root, previous):
        done = threading.Event()
        result: dict = {}

        def show():
            FomodDialog(module, mod_root, previous, game_dir,
                        lambda choice: (result.update(choice=choice), done.set())).present(win)
            return False
        GLib.idle_add(show)
        done.wait()
        return result.get("choice")
    return chooser


class FomodDialog(Adw.Dialog):
    def __init__(self, module: fomod.Module, mod_root: Path, previous, game_dir: Path, on_done):
        super().__init__(title=_('Instalar {0}').format(module.name) if module.name else _('Instalador del mod'),
                         content_width=980, content_height=680, can_close=True)
        self.module, self.root, self.game_dir, self.on_done = module, mod_root, game_dir, on_done
        self.choice: fomod.Choice = previous or fomod.default_choice(module, game_dir)
        self._finished = False
        self.connect("closed", lambda *_u: self._finish(None))

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False)
        self.back = Gtk.Button(label=_('Atrás'))
        self.back.connect("clicked", lambda *_u: self._go(-1))
        cancel = Gtk.Button(label=_('Cancelar'))
        cancel.connect("clicked", lambda *_u: self.close())
        self.next = Gtk.Button(css_classes=["suggested-action"])
        self.next.connect("clicked", lambda *_u: self._go(+1))
        hb.pack_start(cancel)
        hb.pack_end(self.next)
        hb.pack_end(self.back)
        tv.add_top_bar(hb)

        paned = Gtk.Paned(position=560, shrink_start_child=False, shrink_end_child=False)
        self.groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=20, margin_end=12,
                                  margin_top=12, margin_bottom=20)
        self.step_title = Gtk.Label(xalign=0, css_classes=["title-2"])
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left.append(self.step_title)
        left.append(self.groups_box)
        self.step_title.set_margin_start(20)
        self.step_title.set_margin_top(12)
        paned.set_start_child(Gtk.ScrolledWindow(child=left, hscrollbar_policy=Gtk.PolicyType.NEVER))
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=12, margin_end=20,
                       margin_top=12, margin_bottom=20)
        self.pic_box = Gtk.Box(overflow=Gtk.Overflow.HIDDEN, css_classes=["mod-picture"], halign=Gtk.Align.START)
        side.append(self.pic_box)
        self.desc_title = Gtk.Label(xalign=0, wrap=True, css_classes=["heading"])
        self.desc = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["body"], valign=Gtk.Align.START)
        side.append(self.desc_title)
        side.append(self.desc)
        paned.set_end_child(Gtk.ScrolledWindow(child=side, hscrollbar_policy=Gtk.PolicyType.NEVER))
        tv.set_content(paned)
        self.set_child(tv)
        self.visible = fomod.visible_steps(module, self.choice, game_dir)
        self.pos = 0
        if not self.visible:  # sin pasos: solo archivos obligatorios
            self.step_title.set_label(_('Este instalador no tiene opciones'))
            self.next.set_label(_('Instalar'))
            self.back.set_sensitive(False)
        else:
            self._render()
        if not self.visible:
            self._show_info(module.name, "", module.image)

    # ---------- navegación ----------
    def _go(self, delta: int) -> None:
        if not self.visible:
            self._finish(self.choice)
            return
        if delta > 0:
            si = self.visible[self.pos]
            for gi, g in enumerate(self.module.steps[si].groups):
                err = fomod.validate_group(g, self.choice.get((si, gi), set()))
                if err:
                    self.desc_title.set_label(_('Falta elegir'))
                    self.desc.set_label(err)
                    return
        # Las elecciones de este paso pueden mostrar u ocultar los siguientes.
        self.visible = fomod.visible_steps(self.module, self.choice, self.game_dir)
        self.pos += delta
        if self.pos >= len(self.visible):
            self._finish(self.choice)
            return
        self.pos = max(0, self.pos)
        self._render()

    def _finish(self, choice) -> None:
        if self._finished:
            return
        self._finished = True
        self.on_done(choice)
        if choice is not None:
            self.close()

    # ---------- pintado ----------
    def _render(self) -> None:
        si = self.visible[self.pos]
        step = self.module.steps[si]
        total = len(self.visible)
        self.step_title.set_label(f"{step.name or 'Opciones'}  ·  paso {self.pos + 1} de {total}")
        self.back.set_sensitive(self.pos > 0)
        last = self.pos == total - 1
        self.next.set_label(_('Instalar') if last else _('Siguiente'))
        child = self.groups_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.groups_box.remove(child)
            child = nxt
        flags = fomod.flags_for(self.module, self.choice, si)
        for gi, g in enumerate(step.groups):
            self.groups_box.append(self._group(si, gi, g, flags))
        # Panel derecho: la primera opción elegida del paso (o la primera del paso).
        picked = [(g.plugins[pi]) for gi, g in enumerate(step.groups)
                  for pi in sorted(self.choice.get((si, gi), set()))] or \
            [p for g in step.groups for p in g.plugins][:1]
        if picked:
            self._show_info(picked[0].name, picked[0].description, picked[0].image)

    def _group(self, si: int, gi: int, g: fomod.Group, flags) -> Gtk.Widget:
        grp = Adw.PreferencesGroup(title=GLib.markup_escape_text(g.name or _('Opciones')),
                                   description=_HINT.get(g.type, ""))
        key = (si, gi)
        sel = self.choice.setdefault(key, set())
        radio = g.type == "SelectExactlyOne"
        first: Gtk.CheckButton | None = None
        buttons: list[Gtk.CheckButton] = []
        for pi, p in enumerate(g.plugins):
            typ = p.type_for(flags, self.game_dir)
            row = Adw.ActionRow(title=GLib.markup_escape_text(p.name), activatable=True)
            if typ in _TYPE:
                row.set_subtitle(_TYPE[typ])
            btn = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=pi in sel)
            if radio:
                if first is None:
                    first = btn
                else:
                    btn.set_group(first)
            locked = g.type == "SelectAll" or typ in ("Required", "NotUsable")
            if g.type == "SelectAll" or typ == "Required":
                btn.set_active(True)
                sel.add(pi)
            if typ == "NotUsable":
                btn.set_active(False)
                sel.discard(pi)
            btn.set_sensitive(not locked)
            row.set_sensitive(typ != "NotUsable")
            row.add_prefix(btn)
            row.set_activatable_widget(btn)
            btn.connect("toggled", self._toggled, key, pi, g, buttons)
            ctrl = Gtk.EventControllerMotion()
            ctrl.connect("enter", lambda *_a, p=p: self._show_info(p.name, p.description, p.image))
            row.add_controller(ctrl)
            buttons.append(btn)
            grp.add(row)
        return grp

    def _toggled(self, btn: Gtk.CheckButton, key, pi: int, g: fomod.Group, buttons) -> None:
        sel = self.choice.setdefault(key, set())
        if btn.get_active():
            if g.type in ("SelectExactlyOne", "SelectAtMostOne"):
                sel.clear()
                for i, other in enumerate(buttons):
                    if i != pi and other.get_active():
                        other.set_active(False)
            sel.add(pi)
            p = g.plugins[pi]
            self._show_info(p.name, p.description, p.image)
        else:
            sel.discard(pi)

    def _show_info(self, title: str, desc: str, image: str) -> None:
        self.desc_title.set_label(title or "")
        self.desc.set_label(desc or "")
        child = self.pic_box.get_first_child()
        if child:
            self.pic_box.remove(child)
        src = fomod._ci_source(self.root, image) if image else None
        tex = local_texture(src, 360, 0) if src and src.is_file() else None
        if tex:
            self.pic_box.append(Gtk.Picture(paintable=tex, can_shrink=False))
