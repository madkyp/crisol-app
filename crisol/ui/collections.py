"""Colecciones de Nexus: lista por juego y ficha con sus mods para instalarlos en orden."""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk, Pango

from ..providers.base import CollectionInfo
from .util import human_count, human_size, load_remote, run_async


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
        col.append(Gtk.Label(label=f"{info.author} · {info.mod_count} mods · {human_size(info.size)}", xalign=0,
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
        for t in (f"por {info.author}", f"revisión {info.revision}", f"{info.mod_count} mods", human_size(info.size),
                  f"{human_count(info.downloads)} descargas"):
            meta.append(Gtk.Label(label=t, css_classes=["chip"]))
        box.append(meta)
        box.append(Gtk.Label(label=info.summary, xalign=0, wrap=True))
        web = Gtk.Button(halign=Gtk.Align.START, css_classes=["flat"])
        web.set_child(Adw.ButtonContent(icon_name="web-browser-symbolic", label="Abrir en Nexus Mods"))
        web.connect("clicked", lambda *_: Gtk.UriLauncher.new(
            f"https://www.nexusmods.com/games/{self.domain}/collections/{info.slug}").launch(self.win, None, None))
        box.append(web)
        how = ("Cuenta Premium: se descargan e instalan uno tras otro." if page.ctl.is_premium else
               "Sin Premium, Nexus pide pulsar «Slow download» en cada mod: Crisol abre la página de cada uno por "
               "turnos y pasa al siguiente en cuanto llega el enlace.")
        box.append(Gtk.Label(label=how + " Se instalan en el orden de la colección. Las opciones de los "
                             "instaladores FOMOD las eliges tú (no se copian las del autor).",
                             xalign=0, wrap=True, css_classes=["dim-label", "caption"]))
        self.group = Adw.PreferencesGroup(title="Mods de la colección")
        self.spinner = Adw.Spinner(height_request=32)
        self.group.add(self.spinner)
        box.append(self.group)
        self.install = Gtk.Button(label="Instalar", halign=Gtk.Align.END, css_classes=["pill", "suggested-action"],
                                  sensitive=False)
        self.install.connect("clicked", lambda *_: self._install())
        box.append(self.install)
        tv.set_content(Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.set_child(tv)
        self.checks: list[tuple[Gtk.CheckButton, object]] = []
        run_async(page.ctl.nexus.collection_mods, self._loaded, self._failed, self.domain, info.slug)

    def _failed(self, e):
        self.group.remove(self.spinner)
        self.group.add(Adw.ActionRow(title="No se pudo cargar la colección", subtitle=GLib.markup_escape_text(str(e))))

    def _loaded(self, mods):
        self.group.remove(self.spinner)
        st = self.page.ctx.state
        for m in mods:
            exact = st.find("nexus", m.mod_id, m.file_id)
            other = None if exact else st.find("nexus", m.mod_id)
            row = Adw.ActionRow(title=GLib.markup_escape_text(m.name),
                                subtitle=GLib.markup_escape_text(" · ".join(x for x in (
                                    m.file_name, f"v{m.version}" if m.version else "", human_size(m.size),
                                    "opcional" if m.optional else "") if x)))
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=not m.optional and not exact and not other)
            if exact:
                check.set_sensitive(False)
                row.add_suffix(Gtk.Label(label="Instalado", css_classes=["chip", "success"], valign=Gtk.Align.CENTER))
            elif other:
                row.add_suffix(Gtk.Label(label=f"Tienes v{other.version}", css_classes=["chip", "warning"],
                                         valign=Gtk.Align.CENTER, tooltip_text="Márcalo para cambiarlo por el de la colección"))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            check.connect("toggled", lambda *_: self._count())
            self.checks.append((check, m))
            self.group.add(row)
        self._count()

    def _count(self):
        n = sum(1 for c, _ in self.checks if c.get_active())
        self.install.set_label(f"Instalar {n} mods" if n != 1 else "Instalar 1 mod")
        self.install.set_sensitive(n > 0 and bool(self.page.ctl.nexus.api_key))

    def _install(self):
        items = [m for c, m in self.checks if c.get_active()]
        self.win.install_collection(self.page.game, self.domain, self.info.name, items)
        self.close()
