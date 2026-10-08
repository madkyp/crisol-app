"""Colecciones de Nexus: lista por juego y ficha con sus mods para instalarlos en orden."""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk, Pango

from ..providers.base import CollectionInfo
from .util import human_count, human_size, load_remote, run_async
from ..i18n import _


class CollectionCard(Gtk.Box):
    def __init__(self, info: CollectionInfo):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["mod-card"], width_request=230)
        self.info = info
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, height_request=130, hexpand=True)
        frame = Gtk.Box(overflow=Gtk.Overflow.HIDDEN, css_classes=["thumb"], height_request=130)
        frame.append(pic)
        load_remote(pic, info.image, 230, 130)
        self.append(frame)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, margin_start=12, margin_end=12,
                      margin_top=10, margin_bottom=12)
        col.append(Gtk.Label(label=info.name, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["mod-name"],
                             max_width_chars=1, hexpand=True, tooltip_text=info.name))
        col.append(Gtk.Label(label=_("{0} · {1} mods · {2}").format(info.author, info.mod_count, human_size(info.size)), xalign=0,
                             ellipsize=Pango.EllipsizeMode.END, css_classes=["mod-meta"], max_width_chars=1))
        col.append(Gtk.Label(label=info.summary, xalign=0, wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END,
                             css_classes=["mod-summary"], height_request=36, valign=Gtk.Align.START,
                             max_width_chars=1, width_chars=1))
        stats = Gtk.Box(spacing=12)
        for icon, val in (("folder-download-symbolic", info.downloads), ("starred-symbolic", info.endorsements)):
            b = Gtk.Box(spacing=4, css_classes=["mod-meta"])
            b.append(Gtk.Image.new_from_icon_name(icon))
            b.append(Gtk.Label(label=human_count(val)))
            stats.append(b)
        col.append(stats)
        self.append(col)


class CollectionDialog(Adw.Dialog):
    def __init__(self, page, info: CollectionInfo):
        super().__init__(title=info.name, content_width=760, content_height=760)
        self.page, self.info = page, info
        self.win = page.win
        self.domain = page.ctx.state.nexus_domain
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=24, margin_end=24,
                      margin_top=6, margin_bottom=24)
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, height_request=220, hexpand=True)
        frame = Gtk.Box(overflow=Gtk.Overflow.HIDDEN, css_classes=["mod-picture"], height_request=220)
        frame.append(pic)
        load_remote(pic, info.image, 712, 220)
        box.append(frame)
        box.append(Gtk.Label(label=info.name, xalign=0, wrap=True, css_classes=["title-1"]))
        meta = Gtk.Box(spacing=8)
        for t in (_('por {0}').format(info.author), _('revisión {0}').format(info.revision), _("{0} mods").format(info.mod_count), human_size(info.size),
                  _("{0} descargas").format(human_count(info.downloads))):
            meta.append(Gtk.Label(label=t, css_classes=["chip"]))
        box.append(meta)
        box.append(Gtk.Label(label=info.summary, xalign=0, wrap=True))
        web = Gtk.Button(halign=Gtk.Align.START, css_classes=["flat"])
        web.set_child(Adw.ButtonContent(icon_name="web-browser-symbolic", label=_('Abrir en Nexus Mods')))
        web.connect("clicked", lambda *_u: Gtk.UriLauncher.new(
            f"https://www.nexusmods.com/games/{self.domain}/collections/{info.slug}").launch(self.win, None, None))
        box.append(web)
        how = (_('Cuenta Premium: se descargan e instalan uno tras otro.') if page.ctl.is_premium else
               _('Sin Premium, Nexus pide pulsar «Slow download» en cada mod: Crisol abre la página de cada uno por turnos y pasa al siguiente en cuanto llega el enlace.'))
        self.how = Gtk.Label(label=how, xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        box.append(self.how)
        self._how_base = how
        self.group = Adw.PreferencesGroup(title=_('Mods de la colección'))
        self.spinner = Adw.Spinner(height_request=32)
        self.group.add(self.spinner)
        box.append(self.group)
        self.install = Gtk.Button(label=_('Instalar'), halign=Gtk.Align.END, css_classes=["pill", "suggested-action"],
                                  sensitive=False)
        self.install.connect("clicked", lambda *_u: self._install())
        box.append(self.install)
        tv.set_content(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_child(tv)
        self.checks: list[tuple[Gtk.CheckButton, object]] = []
        run_async(self._load, self._loaded, self._failed)

    def _load(self):
        """Lista de mods (rápida) y, si se puede, el manifiesto completo: orden del autor, sus reglas,
        sus opciones FOMOD, lo incluido en la colección y lo que hay que bajar a mano."""
        from .. import collection
        from ..providers.base import CollectionMod
        nexus = self.page.ctl.nexus
        mods = nexus.collection_mods(self.domain, self.info.slug)
        try:
            man = collection.parse(collection.download(nexus, self.domain, self.info.slug))
        except Exception as e:  # noqa: BLE001 — sin manifiesto se sigue con la lista básica
            return mods, None, str(e)
        sizes = {m.file_id: m for m in mods}
        out = []
        for it in collection.ordered(man):
            if it.kind == "nexus" and it.mod_id:
                base = sizes.get(it.file_id)
                out.append(CollectionMod(it.mod_id, it.file_id, it.name, base.file_name if base else it.logical,
                                         it.version, base.size if base else 0, it.optional,
                                         fomod_options=it.choices))
            elif it.kind == "bundle":
                out.append(CollectionMod(0, 0, it.name, it.bundled, it.version, 0, it.optional, kind="bundle",
                                         bundled=it.bundled, collection_archive=str(man.archive)))
            else:
                out.append(CollectionMod(0, 0, it.name, it.logical, it.version, 0, it.optional, kind="external",
                                         url=it.url, instructions=it.instructions))
        return out, man, ""

    def _failed(self, e):
        self.group.remove(self.spinner)
        self.group.add(Adw.ActionRow(title=_('No se pudo cargar la colección'), subtitle=GLib.markup_escape_text(str(e))))

    def _loaded(self, res):
        mods, man, err = res
        self.group.remove(self.spinner)
        if man:
            self.how.set_label(self._how_base + " " + _("Se instalan en el orden del autor (con sus reglas) y con las "
                                                        "opciones de instalación que eligió él."))
        else:
            self.how.set_label(self._how_base + " " + _("Se instalan en el orden de la colección; las opciones de los "
                                                        "instaladores las eliges tú.") + (f" ({err})" if err else ""))
        from ..layouts import me3_redundant
        st = self.page.ctx.state
        external = self.page.ctx.layout.external
        for m in mods:
            exact = st.find("nexus", m.mod_id, m.file_id)
            other = None if exact else st.find("nexus", m.mod_id)
            redundant = external and me3_redundant(m.name)
            row = Adw.ActionRow(title=GLib.markup_escape_text(m.name),
                                subtitle=GLib.markup_escape_text(" · ".join(x for x in (
                                    m.file_name, f"v{m.version}" if m.version else "",
                                    human_size(m.size) if m.size else "", _("opcional") if m.optional else "",
                                    _("con las opciones del autor") if m.fomod_options else "") if x)))
            if m.kind == "external":
                # Fuera de Nexus: no se puede bajar desde aquí. Se enseña dónde y cómo.
                row.set_subtitle(GLib.markup_escape_text(_("Descarga externa: instálalo a mano con «Importar "
                                                           "archivo…»") + (f" — {m.instructions}" if m.instructions else "")))
                row.set_subtitle_lines(3)
                if m.url:
                    b = Gtk.Button(icon_name="adw-external-link-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                   tooltip_text=m.url)
                    b.connect("clicked", lambda *_a, u=m.url: Gtk.UriLauncher.new(u).launch(self.win, None, None))
                    row.add_suffix(b)
                row.add_prefix(Gtk.Image.new_from_icon_name("web-browser-symbolic"))
                self.group.add(row)
                continue
            if m.kind == "bundle":
                exact = next((r for r in st.mods.values() if r.file_name == f"bundle:{m.bundled}"), None)
                other = None
                row.add_suffix(Gtk.Label(label=_("Incluido en la colección"), css_classes=["chip"],
                                         valign=Gtk.Align.CENTER))
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER,
                                    active=not m.optional and not exact and not other and not redundant)
            if redundant:
                row.add_suffix(Gtk.Label(label=_("No hace falta con ME3"), css_classes=["chip"], valign=Gtk.Align.CENTER,
                                         tooltip_text=_("Mod Engine 3 ya carga los mods y arranca el juego sin anticheat")))
            elif exact:
                check.set_sensitive(False)
                row.add_suffix(Gtk.Label(label=_('Instalado'), css_classes=["chip", "success"], valign=Gtk.Align.CENTER))
            elif other:
                row.add_suffix(Gtk.Label(label=_('Tienes v{0}').format(other.version), css_classes=["chip", "warning"],
                                         valign=Gtk.Align.CENTER, tooltip_text=_('Márcalo para cambiarlo por el de la colección')))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            check.connect("toggled", lambda *_u: self._count())
            self.checks.append((check, m))
            self.group.add(row)
        self._count()

    def _count(self):
        n = sum(1 for c, _u in self.checks if c.get_active())
        self.install.set_label(_('Instalar {0} mods').format(n) if n != 1 else _('Instalar 1 mod'))
        self.install.set_sensitive(n > 0 and bool(self.page.ctl.nexus.api_key))

    def _install(self):
        items = [m for c, m in self.checks if c.get_active() and m.kind != "external"]
        self.win.install_collection(self.page.game, self.domain, self.info.name, items)
        self.close()
