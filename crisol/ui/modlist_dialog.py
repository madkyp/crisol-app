"""Exportar la lista de mods de un juego a un archivo y traerla en otro PC."""
from __future__ import annotations

import time
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from .. import modlist
from ..i18n import _


def _json_filter() -> Gio.ListStore:
    f = Gtk.FileFilter(name=_("Lista de mods de Crisol (.json)"))
    f.add_pattern("*.json")
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(f)
    return filters


def export_dialog(page) -> None:
    ctx = page.ctx
    if not ctx.state.mods:
        page.win.toast(_("No hay mods instalados que exportar"))
        return
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in page.game.name).strip()
    dlg = Gtk.FileDialog(title=_("Exportar lista de mods"), filters=_json_filter(),
                         initial_name=f"{safe} - {ctx.state.active} - {time.strftime('%Y-%m-%d')}.json")
    dlg.set_initial_folder(Gio.File.new_for_path(str(Path.home())))

    def picked(d, res):
        try:
            gf = d.save_finish(res)
        except GLib.Error:
            return
        path = Path(gf.get_path())
        try:
            n = modlist.save(ctx, path)
        except OSError as e:
            page.win.error(_("No se pudo exportar"), e)
            return
        page.win.toast(_("Lista exportada: {0} mods en {1}").format(n, path.name), 6)
    dlg.save(page.win, None, picked)


def import_dialog(page) -> None:
    dlg = Gtk.FileDialog(title=_("Importar lista de mods"), filters=_json_filter())
    dlg.set_initial_folder(Gio.File.new_for_path(str(Path.home())))

    def picked(d, res):
        try:
            gf = d.open_finish(res)
        except GLib.Error:
            return
        try:
            data = modlist.load(Path(gf.get_path()))
        except modlist.ModListError as e:
            page.win.error(_("No se pudo importar"), e)
            return
        ImportDialog(page, data).present(page.win)
    dlg.open(page.win, None, picked)


STATUS = {
    "installed": (_("Instalado"), "success"),
    "other-version": (_("Tienes otra versión"), "warning"),
    "missing": (_("Falta"), "accent"),
    "manual": (_("A mano"), "warning"),
}


class ImportDialog(Adw.Dialog):
    def __init__(self, page, data: dict, on_import=None):
        super().__init__(title=_("Importar lista de mods"), content_width=720, content_height=720)
        self.page, self.data, self.win = page, data, page.win
        self.on_import = on_import
        self.plan = modlist.plan(page.ctx, data)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=24, margin_end=24,
                      margin_top=6, margin_bottom=24)
        game = data.get("game") or {}
        box.append(Gtk.Label(label=game.get("name") or page.game.name, xalign=0, wrap=True, css_classes=["title-1"]))
        meta = Gtk.Box(spacing=8)
        when = time.strftime("%d/%m/%Y", time.localtime(data.get("exported") or 0)) if data.get("exported") else ""
        for t in (_("perfil «{0}»").format(data.get("profile") or "?"), _("{0} mods").format(len(data["mods"])),
                  when, f"Crisol {data.get('app', '?')}"):
            if t:
                meta.append(Gtk.Label(label=t, css_classes=["chip"]))
        box.append(meta)
        if self.plan.other_game:
            box.append(Gtk.Label(label=_("Ojo: esta lista es de otro juego ({0}).").format(self.plan.other_game),
                                 xalign=0, wrap=True, css_classes=["error"]))
        box.append(Gtk.Label(label=_("Los mods que faltan se bajan de Nexus Mods con las mismas opciones de "
                                     "instalación. Al terminar se crea el perfil «{0}» con el orden y los mods "
                                     "activos de la lista; tus perfiles no se tocan.").format(self._profile_name()),
                             xalign=0, wrap=True, css_classes=["dim-label", "caption"]))
        group = Adw.PreferencesGroup(title=_("Mods de la lista"))
        self.checks: list[tuple[Gtk.CheckButton, modlist.Entry]] = []
        for e in self.plan.entries:
            d = e.data
            title = d.get("file_title") or d.get("file_name") or ""
            if title.startswith("bundle:"):
                title = _("incluido en una colección")
            ver = d.get("version") or ""
            sub = [x for x in (title, f"v{ver}" if ver and ver.lower().lstrip("v") not in title.lower() else "",
                               _("desactivado") if not d.get("enabled", True) else "",
                               _("con tus opciones de instalación") if d.get("fomod") else "") if x]
            row = Adw.ActionRow(title=GLib.markup_escape_text(d.get("name", "?")),
                                subtitle=GLib.markup_escape_text(" · ".join(sub)))
            label, css = STATUS[e.status]
            if e.status == "other-version":
                label = _("Tienes v{0}").format(page.ctx.state.mods[e.uid].version)
            chip = Gtk.Label(label=label, css_classes=["chip", css], valign=Gtk.Align.CENTER)
            if e.status == "manual":
                chip.set_tooltip_text(_("Se instaló a mano o venía en una colección: impórtalo con «Importar "
                                        "archivo…» y vuelve a importar la lista."))
            row.add_suffix(chip)
            if e.item and e.status in ("missing", "other-version"):
                check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=e.status == "missing")
                if e.status == "other-version":
                    check.set_tooltip_text(_("Márcalo para cambiarlo por la versión de la lista"))
                check.connect("toggled", lambda *_u: self._count())
                row.add_prefix(check)
                row.set_activatable_widget(check)
                self.checks.append((check, e))
            group.add(row)
        box.append(group)
        self.button = Gtk.Button(halign=Gtk.Align.END, css_classes=["pill", "suggested-action"])
        self.button.connect("clicked", lambda *_u: self._import())
        box.append(self.button)
        tv.set_content(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_child(tv)
        self._count()

    def _profile_name(self) -> str:
        return _("{0} (importado)").format(self.data.get("profile") or _("Lista"))

    def _items(self) -> list:
        return [e.item for c, e in self.checks if c.get_active()]

    def _count(self):
        n = len(self._items())
        if n:
            self.button.set_label(_("Instalar {0} y crear el perfil").format(n) if n != 1
                                  else _("Instalar 1 y crear el perfil"))
            nexus = any(i.kind == "nexus" for i in self._items())
            self.button.set_sensitive(bool(self.page.ctl.nexus.api_key) or not nexus)
        else:
            self.button.set_label(_("Crear el perfil"))
            self.button.set_sensitive(True)

    def _import(self):
        page, data = self.page, self.data
        items = self._items()
        self.close()
        if self.on_import:
            self.on_import()

        def finished(failed):
            name, missing = modlist.finish(page.ctx, data, self._profile_name())
            page.changed()
            body = _("Perfil «{0}» creado y activado. Pulsa «Aplicar» para llevarlo al juego.").format(name)
            if failed:
                body += "\n\n" + _("No se pudieron instalar:") + "\n" + "\n".join(f"• {n}: {m}" for n, m, _s in failed)
            if missing:
                body += "\n\n" + _("No están en este PC (quedan fuera del perfil):") + "\n" + \
                    "\n".join(f"• {n}" for n in missing)
            if failed or missing:
                self.win.error(_("Lista importada"), body)
            else:
                self.win.toast(body, 8)
        self.win.install_collection(page.game, page.ctx.state.nexus_domain or "", _("Lista importada"), items,
                                    on_done=finished)
