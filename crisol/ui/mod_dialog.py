"""Ficha de un mod: imagen, descripción, requisitos y archivos para descargar."""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .. import nxm_handler
from ..providers.base import FileInfo, ModInfo
from ..providers.nexus import Nexus, NxmLink
from .util import human_count, human_size, load_remote, run_async
from ..i18n import _

_CATEGORY = {"MAIN": _('Principal'), "OPTIONAL": _('Opcional'), "UPDATE": _('Actualización'), "MISCELLANEOUS": _('Varios'),
             "OLD_VERSION": _('Versión antigua'), "ARCHIVED": _('Archivado'), "REMOVED": _('Retirado')}


class ModDialog(Adw.Dialog):
    def __init__(self, page, info: ModInfo):
        super().__init__(title=info.name, content_width=760, content_height=760)
        self.page = page
        self.win = page.win
        self.ctl = page.ctl
        self.info = info
        self.domain = page.ctx.state.nexus_domain
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_start=24, margin_end=24,
                           margin_top=6, margin_bottom=24)
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, height_request=260, hexpand=True)
        frame = Gtk.Box(overflow=Gtk.Overflow.HIDDEN, css_classes=["mod-picture"], height_request=260)
        frame.append(pic)
        load_remote(pic, info.picture or info.thumbnail, 712, 260)
        self.box.append(frame)
        self.box.append(Gtk.Label(label=info.name, xalign=0, wrap=True, css_classes=["title-1"]))
        meta = Gtk.Box(spacing=8)
        for txt in (_('por {0}').format(info.author) if info.author else "", f"v{info.version}" if info.version else "",
                    f"{human_count(info.downloads)} descargas", f"{human_count(info.endorsements)} endorsements",
                    info.category):
            if txt:
                meta.append(Gtk.Label(label=txt, css_classes=["chip"]))
        self.box.append(meta)
        self.summary = Gtk.Label(label=info.summary, xalign=0, wrap=True)
        self.box.append(self.summary)
        web = Gtk.Button(halign=Gtk.Align.START, css_classes=["flat"])
        web.set_child(Adw.ButtonContent(icon_name="web-browser-symbolic", label=_('Abrir en Nexus Mods')))
        web.connect("clicked", lambda *_u: self._launch(f"https://www.nexusmods.com/{self.domain}/mods/{info.mod_id}"))
        self.box.append(web)

        self.how = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        self.box.append(self.how)
        self.req_group = Adw.PreferencesGroup(title=_('Requisitos'), visible=False)
        self.box.append(self.req_group)
        self.files_group = Adw.PreferencesGroup(title=_('Archivos'))
        self.files_spinner = Adw.Spinner(height_request=32)
        self.files_group.add(self.files_spinner)
        self.box.append(self.files_group)
        self.old_files = Adw.ExpanderRow(title=_('Versiones antiguas y archivadas'), visible=False)
        tv.set_content(Gtk.ScrolledWindow(child=self.box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_child(tv)
        self._explain()
        run_async(self._load, self._loaded, self._failed)

    def _explain(self) -> None:
        if not self.ctl.nexus.api_key:
            txt = (_('Para descargar hace falta tu API key de Nexus Mods (Preferencias → Nexus Mods).'))
        elif self.ctl.is_premium:
            txt = _('Cuenta Premium: «Instalar» descarga directamente.')
        else:
            txt = (_('Sin Premium, Nexus solo permite descargar desde su web: «Descargar» abre la página del archivo; pulsa allí «Slow download» y el navegador pasará el enlace a Crisol, que lo instala solo.'))
            if not nxm_handler.is_default():
                txt += _(' ⚠ Crisol aún no es la app de los enlaces nxm: actívalo en Preferencias.')
        ld = self.page.ctx.loader()
        if ld and ld.level == "required":
            txt += (_('\n\nEste juego carga los mods con {0}: ').format(ld.name) +
                    (_("ya lo tienes.") if ld.installed else _('aún no lo tienes.')))
        elif ld and ld.level == "none":
            txt += _('\n\nEste juego no necesita cargador de mods.')
        self.how.set_label(txt)

    def _load(self):
        nexus: Nexus = self.ctl.nexus
        return nexus.mod(self.domain, self.info.mod_id), nexus.files(self.domain, self.info.mod_id)

    def _failed(self, e):
        self.files_group.remove(self.files_spinner)
        self.files_group.add(Adw.ActionRow(title=_('No se pudieron cargar los archivos'),
                                           subtitle=GLib.markup_escape_text(str(e))))

    def _loaded(self, res):
        full, files = res
        self.info = full
        if full.summary:
            self.summary.set_label(full.summary)
        self._show_requirements(full)
        self.files_group.remove(self.files_spinner)
        current = [f for f in files if f.category not in ("OLD_VERSION", "ARCHIVED", "REMOVED")]
        old = [f for f in files if f.category in ("OLD_VERSION", "ARCHIVED", "REMOVED")]
        if not files:
            self.files_group.add(Adw.ActionRow(title=_('Este mod no tiene archivos disponibles')))
        for f in current:
            self.files_group.add(self._file_row(f))
        if old:
            self.old_files.set_visible(True)
            for f in old:
                self.old_files.add_row(self._file_row(f))
            self.files_group.add(self.old_files)

    def _loader_check(self, full: ModInfo) -> None:
        """¿Este mod pide un cargador? Se mira en sus requisitos y en su resumen."""
        from ..layouts import loader_in_text
        texts = [r.get("name", "") for r in full.requirements] + [full.summary]
        name = next((n for n in (loader_in_text(t) for t in texts) if n), None)
        if not name or loader_in_text(full.name):
            return  # el propio mod es el cargador
        st = self.page.ctx.state
        ld = self.page.ctx.loader()
        have = any(name.lower() in m.name.lower() for m in st.mods.values()) or \
            bool(ld and ld.installed and name.lower() in ld.name.lower())
        b = Gtk.Box(spacing=10, css_classes=["note-box"])
        b.append(Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if have else "dialog-warning-symbolic"))
        b.append(Gtk.Label(label=_('Este mod necesita el cargador {0}: ').format(name) + (
            _("ya lo tienes.") if have else _('no lo tienes. Instálalo primero (como un mod más) y ponlo arriba en la lista.')),
            xalign=0, wrap=True, hexpand=True))
        self.box.insert_child_after(b, self.how)

    def _show_requirements(self, full: ModInfo) -> None:
        self._loader_check(full)
        if not full.requirements:
            return
        st = self.page.ctx.state
        installed = {m.mod_id for m in st.mods.values() if m.provider == "nexus"}
        self.req_group.set_visible(True)
        for r in full.requirements:
            row = Adw.ActionRow(title=GLib.markup_escape_text(r.get("name") or "?"),
                                subtitle=GLib.markup_escape_text(r.get("notes") or ""))
            if r.get("external"):
                row.add_suffix(Gtk.Label(label=_('Externo'), css_classes=["chip", "warning"], valign=Gtk.Align.CENTER))
            elif r.get("mod_id") in installed:
                row.add_suffix(Gtk.Label(label=_('Instalado'), css_classes=["chip", "success"], valign=Gtk.Align.CENTER))
            else:
                row.add_suffix(Gtk.Label(label=_('Falta'), css_classes=["chip", "error"], valign=Gtk.Align.CENTER))
            if r.get("url"):
                b = Gtk.Button(icon_name="adw-external-link-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                               tooltip_text=_('Abrir'))
                b.connect("clicked", lambda *_a, u=r["url"]: self._launch(u))
                row.add_suffix(b)
            self.req_group.add(row)

    def _file_row(self, f: FileInfo) -> Gtk.Widget:
        st = self.page.ctx.state
        row = Adw.ActionRow(title=GLib.markup_escape_text(f.name or f.file_name),
                            subtitle=GLib.markup_escape_text(
                                " · ".join(x for x in (f"v{f.version}" if f.version else "", human_size(f.size),
                                                        _CATEGORY.get(f.category, f.category)) if x)))
        if f.description:
            row.set_tooltip_text(f.description[:600])
        if st.find("nexus", self.info.mod_id, f.file_id):
            row.add_suffix(Gtk.Label(label=_('Instalado'), css_classes=["chip", "success"], valign=Gtk.Align.CENTER))
        btn = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"]
                         if f.category == "MAIN" else ["pill"])
        if self.ctl.is_premium:
            btn.set_label(_('Instalar'))
            btn.connect("clicked", lambda *_u: self._install_direct(f))
        else:
            btn.set_label(_('Descargar'))
            btn.connect("clicked", lambda *_u: self._open_download(f))
        btn.set_sensitive(bool(self.ctl.nexus.api_key))
        row.add_suffix(btn)
        return row

    def _install_direct(self, f: FileInfo) -> None:
        self.win.install_nxm(self.page.game, NxmLink(self.domain, self.info.mod_id, f.file_id, None, None))
        self.close()

    def _open_download(self, f: FileInfo) -> None:
        # nmm=1 abre el diálogo de descarga «con gestor», que genera el enlace nxm://
        self._launch(Nexus.file_page(self.domain, self.info.mod_id, f.file_id) + "&nmm=1")
        self.win.wait_for_nxm(self.page.game, self.domain, self.info.mod_id, f.file_id, f.name or self.info.name)
        self.close()

    def _launch(self, url: str) -> None:
        Gtk.UriLauncher.new(url).launch(self.win, None, None)

