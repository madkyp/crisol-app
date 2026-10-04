"""Detalle de un juego: cabecera, buscador de mods y mods instalados con su orden de carga."""
from __future__ import annotations

import logging
import os
import pwd
import re
import subprocess
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .. import manager, paths
from ..deploy import Conflict, Plan
from ..games import Game
from ..layouts import LAYOUTS
from ..providers.base import SORTS, ModInfo, SearchPage
from ..store import ModRecord
from .fomod_dialog import make_chooser
from .util import human_count, human_size, load_remote, local_texture, placeholder, run_async
from ..i18n import _

log = logging.getLogger(__name__)
PAGE_SIZE = 24


class GamePage(Adw.NavigationPage):
    def __init__(self, win, game: Game):
        super().__init__(title=game.name, tag=f"game-{game.key}")
        self.win = win
        self.ctl = win.ctl
        self.game = game
        self.ctx = manager.context(game)
        self._offset = 0
        self._query_id = 0
        self._plan: Plan | None = None
        self._missing: dict[str, list[dict]] = {}

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_title=False)
        menu = Gio.Menu()
        menu.append(_('Ajustes del juego…'), "game.settings")
        menu.append(_('Abrir carpeta del juego'), "game.open-dir")
        menu.append(_('Abrir carpeta de descargas'), "game.open-downloads")
        hb.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text=_('Más opciones')))
        tv.add_top_bar(hb)
        group = Gio.SimpleActionGroup()
        for name, cb in (("settings", self.show_settings), ("open-dir", lambda: _open(game.install_dir)),
                         ("open-downloads", lambda: _open(Path.home() / ".local/share/crisol/downloads"))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, cb=cb: cb())
            group.add_action(a)
        self.insert_action_group("game", group)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=28, margin_end=28,
                       margin_top=8, margin_bottom=24)
        body.append(self._build_hero())
        self.loader_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.append(self.loader_box)

        self.stack = Adw.ViewStack(vexpand=True)
        self.stack.add_titled_with_icon(self._build_search(), "search", _('Buscar mods'), "system-search-symbolic")
        self.installed_page = self.stack.add_titled_with_icon(self._build_installed(), "installed", _('Instalados'),
                                                              "view-list-bullet-symbolic")
        self.stack.add_titled_with_icon(self._build_collections(), "collections", _('Colecciones'),
                                        "view-grid-symbolic")
        self.stack.connect("notify::visible-child-name", self._tab_changed)
        self._collections_loaded = False
        switcher = Adw.InlineViewSwitcher(stack=self.stack, halign=Gtk.Align.START)
        switcher.add_css_class("round")
        body.append(switcher)
        body.append(self.stack)
        self.scroll = Gtk.ScrolledWindow(child=body, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        tv.set_content(self.scroll)
        self.set_child(tv)

        # Si el tipo de juego ha cambiado (p. ej. Elden Ring pasa a ME3), se recolocan los mods ya instalados.
        lay = self.ctx.layout.id
        if any(m.layout != lay for m in self.ctx.state.mods.values()):
            failed = manager.remap(self.ctx)
            if failed:
                GLib.idle_add(lambda: (self.win.error(
                    _('Mods que no encajan en este juego'),
                    _('Con el tipo «{0}» estos mods no se pueden colocar: ').format(self.ctx.layout.label)
                    + ", ".join(failed) + _('.\n\nSi alguno se descargó mal, usa ⋮ → «Reinstalar desde la descarga»; si no es para este cargador, desinstálalo.')), False)[1])
        self.refresh_installed()
        if self.ctx.state.mods:
            self.stack.set_visible_child_name("installed")
        self.search(reset=True)

    # ================================================================ cabecera
    def _build_hero(self) -> Gtk.Widget:
        g = self.game
        hero = Gtk.Box(css_classes=["game-hero"], overflow=Gtk.Overflow.HIDDEN)
        if g.hero and g.hero.is_file():
            # Imagen ancha del juego de fondo, oscurecida para que el texto se lea.
            css = Gtk.CssProvider()
            url = GLib.filename_to_uri(str(g.hero))
            cls = "hero-" + "".join(c if c.isalnum() else "-" for c in g.key)
            css.load_from_string(f".game-hero.{cls} {{ background-image: linear-gradient(90deg, rgba(16,18,23,0.95) 0%, "
                                 "rgba(16,18,23,0.80) 50%, rgba(16,18,23,0.55) 100%), "
                                 f"url('{url}'); }}")
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)
            hero.add_css_class(cls)
        row = Gtk.Box(spacing=26, hexpand=True)
        icon_tex = local_texture(g.cover, 150, 225) or local_texture(g.icon, 150, 225)
        icon_box = Gtk.Box(width_request=150, height_request=225, css_classes=["hero-cover"],
                           overflow=Gtk.Overflow.HIDDEN, valign=Gtk.Align.START, halign=Gtk.Align.START,
                           hexpand=False)
        if icon_tex:
            icon_box.append(Gtk.Picture(paintable=icon_tex, content_fit=Gtk.ContentFit.COVER))
        else:
            ph = placeholder(size=72)
            ph.set_hexpand(True)
            icon_box.append(ph)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, hexpand=True, valign=Gtk.Align.FILL)
        title = Gtk.Label(label=g.name, xalign=0, wrap=True, css_classes=["hero-title"])
        info.append(title)
        chips = Gtk.Box(spacing=6)
        chips.append(Gtk.Label(label="Steam" if g.source == "steam" else "Umbral", css_classes=["chip", g.source]))
        self.domain_chip = Gtk.Label(css_classes=["chip"])
        chips.append(self.domain_chip)
        self.layout_chip = Gtk.Label(css_classes=["chip"])
        chips.append(self.layout_chip)
        self.loader_chip = Gtk.Label(css_classes=["chip"])
        chips.append(self.loader_chip)
        info.append(chips)
        path = Gtk.Label(label=_tilde(str(g.install_dir)), xalign=0,
                         ellipsize=Pango.EllipsizeMode.MIDDLE, css_classes=["hero-sub", "caption"])
        info.append(path)
        self.hero_status = Gtk.Label(xalign=0, css_classes=["hero-sub"], wrap=True)
        info.append(self.hero_status)
        spacer = Gtk.Box(vexpand=True)
        info.append(spacer)
        actions = Gtk.Box(spacing=10)
        self.apply_btn = Gtk.Button(label=_('Aplicar mods'), css_classes=["pill", "suggested-action"])
        self.apply_btn.connect("clicked", lambda *_u: self.apply())
        self.restore_btn = Gtk.Button(label=_('Restaurar juego sin mods'), css_classes=["pill"])
        self.restore_btn.connect("clicked", lambda *_u: self.confirm_restore())
        actions.append(self.apply_btn)
        actions.append(self.restore_btn)
        self.play_btn = Gtk.Button(css_classes=["pill"], visible=False)
        self.play_btn.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic", label=_('Jugar con mods')))
        self.play_btn.connect("clicked", lambda *_u: self.play())
        actions.append(self.play_btn)
        info.append(actions)
        row.append(icon_box)
        row.append(info)
        hero.append(row)
        self._update_hero()
        return hero

    def _update_hero(self) -> None:
        st = self.ctx.state
        dom = st.nexus_domain
        self.domain_chip.set_label(_('Nexus: {0}').format(dom) if dom else _('Sin juego de Nexus'))
        self.domain_chip.set_css_classes(["chip", "accent" if dom else "warning"])
        lay = self.ctx.layout
        self.layout_chip.set_label(lay.label + ("" if st.layout else " (auto)"))
        deployed = self.ctx.is_applied()
        n_on, n = len(st.profile.enabled), len(st.mods)
        if not n:
            txt = _('Aún no hay mods instalados.')
        elif st.dirty_deploy:
            txt = _('Perfil «{0}»: {1} de {2} mods activos · hay cambios sin aplicar al juego.').format(st.active, n_on, n)
        elif deployed:
            txt = _('Perfil «{0}» aplicado: {1} de {2} mods activos en el juego.').format(st.active, n_on, n)
        else:
            txt = _('Perfil «{0}»: {1} de {2} mods activos · el juego está sin mods.').format(st.active, n_on, n)
        self.hero_status.set_label(txt)
        self.apply_btn.set_sensitive(bool(n) and (st.dirty_deploy or not deployed))
        self.restore_btn.set_sensitive(deployed)
        external = lay.external
        self.restore_btn.set_label(_('Quitar perfil de ME3') if external else _('Restaurar juego sin mods'))
        self.apply_btn.set_label(_('Guardar perfil de ME3') if external else _('Aplicar mods'))
        self.play_btn.set_visible(external)
        self.play_btn.set_sensitive(deployed and not st.dirty_deploy)
        ld = self.ctx.loader()
        if ld is None:
            self.loader_chip.set_visible(False)
        else:
            self.loader_chip.set_visible(True)
            if ld.level == "none":
                self.loader_chip.set_label(_('No necesita cargador de mods'))
                self.loader_chip.set_css_classes(["chip"])
            elif ld.installed:
                self.loader_chip.set_label(_('Cargador: {0} ✓').format(ld.name))
                self.loader_chip.set_css_classes(["chip", "success"])
            elif ld.level == "required":
                self.loader_chip.set_label(_('Necesita cargador: {0}').format(ld.name))
                self.loader_chip.set_css_classes(["chip", "error"])
            else:
                self.loader_chip.set_label(_('Cargador según el mod: {0}').format(ld.name))
                self.loader_chip.set_css_classes(["chip", "warning"])

    # ================================================================ buscador
    def _build_search(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=12)
        bar = Gtk.Box(spacing=10)
        self.query = Gtk.SearchEntry(placeholder_text=_('Buscar mods en Nexus Mods…'), hexpand=True)
        self.query.connect("search-changed", lambda *_u: self.search(reset=True))
        self.sort_keys = list(SORTS)
        self.sort = Gtk.DropDown.new_from_strings([SORTS[k] for k in self.sort_keys])
        self.sort.set_selected(1)
        self.sort.connect("notify::selected", lambda *_u: self.search(reset=True))
        bar.append(self.query)
        bar.append(self.sort)
        box.append(bar)
        self.results_info = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"])
        box.append(self.results_info)
        self.results = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=14, row_spacing=14,
                                   max_children_per_line=8, min_children_per_line=2, homogeneous=True,
                                   css_classes=["mod-grid"], valign=Gtk.Align.START,
                                   activate_on_single_click=True)
        self.results.connect("child-activated", lambda _f, c: self.open_mod(c.get_child().info))
        self.search_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.search_stack.add_named(self.results, "results")
        self.search_stack.add_named(Adw.Spinner(halign=Gtk.Align.CENTER, width_request=40, height_request=40,
                                                margin_top=40), "loading")
        self.search_status = Adw.StatusPage(icon_name="system-search-symbolic")
        self.search_status.add_css_class("compact")
        self.search_stack.add_named(self.search_status, "status")
        box.append(self.search_stack)
        self.more = Gtk.Button(label=_('Cargar más'), halign=Gtk.Align.CENTER, css_classes=["pill"], visible=False)
        self.more.connect("clicked", lambda *_u: self.search(reset=False))
        box.append(self.more)
        return box

    def search(self, reset: bool) -> None:
        domain = self.ctx.state.nexus_domain
        if not domain:
            self._search_message("dialog-question-symbolic", _('Juego sin enlazar con Nexus Mods'),
                                 _('No se ha encontrado este juego en Nexus Mods. Enlázalo a mano en ⋯ → Ajustes del juego.'))
            return
        if reset:
            self._offset = 0
            self.results.remove_all()
            self.search_stack.set_visible_child_name("loading")
        self._query_id += 1
        qid = self._query_id
        query, sort = self.query.get_text(), self.sort_keys[self.sort.get_selected()]
        offset = self._offset

        def done(page: SearchPage):
            log.debug("búsqueda %d: %d resultados (%d en total)", qid, len(page.mods), page.total)
            if qid != self._query_id:
                return  # llegó tarde: ya hay otra búsqueda
            for m in page.mods:
                self.results.append(ModCard(m))
            self._offset = offset + len(page.mods)
            self.more.set_visible(self._offset < page.total)
            self.results_info.set_label(_("{0} mods").format(human_count(page.total)) + (_(' para «{0}»').format(query) if query else ""))
            if self._offset == 0:
                self._search_message("system-search-symbolic", _('Sin resultados'), _('Prueba con otras palabras.'))
            else:
                self.search_stack.set_visible_child_name("results")

        def fail(e):
            if qid == self._query_id:
                self._search_message("network-error-symbolic", _('No se pudo buscar'), str(e))
        # Pequeña espera para no lanzar una petición por cada tecla.
        GLib.timeout_add(250 if reset and query else 0, lambda: (
            qid == self._query_id and run_async(self.ctl.nexus.search, done, fail, domain, query, sort, offset,
                                                PAGE_SIZE), False)[1])

    def _search_message(self, icon, title, desc) -> None:
        self.search_status.set_icon_name(icon)
        self.search_status.set_title(title)
        self.search_status.set_description(GLib.markup_escape_text(desc))
        self.search_stack.set_visible_child_name("status")
        self.more.set_visible(False)

    # ================================================================ colecciones
    def _build_collections(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=12)
        box.append(Gtk.Label(label=_('Packs de mods preparados por la comunidad. Se instalan en el orden de la colección.'),
                             xalign=0, wrap=True, css_classes=["dim-label", "caption"]))
        self.col_grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=14, row_spacing=14,
                                    max_children_per_line=8, min_children_per_line=2, homogeneous=True,
                                    css_classes=["mod-grid"], valign=Gtk.Align.START, activate_on_single_click=True)
        self.col_grid.connect("child-activated", lambda _f, c: self.open_collection(c.get_child().info))
        self.col_stack = Gtk.Stack()
        self.col_stack.add_named(Adw.Spinner(halign=Gtk.Align.CENTER, width_request=40, height_request=40,
                                             margin_top=40), "loading")
        self.col_stack.add_named(self.col_grid, "grid")
        self.col_status = Adw.StatusPage(icon_name="view-grid-symbolic", title=_('Sin colecciones'))
        self.col_status.add_css_class("compact")
        self.col_stack.add_named(self.col_status, "status")
        box.append(self.col_stack)
        return box

    def _tab_changed(self, *_u):
        if self.stack.get_visible_child_name() == "collections" and not self._collections_loaded:
            self._collections_loaded = True
            self._load_collections()

    def _load_collections(self) -> None:
        from .collections import CollectionCard
        dom = self.ctx.state.nexus_domain
        if not dom:
            self.col_status.set_description(_('Este juego no está enlazado con Nexus Mods.'))
            self.col_stack.set_visible_child_name("status")
            return

        def done(res):
            cols, total = res
            for c in cols:
                self.col_grid.append(CollectionCard(c))
            if cols:
                self.col_stack.set_visible_child_name("grid")
            else:
                self.col_status.set_description(_('Nadie ha publicado colecciones para este juego en Nexus Mods.'))
                self.col_stack.set_visible_child_name("status")

        def fail(e):
            self._collections_loaded = False
            self.col_status.set_title(_('No se pudieron cargar'))
            self.col_status.set_description(GLib.markup_escape_text(str(e)))
            self.col_stack.set_visible_child_name("status")
        run_async(self.ctl.nexus.collections, done, fail, dom, 0, 40)

    def open_collection(self, info) -> None:
        from .collections import CollectionDialog
        CollectionDialog(self, info).present(self.win)

    def open_mod(self, info: ModInfo) -> None:
        from .mod_dialog import ModDialog
        ModDialog(self, info).present(self.win)

    def account_changed(self) -> None:
        pass

    # ================================================================ instalados
    def _build_installed(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12)
        bar = Gtk.Box(spacing=8)
        bar.append(Gtk.Label(label=_('Perfil'), css_classes=["dim-label"]))
        self.profiles = Gtk.DropDown()
        self.profiles.connect("notify::selected", self._profile_selected)
        bar.append(self.profiles)
        pmenu = Gio.Menu()
        pmenu.append(_('Nuevo perfil (copia del actual)…'), "inst.profile-new")
        pmenu.append(_('Renombrar perfil…'), "inst.profile-rename")
        pmenu.append(_('Borrar perfil'), "inst.profile-delete")
        bar.append(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=pmenu, css_classes=["flat"],
                                  tooltip_text=_('Perfiles')))
        bar.append(Gtk.Box(hexpand=True))
        imp = Gtk.Button(label=_('Importar archivo…'), icon_name="document-open-symbolic",
                         tooltip_text=_('Instalar un mod descargado a mano (zip, 7z, rar o .pak)'))
        imp.set_child(Adw.ButtonContent(icon_name="document-open-symbolic", label=_('Importar archivo…')))
        imp.connect("clicked", lambda *_u: self.import_file())
        upd = Gtk.Button()
        upd.set_child(Adw.ButtonContent(icon_name="view-refresh-symbolic", label=_('Buscar actualizaciones')))
        upd.connect("clicked", lambda *_u: self.check_updates())
        self.update_all = Gtk.Button(css_classes=["suggested-action"], visible=False)
        self.update_all.connect("clicked", lambda *_u: self.update_mods(
            [m for m in self.ctx.state.ordered() if m.update_available]))
        bar.append(self.update_all)
        bar.append(imp)
        bar.append(upd)
        box.append(bar)
        group = Gio.SimpleActionGroup()
        for name, cb in (("profile-new", self.profile_new), ("profile-rename", self.profile_rename),
                         ("profile-delete", self.profile_delete)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, cb=cb: cb())
            group.add_action(a)
        self.insert_action_group("inst", group)

        space = Gtk.Box(spacing=8)
        self.space_label = Gtk.Label(xalign=0, hexpand=True, css_classes=["dim-label", "caption"])
        space.append(self.space_label)
        self.space_btn = Gtk.Button(label=_('Borrar descargas'), css_classes=["flat", "caption"],
                                    tooltip_text=_('Borra los archivos descargados de este juego; los mods siguen instalados'))
        self.space_btn.connect("clicked", lambda *_u: self.delete_archives())
        space.append(self.space_btn)
        box.append(space)
        self.order_hint = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        box.append(self.order_hint)
        self.launch_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.launch_box)
        self.notes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.notes)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["load-order"])
        self.list_empty = Adw.StatusPage(icon_name="folder-download-symbolic", title=_('Sin mods instalados'),
                                         description=_('Busca un mod en la pestaña «Buscar mods» o importa un archivo que ya tengas descargado.'))
        self.list_empty.add_css_class("compact")
        self.list_stack = Gtk.Stack()
        self.list_stack.add_named(self.list, "list")
        self.list_stack.add_named(self.list_empty, "empty")
        box.append(self.list_stack)
        self.conflicts = Adw.ExpanderRow(title=_('Conflictos'), subtitle=_('Calculando…'))
        cl = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        cl.append(self.conflicts)
        self.conflicts_box = cl
        box.append(cl)
        return box

    def refresh_installed(self) -> None:
        st = self.ctx.state
        self._loading_profiles = True
        names = list(st.profiles)
        self.profiles.set_model(Gtk.StringList.new(names))
        self.profiles.set_selected(names.index(st.active))
        self._loading_profiles = False
        self.order_hint.set_label(self.ctx.layout.order_hint)
        self.installed_page.set_title(_('Instalados ({0})').format(len(st.mods)) if st.mods else _('Instalados'))
        self.installed_page.set_needs_attention(st.dirty_deploy and bool(st.mods))
        self._missing = manager.missing_requirements(self.ctx)
        n_upd = sum(1 for m in st.mods.values() if m.update_available)
        self.update_all.set_visible(bool(n_upd))
        self.update_all.set_label(_('Actualizar todo ({0})').format(n_upd))
        self._show_space()
        self._show_loader()
        self._fill_list()
        self._update_hero()
        self._compute_conflicts()

    def _fill_list(self, conflicts: list[Conflict] | None = None) -> None:
        self.list.remove_all()
        st = self.ctx.state
        wins: dict[str, int] = {}
        loses: dict[str, int] = {}
        for c in conflicts or []:
            wins[c.winner] = wins.get(c.winner, 0) + 1
            for u in c.losers:
                loses[u] = loses.get(u, 0) + 1
        for i, m in enumerate(st.ordered()):
            self.list.append(ModRow(self, m, i, st.is_enabled(m.uid), wins.get(m.uid, 0), loses.get(m.uid, 0),
                                    self._missing.get(m.uid, [])))
        self.list_stack.set_visible_child_name("list" if st.mods else "empty")
        self.conflicts_box.set_visible(bool(st.mods))

    def _compute_conflicts(self) -> None:
        if not self.ctx.state.mods:
            return
        self.conflicts.set_subtitle(_('Calculando…'))

        def done(plan: Plan):
            self._plan = plan
            self._fill_list(plan.conflicts)
            self._show_conflicts(plan)
        run_async(self.ctx.plan, done, lambda e: self.conflicts.set_subtitle(_('No se pudieron calcular: {0}').format(e)), True)

    def _show_conflicts(self, plan: Plan) -> None:
        # Vaciar filas previas del expander.
        for row in getattr(self, "_conflict_rows", []):
            self.conflicts.remove(row)
        self._conflict_rows = []
        mods = self.ctx.state.mods
        files = [c for c in plan.conflicts if not c.internal]
        internal = [c for c in plan.conflicts if c.internal]
        if not plan.conflicts:
            self.conflicts.set_subtitle(_('Ningún mod activo pisa archivos de otro.'))
        else:
            parts = []
            if files:
                parts.append(_("{0} archivos en conflicto").format(len(files)))
            if internal:
                parts.append(f"{len(internal)} entradas repetidas dentro de paquetes .pak")
            self.conflicts.set_subtitle(" · ".join(parts) + _('. Cambia el orden para decidir cuál gana.'))
        for c in plan.conflicts[:300]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(c.target),
                                subtitle=GLib.markup_escape_text(
                                    _('Gana «{0}» · pisa a ').format(mods[c.winner].name)
                                    + ", ".join(f"«{mods[u].name}»" for u in c.losers)
                                    + (_(' (dentro de .pak)') if c.internal else "")))
            row.set_subtitle_lines(2)
            self.conflicts.add_row(row)
            self._conflict_rows.append(row)
        if len(plan.conflicts) > 300:
            row = Adw.ActionRow(title=_('… y {0} más').format(len(plan.conflicts) - 300))
            self.conflicts.add_row(row)
            self._conflict_rows.append(row)
        # Avisos del tipo de juego (p. ej. DLL de cargadores en Proton).
        child = self.notes.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.notes.remove(child)
            child = nxt
        for n in plan.notes:
            b = Gtk.Box(spacing=10, css_classes=["note-box"])
            b.append(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
            b.append(Gtk.Label(label=n, xalign=0, wrap=True, selectable=True, hexpand=True))
            self.notes.append(b)

    def _show_space(self) -> None:
        if not self.ctx.state.mods:
            self.space_label.set_label("")
            self.space_btn.set_visible(False)
            return

        def done(sizes):
            staged, downloads = sizes
            self.space_label.set_label(_('Espacio: mods extraídos {0} · descargas {1}').format(human_size(staged), human_size(downloads))
)
            self.space_btn.set_visible(downloads > 0)
        run_async(manager.disk_usage, done, None, self.ctx)

    def delete_archive(self, m: ModRecord) -> None:
        freed = manager.delete_archive(self.ctx, m.uid)
        self.win.toast(_('Liberados {0}. Para reinstalar «{1}» habrá que descargarlo de nuevo.').format(human_size(freed), m.name))
        self.refresh_installed()

    def delete_archives(self) -> None:
        d = Adw.AlertDialog(heading=_('¿Borrar las descargas de este juego?'),
                            body=_('Los mods siguen instalados y funcionando. Solo se borran los archivos .zip/.7z/.rar descargados: para reinstalar un mod habrá que descargarlo otra vez.'))
        d.add_response("cancel", _('Cancelar'))
        d.add_response("delete", _('Borrar descargas'))
        d.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "delete":
                freed = manager.delete_archives(self.ctx)
                self.win.toast(_('Liberados {0}').format(human_size(freed)))
                self.refresh_installed()
        d.connect("response", resp)
        d.present(self.win)

    def _show_loader(self) -> None:
        _clear(self.loader_box)
        _clear(self.launch_box)
        ld = self.ctx.loader()
        if ld is None:
            return
        level = {"required": _('obligatorio'), "optional": _('depende del mod'), "none": _('no hace falta')}[ld.level]
        state = "" if ld.level == "none" else (_(" · instalado") if ld.installed else _(' · no instalado'))
        b = Gtk.Box(spacing=12, css_classes=["note-box"])
        icon = ("emblem-ok-symbolic" if ld.installed or ld.level == "none" else
                "dialog-warning-symbolic" if ld.level == "required" else "dialog-information-symbolic")
        b.append(Gtk.Image.new_from_icon_name(icon))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        title = _('Cargador de mods: {0}').format(level) + ("" if ld.level == "none" else f" — {ld.name}{state}")
        col.append(Gtk.Label(label=title, xalign=0, css_classes=["heading"]))
        col.append(Gtk.Label(label=_tilde((f"{ld.engine}. " if ld.engine and ld.engine not in ld.detail else "")
                                    + ld.detail),
                             xalign=0, wrap=True, css_classes=["caption"]))
        self.loader_links = Gtk.Box(spacing=6)
        col.append(self.loader_links)
        b.append(col)
        if ld.url and not ld.installed and ld.level != "none":
            link = Gtk.Button(label=_('Web oficial'), valign=Gtk.Align.CENTER, css_classes=["pill"])
            link.connect("clicked", lambda *_u: Gtk.UriLauncher.new(ld.url).launch(self.win, None, None))
            b.append(link)
        self.loader_box.append(b)
        # Cargadores publicados en Nexus para este juego (se instalan como un mod más).
        dom = self.ctx.state.nexus_domain
        if dom and ld.search and not (ld.installed and ld.level != "none"):
            def done(found):
                if not found:
                    return
                self.loader_links.append(Gtk.Label(label=_('En Nexus:'), css_classes=["caption", "dim-label"]))
                for m in found:
                    btn = Gtk.Button(label=f"{m.name} · {human_count(m.downloads)} ↓", css_classes=["pill", "flat"])
                    btn.connect("clicked", lambda *_a, m=m: self.open_mod(m))
                    self.loader_links.append(btn)
            run_async(self.ctl.nexus.find_loaders, done, None, dom, ld.search)
        cmd = manager.me3_command(self.ctx)
        if cmd:
            # Para lanzar desde Steam: la orden de ME3 sustituye a la del juego («# %command%» la anula).
            line = " ".join(f'"{c}"' if " " in c else c for c in cmd) + " # %command%"
            s = Gtk.Box(spacing=12, css_classes=["note-box"])
            s.append(Gtk.Image.new_from_icon_name("applications-games-symbolic"))
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
            col.append(Gtk.Label(label=_('Para jugar desde Steam: Propiedades → Opciones de lanzamiento'), xalign=0,
                                 css_classes=["heading"]))
            col.append(Gtk.Label(label=_tilde(line), xalign=0, wrap=True, selectable=True,
                                 css_classes=["caption", "monospace"]))
            s.append(col)
            copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_('Copiar'),
                              css_classes=["flat"])
            copy.connect("clicked", lambda *_u: (self.get_clipboard().set(line), self.win.toast(_('Copiado'))))
            s.append(copy)
            self.launch_box.append(s)

    def play(self) -> None:
        cmd = manager.me3_command(self.ctx)
        if not cmd:
            self.win.error(_('Falta Mod Engine 3'), _('No se ha encontrado ME3. Instala un mod que lo traiga o descárgalo.'))
            return
        manager.backup_saves(self.ctx, _('antes de jugar con mods'))
        log.info("lanzando: %s", cmd)
        logfile = paths.LOG_DIR / "me3.log"
        out = logfile.open("w")
        # Sin colores ANSI en el registro, para poder leerlo y mostrarlo.
        env = {**os.environ, "NO_COLOR": "1"}
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        out.close()
        self.win.toast(_('Lanzando el juego con ME3… (Steam debe estar abierto)'), 6)
        ticks = [0]

        def watch():
            ticks[0] += 1
            code = proc.poll()
            if code is None:
                return ticks[0] < 60  # se sigue mirando hasta 30 s; si sigue vivo, todo bien
            if code != 0:
                self._me3_failed(logfile)
            return False
        GLib.timeout_add(500, watch)

    def _me3_failed(self, logfile: Path) -> None:
        text = re.sub(r"\x1b\[[0-9;]*m", "", logfile.read_text(errors="replace"))
        errors = [ln for ln in text.splitlines() if "ERROR" in ln] or text.splitlines()[-3:]
        msg = errors[-1].split("error=")[-1].strip() if errors else "error desconocido"
        hint = ""
        m = re.search(r"Proton runtime (\S+)", msg)
        if m:
            hint = (_('\n\nME3 usa el Proton que Steam tiene asignado al juego ({0}) y no está instalado. En Steam: el juego → Propiedades → Compatibilidad → «Forzar el uso de una herramienta de compatibilidad específica» → elige uno que tengas (p. ej. Proton Experimental).').format(m.group(1)))
        elif "steam" in msg.lower():
            hint = _('\n\nAbre Steam (con tu sesión iniciada) y vuelve a pulsar «Jugar con mods».')
        self.win.error(_('ME3 no ha podido lanzar el juego'), f"{msg}{hint}\n\nRegistro completo: {logfile}")

    def changed(self) -> None:
        """Tras cualquier cambio de orden/estado: guardar y repintar."""
        self.ctx.state.save()
        self.refresh_installed()
        self.win.refresh_library()

    # ---------- acciones sobre mods ----------
    def move(self, uid: str, index: int) -> None:
        self.ctx.state.move(uid, index)
        self.changed()

    def set_enabled(self, uid: str, on: bool) -> None:
        self.ctx.state.set_enabled(uid, on)
        self.changed()

    def remove_mod(self, m: ModRecord) -> None:
        d = Adw.AlertDialog(heading=_('¿Desinstalar «{0}»?').format(m.name),
                            body=_('Se borra de Crisol (los archivos extraídos y la descarga). Si estaba aplicado, sus archivos se quitan del juego al pulsar «Aplicar».'))
        d.add_response("cancel", _('Cancelar'))
        d.add_response("remove", _('Desinstalar'))
        d.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "remove":
                if self.ctx.layout.external:
                    try:
                        manager.ensure_closed(self.ctx)  # con ME3 el juego lee el staging
                    except Exception as e:  # noqa: BLE001
                        self.win.error(_('Juego abierto'), e)
                        return
                self.ctx.state.remove(m.uid)
                if m.archive and Path(m.archive).is_file():
                    Path(m.archive).unlink(missing_ok=True)
                self.changed()
                if self.ctx.is_applied():
                    self.win.toast(_('Pulsa «Aplicar mods» para quitar sus archivos del juego.'))
        d.connect("response", resp)
        d.present(self.win)

    def reinstall_mod(self, m: ModRecord, reconfigure: bool = False) -> None:
        task = self.win.taskbar.add(_('Reinstalando «{0}» desde la descarga').format(m.name))
        chooser = make_chooser(self.win, self.game.install_dir) if reconfigure else None

        def work():
            with manager.game_lock(self.game):
                return manager.reinstall(self.ctx, m.uid, task.update, chooser=chooser)

        def done(rec):
            task.done()
            self.win.toast(_('«{0}» reinstalado: {1} archivos').format(rec.name, len(rec.files)))
            self.changed()

        def fail(e):
            task.done()
            if isinstance(e, manager.InstallCancelled):
                self.win.toast(_('Sin cambios'))
            else:
                self.win.error(_('No se pudo reinstalar'), e)
        run_async(work, done, fail)

    def update_mod(self, m: ModRecord) -> None:
        self.update_mods([m])

    def update_mods(self, mods: list[ModRecord]) -> None:
        """Busca el archivo nuevo de cada mod y lo instala en su sitio (con Premium, directo; sin Premium,
        abriendo su página de descarga uno tras otro)."""
        from ..providers.base import CollectionMod
        task = self.win.taskbar.add(_('Buscando los archivos nuevos'))

        def work():
            items = []
            for m in mods:
                f = manager.update_target(self.ctl.nexus, m)
                if f:
                    items.append(CollectionMod(m.mod_id, f.file_id, m.name, f.name, f.version, f.size, False))
            return items

        def done(items):
            task.done()
            if not items:
                self.win.toast(_('No se ha encontrado un archivo nuevo en Nexus Mods'))
                return
            self.win.install_collection(self.game, self.ctx.state.nexus_domain,
                                        "actualizaciones" if len(items) > 1 else items[0].name, items)

        def fail(e):
            task.done()
            self.win.error(_('No se pudo actualizar'), e)
        run_async(work, done, fail)

    def import_file(self) -> None:
        dlg = Gtk.FileDialog(title=_('Importar mod'))
        f = Gtk.FileFilter(name=_('Mods (zip, 7z, rar, pak)'))
        for pat in ("*.zip", "*.7z", "*.rar", "*.pak", "*.tar.gz", "*.ZIP", "*.7Z", "*.RAR"):
            f.add_pattern(pat)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dlg.set_filters(filters)
        dlg.set_initial_folder(Gio.File.new_for_path(str(Path.home() / _('Descargas'))
                                                     if (Path.home() / _('Descargas')).is_dir() else str(Path.home())))

        def picked(d, res):
            try:
                gf = d.open_finish(res)
            except GLib.Error:
                return
            path = Path(gf.get_path())
            task = self.win.taskbar.add(_('Importando {0}').format(path.name))

            def work():
                with manager.game_lock(self.game):
                    return manager.install_manual(self.ctx, path,
                                                  chooser=make_chooser(self.win, self.game.install_dir))

            def done(rec):
                task.done()
                if not self.ctl.cfg.keep_archives:
                    manager.delete_archive(self.ctx, rec.uid)  # la copia; el original sigue donde estaba
                self.win.toast(_('«{0}» importado').format(rec.name) + (_(' y reconocido en Nexus Mods') if rec.mod_id else ""))
                self.changed()

            def fail(e):
                task.done()
                if isinstance(e, manager.InstallCancelled):
                    self.win.toast(_('Instalación cancelada'))
                else:
                    self.win.error(_('No se pudo importar'), e)
            run_async(work, done, fail)
        dlg.open(self.win, None, picked)

    def check_updates(self) -> None:
        task = self.win.taskbar.add(_('Buscando actualizaciones'))

        def done(n):
            task.done()
            self.win.toast(_("{0} mods tienen actualización").format(n) if n else _('Todos los mods están al día'))
            self.refresh_installed()

        def fail(e):
            task.done()
            self.win.error(_('No se pudieron buscar actualizaciones'), e)
        run_async(manager.check_updates, done, fail, self.ctx, self.ctl.nexus)

    # ---------- aplicar / restaurar ----------
    def apply(self) -> None:
        task = self.win.taskbar.add(_('Aplicando mods a {0}').format(self.game.name))

        def done(r):
            task.done()
            msg = _('Aplicado: {0} archivos').format(r.placed)
            if r.backed_up:
                msg += f", {r.backed_up} originales guardados"
            if r.method:
                msg += f" ({r.method})"
            self.win.toast(msg, 5)
            if r.kept_changed:
                self.win.error(_('Archivos cambiados por otro programa'),
                               _('Estos archivos se habían cambiado fuera de Crisol (p. ej. al verificar el juego en Steam) y se han dejado como estaban:\n\n') + "\n".join(r.kept_changed[:30]))
            self.changed()

        def fail(e):
            task.done()
            self.win.error(_('No se pudieron aplicar los mods'), e)
            self.changed()
        run_async(manager.apply, done, fail, self.ctx, task.update)

    def confirm_restore(self) -> None:
        d = Adw.AlertDialog(heading=_('¿Restaurar el juego sin mods?'),
                            body=_('Se quitan del juego todos los archivos que puso Crisol y se devuelven los originales. Los mods siguen instalados en Crisol para volver a aplicarlos.'))
        d.add_response("cancel", _('Cancelar'))
        d.add_response("restore", _('Restaurar'))
        d.set_response_appearance("restore", Adw.ResponseAppearance.DESTRUCTIVE)
        d.connect("response", lambda _d, r: r == "restore" and self.restore())
        d.present(self.win)

    def restore(self) -> None:
        task = self.win.taskbar.add(_('Restaurando {0}').format(self.game.name))

        def done(r):
            task.done()
            self.win.toast(_('Juego restaurado: {0} archivos quitados, {1} originales devueltos').format(r.removed, r.restored), 5)
            if r.kept_changed:
                self.win.error(_('Archivos cambiados por otro programa'),
                               _('No se han tocado porque ya no eran los que puso Crisol:\n\n')
                               + "\n".join(r.kept_changed[:30]))
            self.changed()

        def fail(e):
            task.done()
            self.win.error(_('No se pudo restaurar'), e)
        run_async(manager.restore, done, fail, self.ctx, task.update)

    # ---------- perfiles ----------
    def _profile_selected(self, dd, _p):
        if getattr(self, "_loading_profiles", False):
            return
        item = dd.get_selected_item()
        if item and item.get_string() != self.ctx.state.active:
            self.ctx.state.switch(item.get_string())
            self.changed()
            self.win.toast(_('Perfil cambiado. Pulsa «Aplicar mods» para llevarlo al juego.'))

    def _ask_name(self, heading: str, initial: str, cb) -> None:
        d = Adw.AlertDialog(heading=heading)
        entry = Gtk.Entry(text=initial, activates_default=True)
        d.set_extra_child(entry)
        d.add_response("cancel", _('Cancelar'))
        d.add_response("ok", _('Aceptar'))
        d.set_default_response("ok")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            name = entry.get_text().strip()
            if r == "ok" and name:
                if name in self.ctx.state.profiles and name != initial:
                    self.win.error(_('Nombre repetido'), _('Ya hay un perfil «{0}».').format(name))
                    return
                cb(name)
        d.connect("response", resp)
        d.present(self.win)

    def profile_new(self) -> None:
        def make(name):
            self.ctx.state.add_profile(name, copy_from=self.ctx.state.active)
            self.ctx.state.switch(name)
            self.changed()
        self._ask_name(_('Nuevo perfil'), _("{0} (copia)").format(self.ctx.state.active), make)

    def profile_rename(self) -> None:
        old = self.ctx.state.active

        def ren(name):
            self.ctx.state.rename_profile(old, name)
            self.changed()
        self._ask_name(_('Renombrar perfil'), old, ren)

    def profile_delete(self) -> None:
        st = self.ctx.state
        if len(st.profiles) == 1:
            self.win.error(_('No se puede borrar'), _('Es el único perfil del juego.'))
            return
        st.delete_profile(st.active)
        self.changed()

    # ---------- ajustes ----------
    def show_settings(self) -> None:
        GameSettingsDialog(self).present(self.win)

    def settings_changed(self) -> None:
        self._update_hero()
        self.refresh_installed()
        self.search(reset=True)


def _tilde(text: str) -> str:
    """Abrevia la carpeta personal con «~» (la de $HOME y la del usuario en el sistema)."""
    for home in sorted({str(Path.home()), pwd.getpwuid(os.getuid()).pw_dir}, key=len, reverse=True):
        text = text.replace(home, "~")
    return text


def _clear(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def _open(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ================================================================ tarjeta de resultado

class ModCard(Gtk.Box):
    def __init__(self, info: ModInfo):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["mod-card"], width_request=230)
        self.info = info
        thumb = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, height_request=130, css_classes=["thumb"],
                            can_shrink=True)
        frame = Gtk.Box(overflow=Gtk.Overflow.HIDDEN, css_classes=["thumb"], height_request=130)
        thumb.set_hexpand(True)
        frame.append(thumb)
        load_remote(thumb, info.thumbnail, 230, 130)
        self.append(frame)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, margin_start=12, margin_end=12,
                      margin_top=10, margin_bottom=12)
        col.append(Gtk.Label(label=info.name, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["mod-name"],
                             tooltip_text=info.name, max_width_chars=1, hexpand=True))
        col.append(Gtk.Label(label=f"{info.author} · v{info.version}" if info.version else info.author, xalign=0,
                             ellipsize=Pango.EllipsizeMode.END, css_classes=["mod-meta"], max_width_chars=1))
        summ = Gtk.Label(label=info.summary, xalign=0, wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END,
                         css_classes=["mod-summary"], height_request=36, valign=Gtk.Align.START,
                         max_width_chars=1, width_chars=1)
        col.append(summ)
        stats = Gtk.Box(spacing=12)
        for icon, val in (("folder-download-symbolic", info.downloads), ("starred-symbolic", info.endorsements)):
            b = Gtk.Box(spacing=4, css_classes=["mod-meta"])
            b.append(Gtk.Image.new_from_icon_name(icon))
            b.append(Gtk.Label(label=human_count(val)))
            stats.append(b)
        if info.category:
            stats.append(Gtk.Label(label=info.category, css_classes=["chip"], hexpand=True, halign=Gtk.Align.END,
                                   ellipsize=Pango.EllipsizeMode.END, max_width_chars=12))
        col.append(stats)
        self.append(col)


# ================================================================ fila del orden de carga

class ModRow(Gtk.ListBoxRow):
    def __init__(self, page: GamePage, m: ModRecord, index: int, enabled: bool, wins: int, loses: int,
                 missing: list[dict]):
        super().__init__(activatable=False)
        self.page, self.mod, self.index = page, m, index
        if not enabled:
            self.add_css_class("disabled-mod")
        box = Gtk.Box(spacing=12, margin_start=10, margin_end=10, margin_top=8, margin_bottom=8)
        handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        handle.add_css_class("drag-handle")
        handle.set_tooltip_text(_('Arrastra para cambiar el orden'))
        handle.set_cursor(Gdk.Cursor.new_from_name("grab"))
        box.append(handle)
        box.append(Gtk.Label(label=str(index + 1), css_classes=["order-pos"], xalign=1))
        thumb = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, width_request=64, height_request=40)
        tf = Gtk.Box(overflow=Gtk.Overflow.HIDDEN, css_classes=["row-thumb"], width_request=64, height_request=40,
                     valign=Gtk.Align.CENTER)
        if m.thumbnail:
            tf.append(thumb)
            load_remote(thumb, m.thumbnail, 64, 40)
        else:
            ph = placeholder("package-x-generic-symbolic", 20)
            ph.set_hexpand(True)
            tf.append(ph)
        box.append(tf)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True, valign=Gtk.Align.CENTER)
        col.append(Gtk.Label(label=m.name, xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"]))
        meta = Gtk.Box(spacing=6)
        sub = " · ".join(x for x in (f"v{m.version}" if m.version else "", m.author,
                                     "Nexus" if m.provider == "nexus" else "Importado a mano",
                                     _("{0} archivos").format(len(m.files)),
                                     human_size(m.staged_size) if m.staged_size else "") if x)
        meta.append(Gtk.Label(label=sub, xalign=0, css_classes=["dim-label", "caption"],
                              ellipsize=Pango.EllipsizeMode.END))
        if m.update_available:
            meta.append(Gtk.Label(label=_('Actualización: v{0}').format(m.latest_version), css_classes=["chip", "accent"]))
        if wins and enabled:
            meta.append(Gtk.Label(label=_('Gana {0}').format(wins), css_classes=["chip", "success"],
                                  tooltip_text=_('Archivos en los que este mod pisa a otro')))
        if loses and enabled:
            meta.append(Gtk.Label(label=_('Pierde {0}').format(loses), css_classes=["chip", "warning"],
                                  tooltip_text=_('Archivos en los que otro mod (más abajo) pisa a este')))
        if missing:
            names = ", ".join(r.get("name") or str(r.get("mod_id")) for r in missing)
            internal = [r for r in missing if not r.get("external")]
            meta.append(Gtk.Label(label=_('Faltan requisitos') if internal else _('Requisitos externos'),
                                  css_classes=["chip", "error" if internal else "warning"],
                                  tooltip_text=_('Necesita: {0}').format(names)))
        if m.verified == "md5":
            meta.append(Gtk.Image(icon_name="emblem-ok-symbolic", tooltip_text=_('Descarga verificada con md5'),
                                  css_classes=["dim-label"]))
        col.append(meta)
        box.append(col)
        sw = Gtk.Switch(active=enabled, valign=Gtk.Align.CENTER, tooltip_text=_('Activar o desactivar'))
        sw.connect("notify::active", lambda s, _p: page.set_enabled(m.uid, s.get_active()))
        box.append(sw)
        menu = Gio.Menu()
        if m.update_available:
            menu.append(_('Actualizar a v{0}').format(m.latest_version), "row.update")
        menu.append(_('Subir'), "row.up")
        menu.append(_('Bajar'), "row.down")
        menu.append(_('Al principio'), "row.top")
        menu.append(_('Al final'), "row.bottom")
        if manager.archive_size(m):
            if m.fomod_name:
                menu.append(_('Cambiar opciones del instalador…'), "row.reconfigure")
            menu.append(_('Reinstalar desde la descarga'), "row.reinstall")
            menu.append(_('Borrar archivo descargado ({0})').format(human_size(manager.archive_size(m))), "row.delete-archive")
        if m.mod_id:
            menu.append(_('Ver en Nexus Mods'), "row.page")
        menu.append(_('Desinstalar'), "row.remove")
        box.append(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER,
                                  css_classes=["flat"]))
        n = len(page.ctx.state.mods)
        group = Gio.SimpleActionGroup()
        acts = {
            "update": lambda: page.update_mod(m),
            "up": lambda: page.move(m.uid, index - 1),
            "down": lambda: page.move(m.uid, index + 1),
            "top": lambda: page.move(m.uid, 0),
            "bottom": lambda: page.move(m.uid, n - 1),
            "page": lambda: Gtk.UriLauncher.new(f"https://www.nexusmods.com/{m.game_domain}/mods/{m.mod_id}")
            .launch(page.win, None, None),
            "remove": lambda: page.remove_mod(m),
            "reinstall": lambda: page.reinstall_mod(m),
            "reconfigure": lambda: page.reinstall_mod(m, reconfigure=True),
            "delete-archive": lambda: page.delete_archive(m),
        }
        for name, cb in acts.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, cb=cb: cb())
            group.add_action(a)
        group.lookup_action("up").set_enabled(index > 0)
        group.lookup_action("top").set_enabled(index > 0)
        group.lookup_action("down").set_enabled(index < n - 1)
        group.lookup_action("bottom").set_enabled(index < n - 1)
        self.insert_action_group("row", group)
        self.set_child(box)

        # Arrastrar y soltar para reordenar.
        src = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        src.connect("prepare", lambda *_u: Gdk.ContentProvider.new_for_value(m.uid))
        src.connect("drag-begin", self._drag_begin)
        self.add_controller(src)
        tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        tgt.connect("motion", self._motion)
        tgt.connect("leave", lambda *_u: self._mark(None))
        tgt.connect("drop", self._drop)
        self.add_controller(tgt)

    def _drag_begin(self, src, drag):
        icon = Gtk.WidgetPaintable.new(self)
        src.set_icon(icon, 20, 20)

    def _mark(self, where: str | None) -> None:
        self.remove_css_class("drop-above")
        self.remove_css_class("drop-below")
        if where:
            self.add_css_class(f"drop-{where}")

    def _motion(self, _t, _x, y):
        self._mark("above" if y < self.get_height() / 2 else "below")
        return Gdk.DragAction.MOVE

    def _drop(self, _t, uid, _x, y):
        self._mark(None)
        if uid == self.mod.uid:
            return False
        order = self.page.ctx.state.profile.order
        if uid not in order:
            return False
        target = self.index + (0 if y < self.get_height() / 2 else 1)
        if order.index(uid) < target:
            target -= 1  # al sacar el mod de arriba, las posiciones de debajo bajan una
        GLib.idle_add(lambda: (self.page.move(uid, target), False)[1])
        return True


# ================================================================ ajustes del juego

class GameSettingsDialog(Adw.PreferencesDialog):
    def __init__(self, page: GamePage):
        super().__init__(title=_('Ajustes de {0}').format(page.game.name))
        self.page = page
        st = page.ctx.state
        pg = Adw.PreferencesPage()
        g1 = Adw.PreferencesGroup(title="Nexus Mods",
                                  description=_('Juego de Nexus Mods del que se buscan los mods. Se detecta por el nombre; corrígelo si no es el correcto (el nombre corto de la dirección: nexusmods.com/<b>nombre</b>).'))
        self.domain = Adw.EntryRow(title=_('Dominio en Nexus Mods'), text=st.nexus_domain or "",
                                   show_apply_button=True)
        self.domain.connect("apply", self._apply_domain)
        g1.add(self.domain)
        pg.add(g1)
        g2 = Adw.PreferencesGroup(title=_('Tipo de juego'),
                                  description=_('Cómo se colocan los mods y cómo se expresa el orden. Al cambiarlo se recoloca lo ya instalado (sin volver a descargar).'))
        ids = [None] + list(LAYOUTS)
        labels = [_('Automático')] + [LAYOUTS[i].label for i in LAYOUTS]
        self.layout = Adw.ComboRow(title=_('Tipo'), model=Gtk.StringList.new(labels),
                                   selected=ids.index(st.layout) if st.layout in ids else 0)
        self.layout_ids = ids
        self.layout.connect("notify::selected", self._apply_layout)
        g2.add(self.layout)
        for i in LAYOUTS:
            r = Adw.ActionRow(title=LAYOUTS[i].label, subtitle=LAYOUTS[i].description)
            r.add_css_class("property")
            g2.add(r)
        pg.add(g2)
        if page.ctx.layout.external:
            pg.add(self._me3_group())
        self.add(pg)
        self.add(self._saves_page())

    def _saves_page(self) -> Adw.PreferencesPage:
        from .. import saves
        game = self.page.game
        pg = Adw.PreferencesPage(title=_('Partidas'), icon_name="document-save-symbolic")
        where = Adw.PreferencesGroup(title=_('Dónde guarda este juego'),
                                     description=_('Crisol copia estas carpetas antes de aplicar mods o de jugar con ellos (como mucho cada 10 minutos; se guardan las 5 últimas copias).'))
        self._where = where
        pg.add(where)
        self._backups = Adw.PreferencesGroup(title=_('Copias'))
        now = Gtk.Button(label=_('Hacer una copia ahora'), css_classes=["flat"], valign=Gtk.Align.CENTER)
        now.connect("clicked", lambda *_u: self._backup_now())
        self._backups.set_header_suffix(now)
        pg.add(self._backups)
        self._rows: list = []

        def found(dirs):
            if not dirs:
                where.add(Adw.ActionRow(title=_('No se han encontrado partidas'),
                                        subtitle=_('Puede que el juego guarde en la nube o en un sitio poco habitual.')))
            for d in dirs:
                row = Adw.ActionRow(title=GLib.markup_escape_text(d.rel),
                                    subtitle=_('prefijo de Proton') if d.root == "prefix" else _('carpeta del juego'))
                row.add_suffix(Gtk.Label(label=human_size(d.size), css_classes=["dim-label"]))
                where.add(row)
        run_async(saves.find, found, None, game)
        self._fill_backups()
        return pg

    def _fill_backups(self) -> None:
        import time as _t
        from .. import saves
        for r in self._rows:
            self._backups.remove(r)
        self._rows = []
        items = saves.backups(self.page.game)
        if not items:
            r = Adw.ActionRow(title=_('Aún no hay copias'))
            self._backups.add(r)
            self._rows.append(r)
        for b in items:
            r = Adw.ActionRow(title=_t.strftime("%d/%m/%Y %H:%M", _t.localtime(b["time"])),
                              subtitle=GLib.markup_escape_text(f"{b.get('reason', '')} · {human_size(b.get('size', 0))}"))
            btn = Gtk.Button(label=_('Restaurar'), valign=Gtk.Align.CENTER, css_classes=["flat"])
            btn.connect("clicked", lambda *_a, b=b: self._restore(b))
            r.add_suffix(btn)
            self._backups.add(r)
            self._rows.append(r)

    def _backup_now(self) -> None:
        from .. import saves

        def done(b):
            self.add_toast(Adw.Toast(title=_('Copia hecha') if b else _('No hay partidas que copiar')))
            self._fill_backups()
        run_async(saves.backup, done, lambda e: self.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(e)))),
                  self.page.game, _("copia manual"))

    def _restore(self, b: dict) -> None:
        from .. import saves
        d = Adw.AlertDialog(heading=_('¿Restaurar estas partidas?'),
                            body=_('Las partidas actuales se sustituyen por las de la copia (antes se hace una copia de las actuales, por si acaso). El juego debe estar cerrado.'))
        d.add_response("cancel", _('Cancelar'))
        d.add_response("ok", _('Restaurar'))
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r != "ok":
                return
            try:
                manager.ensure_closed(self.page.ctx)
                n = saves.restore(self.page.game, b["id"])
            except Exception as e:  # noqa: BLE001
                self.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(e)), timeout=8))
                return
            self.add_toast(Adw.Toast(title=_('Partidas restauradas ({0} archivos)').format(n)))
            self._fill_backups()
        d.connect("response", resp)
        d.present(self)

    def _me3_group(self) -> Adw.PreferencesGroup:
        st = self.page.ctx.state
        g = Adw.PreferencesGroup(title="Mod Engine 3",
                                 description=_('Opciones al lanzar el juego con ME3 (también en la línea para Steam).'))
        for key, title, sub in (("skip_logos", _('Saltar los logos de inicio'), ""),
                                ("no_boot_boost", _('Sin caché de arranque'),
                                 _('ME3 guarda descifrados los archivos del juego para arrancar más rápido; desactívalo si da problemas'))):
            row = Adw.SwitchRow(title=title, subtitle=sub, active=bool(st.me3_opts.get(key)))
            row.connect("notify::active", self._me3_opt, key)
            g.add(row)
        for m in st.ordered():
            if len(m.me3_variants) > 1:
                row = Adw.ComboRow(title=_('Perfil de «{0}»').format(m.name), model=Gtk.StringList.new(m.me3_variants),
                                   selected=m.me3_variants.index(m.me3_variant) if m.me3_variant in m.me3_variants else 0)
                row.connect("notify::selected", self._me3_variant, m.uid)
                g.add(row)
        return g

    def _me3_opt(self, row, _p, key):
        st = self.page.ctx.state
        st.me3_opts[key] = row.get_active()
        st.save()
        self.page.settings_changed()

    def _me3_variant(self, row, _p, uid):
        st = self.page.ctx.state
        m = st.mods[uid]
        m.me3_variant = m.me3_variants[row.get_selected()]
        manager.remap(self.page.ctx)
        self.page.settings_changed()

    def _apply_domain(self, row):
        dom = row.get_text().strip().lower().strip("/").split("/")[-1]
        ctl = self.page.ctl

        def check():
            return ctl.nexus.game_id(dom) if dom else 0

        def ok(_gid):
            ctl.set_domain(self.page.game, dom)
            self.page.settings_changed()
            self.add_toast(Adw.Toast(title=_('Juego de Nexus actualizado') if dom else _('Juego desenlazado de Nexus')))

        def bad(e):
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(e))))
        run_async(check, ok, bad)

    def _apply_layout(self, row, _p):
        st = self.page.ctx.state
        st.layout = self.layout_ids[row.get_selected()]
        failed = manager.remap(self.page.ctx)
        if failed:
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(
                _('No encajan en este tipo: ') + ", ".join(failed)), timeout=8))
        self.page.settings_changed()
