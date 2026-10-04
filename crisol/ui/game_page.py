"""Detalle de un juego: cabecera, buscador de mods y mods instalados con su orden de carga."""
from __future__ import annotations

import logging
import os
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
from .util import human_count, load_remote, local_texture, placeholder, run_async

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
        menu.append("Ajustes del juego…", "game.settings")
        menu.append("Abrir carpeta del juego", "game.open-dir")
        menu.append("Abrir carpeta de descargas", "game.open-downloads")
        hb.pack_end(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, tooltip_text="Más opciones"))
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
        self.stack.add_titled_with_icon(self._build_search(), "search", "Buscar mods", "system-search-symbolic")
        self.installed_page = self.stack.add_titled_with_icon(self._build_installed(), "installed", "Instalados",
                                                              "view-list-bullet-symbolic")
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
                    "Mods que no encajan en este juego",
                    f"Con el tipo «{self.ctx.layout.label}» estos mods no se pueden colocar: "
                    + ", ".join(failed) + ".\n\nSi alguno se descargó mal, usa ⋮ → «Reinstalar desde la "
                    "descarga»; si no es para este cargador, desinstálalo."), False)[1])
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
        path = Gtk.Label(label=str(g.install_dir).replace(str(Path.home()), "~"), xalign=0,
                         ellipsize=Pango.EllipsizeMode.MIDDLE, css_classes=["hero-sub", "caption"])
        info.append(path)
        self.hero_status = Gtk.Label(xalign=0, css_classes=["hero-sub"], wrap=True)
        info.append(self.hero_status)
        spacer = Gtk.Box(vexpand=True)
        info.append(spacer)
        actions = Gtk.Box(spacing=10)
        self.apply_btn = Gtk.Button(label="Aplicar mods", css_classes=["pill", "suggested-action"])
        self.apply_btn.connect("clicked", lambda *_: self.apply())
        self.restore_btn = Gtk.Button(label="Restaurar juego sin mods", css_classes=["pill"])
        self.restore_btn.connect("clicked", lambda *_: self.confirm_restore())
        actions.append(self.apply_btn)
        actions.append(self.restore_btn)
        self.play_btn = Gtk.Button(css_classes=["pill"], visible=False)
        self.play_btn.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic", label="Jugar con mods"))
        self.play_btn.connect("clicked", lambda *_: self.play())
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
        self.domain_chip.set_label(f"Nexus: {dom}" if dom else "Sin juego de Nexus")
        self.domain_chip.set_css_classes(["chip", "accent" if dom else "warning"])
        lay = self.ctx.layout
        self.layout_chip.set_label(lay.label + ("" if st.layout else " (auto)"))
        deployed = self.ctx.is_applied()
        n_on, n = len(st.profile.enabled), len(st.mods)
        if not n:
            txt = "Aún no hay mods instalados."
        elif st.dirty_deploy:
            txt = f"Perfil «{st.active}»: {n_on} de {n} mods activos · hay cambios sin aplicar al juego."
        elif deployed:
            txt = f"Perfil «{st.active}» aplicado: {n_on} de {n} mods activos en el juego."
        else:
            txt = f"Perfil «{st.active}»: {n_on} de {n} mods activos · el juego está sin mods."
        self.hero_status.set_label(txt)
        self.apply_btn.set_sensitive(bool(n) and (st.dirty_deploy or not deployed))
        self.restore_btn.set_sensitive(deployed)
        external = lay.external
        self.restore_btn.set_label("Quitar perfil de ME3" if external else "Restaurar juego sin mods")
        self.apply_btn.set_label("Guardar perfil de ME3" if external else "Aplicar mods")
        self.play_btn.set_visible(external)
        self.play_btn.set_sensitive(deployed and not st.dirty_deploy)
        ld = self.ctx.loader()
        if ld is None:
            self.loader_chip.set_visible(False)
        else:
            self.loader_chip.set_visible(True)
            if ld.level == "none":
                self.loader_chip.set_label("No necesita cargador de mods")
                self.loader_chip.set_css_classes(["chip"])
            elif ld.installed:
                self.loader_chip.set_label(f"Cargador: {ld.name} ✓")
                self.loader_chip.set_css_classes(["chip", "success"])
            elif ld.level == "required":
                self.loader_chip.set_label(f"Necesita cargador: {ld.name}")
                self.loader_chip.set_css_classes(["chip", "error"])
            else:
                self.loader_chip.set_label(f"Cargador según el mod: {ld.name}")
                self.loader_chip.set_css_classes(["chip", "warning"])

    # ================================================================ buscador
    def _build_search(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=12)
        bar = Gtk.Box(spacing=10)
        self.query = Gtk.SearchEntry(placeholder_text="Buscar mods en Nexus Mods…", hexpand=True)
        self.query.connect("search-changed", lambda *_: self.search(reset=True))
        self.sort_keys = list(SORTS)
        self.sort = Gtk.DropDown.new_from_strings([SORTS[k] for k in self.sort_keys])
        self.sort.set_selected(1)
        self.sort.connect("notify::selected", lambda *_: self.search(reset=True))
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
        self.more = Gtk.Button(label="Cargar más", halign=Gtk.Align.CENTER, css_classes=["pill"], visible=False)
        self.more.connect("clicked", lambda *_: self.search(reset=False))
        box.append(self.more)
        return box

    def search(self, reset: bool) -> None:
        domain = self.ctx.state.nexus_domain
        if not domain:
            self._search_message("dialog-question-symbolic", "Juego sin enlazar con Nexus Mods",
                                 "No se ha encontrado este juego en Nexus Mods. Enlázalo a mano en "
                                 "⋯ → Ajustes del juego.")
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
            self.results_info.set_label(f"{human_count(page.total)} mods" + (f" para «{query}»" if query else ""))
            if self._offset == 0:
                self._search_message("system-search-symbolic", "Sin resultados", "Prueba con otras palabras.")
            else:
                self.search_stack.set_visible_child_name("results")

        def fail(e):
            if qid == self._query_id:
                self._search_message("network-error-symbolic", "No se pudo buscar", str(e))
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

    def open_mod(self, info: ModInfo) -> None:
        from .mod_dialog import ModDialog
        ModDialog(self, info).present(self.win)

    def account_changed(self) -> None:
        pass

    # ================================================================ instalados
    def _build_installed(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12)
        bar = Gtk.Box(spacing=8)
        bar.append(Gtk.Label(label="Perfil", css_classes=["dim-label"]))
        self.profiles = Gtk.DropDown()
        self.profiles.connect("notify::selected", self._profile_selected)
        bar.append(self.profiles)
        pmenu = Gio.Menu()
        pmenu.append("Nuevo perfil (copia del actual)…", "inst.profile-new")
        pmenu.append("Renombrar perfil…", "inst.profile-rename")
        pmenu.append("Borrar perfil", "inst.profile-delete")
        bar.append(Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=pmenu, css_classes=["flat"],
                                  tooltip_text="Perfiles"))
        bar.append(Gtk.Box(hexpand=True))
        imp = Gtk.Button(label="Importar archivo…", icon_name="document-open-symbolic",
                         tooltip_text="Instalar un mod descargado a mano (zip, 7z, rar o .pak)")
        imp.set_child(Adw.ButtonContent(icon_name="document-open-symbolic", label="Importar archivo…"))
        imp.connect("clicked", lambda *_: self.import_file())
        upd = Gtk.Button()
        upd.set_child(Adw.ButtonContent(icon_name="view-refresh-symbolic", label="Buscar actualizaciones"))
        upd.connect("clicked", lambda *_: self.check_updates())
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

        self.order_hint = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        box.append(self.order_hint)
        self.launch_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.launch_box)
        self.notes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self.notes)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["load-order"])
        self.list_empty = Adw.StatusPage(icon_name="folder-download-symbolic", title="Sin mods instalados",
                                         description="Busca un mod en la pestaña «Buscar mods» o importa un "
                                                     "archivo que ya tengas descargado.")
        self.list_empty.add_css_class("compact")
        self.list_stack = Gtk.Stack()
        self.list_stack.add_named(self.list, "list")
        self.list_stack.add_named(self.list_empty, "empty")
        box.append(self.list_stack)
        self.conflicts = Adw.ExpanderRow(title="Conflictos", subtitle="Calculando…")
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
        self.installed_page.set_title(f"Instalados ({len(st.mods)})" if st.mods else "Instalados")
        self.installed_page.set_needs_attention(st.dirty_deploy and bool(st.mods))
        self._missing = manager.missing_requirements(self.ctx)
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
        self.conflicts.set_subtitle("Calculando…")

        def done(plan: Plan):
            self._plan = plan
            self._fill_list(plan.conflicts)
            self._show_conflicts(plan)
        run_async(self.ctx.plan, done, lambda e: self.conflicts.set_subtitle(f"No se pudieron calcular: {e}"), True)

    def _show_conflicts(self, plan: Plan) -> None:
        # Vaciar filas previas del expander.
        for row in getattr(self, "_conflict_rows", []):
            self.conflicts.remove(row)
        self._conflict_rows = []
        mods = self.ctx.state.mods
        files = [c for c in plan.conflicts if not c.internal]
        internal = [c for c in plan.conflicts if c.internal]
        if not plan.conflicts:
            self.conflicts.set_subtitle("Ningún mod activo pisa archivos de otro.")
        else:
            parts = []
            if files:
                parts.append(f"{len(files)} archivos en conflicto")
            if internal:
                parts.append(f"{len(internal)} entradas repetidas dentro de paquetes .pak")
            self.conflicts.set_subtitle(" · ".join(parts) + ". Cambia el orden para decidir cuál gana.")
        for c in plan.conflicts[:300]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(c.target),
                                subtitle=GLib.markup_escape_text(
                                    f"Gana «{mods[c.winner].name}» · pisa a "
                                    + ", ".join(f"«{mods[u].name}»" for u in c.losers)
                                    + (" (dentro de .pak)" if c.internal else "")))
            row.set_subtitle_lines(2)
            self.conflicts.add_row(row)
            self._conflict_rows.append(row)
        if len(plan.conflicts) > 300:
            row = Adw.ActionRow(title=f"… y {len(plan.conflicts) - 300} más")
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

    def _show_loader(self) -> None:
        _clear(self.loader_box)
        _clear(self.launch_box)
        ld = self.ctx.loader()
        if ld is None:
            return
        level = {"required": "obligatorio", "optional": "depende del mod", "none": "no hace falta"}[ld.level]
        state = "" if ld.level == "none" else (" · instalado" if ld.installed else " · no instalado")
        b = Gtk.Box(spacing=12, css_classes=["note-box"])
        icon = ("emblem-ok-symbolic" if ld.installed or ld.level == "none" else
                "dialog-warning-symbolic" if ld.level == "required" else "dialog-information-symbolic")
        b.append(Gtk.Image.new_from_icon_name(icon))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        title = f"Cargador de mods: {level}" + ("" if ld.level == "none" else f" — {ld.name}{state}")
        col.append(Gtk.Label(label=title, xalign=0, css_classes=["heading"]))
        col.append(Gtk.Label(label=(f"{ld.engine}. " if ld.engine and ld.engine not in ld.detail else "") + ld.detail,
                             xalign=0, wrap=True, css_classes=["caption"]))
        self.loader_links = Gtk.Box(spacing=6)
        col.append(self.loader_links)
        b.append(col)
        if ld.url and not ld.installed and ld.level != "none":
            link = Gtk.Button(label="Web oficial", valign=Gtk.Align.CENTER, css_classes=["pill"])
            link.connect("clicked", lambda *_: Gtk.UriLauncher.new(ld.url).launch(self.win, None, None))
            b.append(link)
        self.loader_box.append(b)
        # Cargadores publicados en Nexus para este juego (se instalan como un mod más).
        dom = self.ctx.state.nexus_domain
        if dom and ld.search and not (ld.installed and ld.level != "none"):
            def done(found):
                if not found:
                    return
                self.loader_links.append(Gtk.Label(label="En Nexus:", css_classes=["caption", "dim-label"]))
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
            col.append(Gtk.Label(label="Para jugar desde Steam: Propiedades → Opciones de lanzamiento", xalign=0,
                                 css_classes=["heading"]))
            col.append(Gtk.Label(label=line, xalign=0, wrap=True, selectable=True, css_classes=["caption", "monospace"]))
            s.append(col)
            copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Copiar",
                              css_classes=["flat"])
            copy.connect("clicked", lambda *_: (self.get_clipboard().set(line), self.win.toast("Copiado")))
            s.append(copy)
            self.launch_box.append(s)

    def play(self) -> None:
        cmd = manager.me3_command(self.ctx)
        if not cmd:
            self.win.error("Falta Mod Engine 3", "No se ha encontrado ME3. Instala un mod que lo traiga o descárgalo.")
            return
        log.info("lanzando: %s", cmd)
        logfile = paths.LOG_DIR / "me3.log"
        out = logfile.open("w")
        # Sin colores ANSI en el registro, para poder leerlo y mostrarlo.
        env = {**os.environ, "NO_COLOR": "1"}
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        out.close()
        self.win.toast("Lanzando el juego con ME3… (Steam debe estar abierto)", 6)
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
        errors = [l for l in text.splitlines() if "ERROR" in l] or text.splitlines()[-3:]
        msg = errors[-1].split("error=")[-1].strip() if errors else "error desconocido"
        hint = ""
        m = re.search(r"Proton runtime (\S+)", msg)
        if m:
            hint = (f"\n\nME3 usa el Proton que Steam tiene asignado al juego ({m.group(1)}) y no está instalado. "
                    "En Steam: el juego → Propiedades → Compatibilidad → «Forzar el uso de una herramienta de "
                    "compatibilidad específica» → elige uno que tengas (p. ej. Proton Experimental).")
        elif "steam" in msg.lower():
            hint = "\n\nAbre Steam (con tu sesión iniciada) y vuelve a pulsar «Jugar con mods»."
        self.win.error("ME3 no ha podido lanzar el juego", f"{msg}{hint}\n\nRegistro completo: {logfile}")

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
        d = Adw.AlertDialog(heading=f"¿Desinstalar «{m.name}»?",
                            body="Se borra de Crisol (los archivos extraídos y la descarga). Si estaba aplicado, "
                                 "sus archivos se quitan del juego al pulsar «Aplicar».")
        d.add_response("cancel", "Cancelar")
        d.add_response("remove", "Desinstalar")
        d.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(_d, r):
            if r == "remove":
                self.ctx.state.remove(m.uid)
                if m.archive and Path(m.archive).is_file():
                    Path(m.archive).unlink(missing_ok=True)
                self.changed()
                if self.ctx.is_applied():
                    self.win.toast("Pulsa «Aplicar mods» para quitar sus archivos del juego.")
        d.connect("response", resp)
        d.present(self.win)

    def reinstall_mod(self, m: ModRecord) -> None:
        task = self.win.taskbar.add(f"Reinstalando «{m.name}» desde la descarga")

        def work():
            with manager.game_lock(self.game):
                return manager.reinstall(self.ctx, m.uid, task.update)

        def done(rec):
            task.done()
            self.win.toast(f"«{rec.name}» reinstalado: {len(rec.files)} archivos")
            self.changed()

        def fail(e):
            task.done()
            self.win.error("No se pudo reinstalar", e)
        run_async(work, done, fail)

    def update_mod(self, m: ModRecord) -> None:
        info = ModInfo(provider="nexus", mod_id=m.mod_id, name=m.name, author=m.author, version=m.latest_version,
                       thumbnail=m.thumbnail)
        self.open_mod(info)

    def import_file(self) -> None:
        dlg = Gtk.FileDialog(title="Importar mod")
        f = Gtk.FileFilter(name="Mods (zip, 7z, rar, pak)")
        for pat in ("*.zip", "*.7z", "*.rar", "*.pak", "*.tar.gz", "*.ZIP", "*.7Z", "*.RAR"):
            f.add_pattern(pat)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dlg.set_filters(filters)
        dlg.set_initial_folder(Gio.File.new_for_path(str(Path.home() / "Descargas")
                                                     if (Path.home() / "Descargas").is_dir() else str(Path.home())))

        def picked(d, res):
            try:
                gf = d.open_finish(res)
            except GLib.Error:
                return
            path = Path(gf.get_path())
            task = self.win.taskbar.add(f"Importando {path.name}")

            def work():
                with manager.game_lock(self.game):
                    return manager.install_manual(self.ctx, path)

            def done(rec):
                task.done()
                self.win.toast(f"«{rec.name}» importado" + (" y reconocido en Nexus Mods" if rec.mod_id else ""))
                self.changed()

            def fail(e):
                task.done()
                self.win.error("No se pudo importar", e)
            run_async(work, done, fail)
        dlg.open(self.win, None, picked)

    def check_updates(self) -> None:
        task = self.win.taskbar.add("Buscando actualizaciones")

        def done(n):
            task.done()
            self.win.toast(f"{n} mods tienen actualización" if n else "Todos los mods están al día")
            self.refresh_installed()

        def fail(e):
            task.done()
            self.win.error("No se pudieron buscar actualizaciones", e)
        run_async(manager.check_updates, done, fail, self.ctx, self.ctl.nexus)

    # ---------- aplicar / restaurar ----------
    def apply(self) -> None:
        task = self.win.taskbar.add(f"Aplicando mods a {self.game.name}")

        def done(r):
            task.done()
            msg = f"Aplicado: {r.placed} archivos"
            if r.backed_up:
                msg += f", {r.backed_up} originales guardados"
            if r.method:
                msg += f" ({r.method})"
            self.win.toast(msg, 5)
            if r.kept_changed:
                self.win.error("Archivos cambiados por otro programa",
                               "Estos archivos se habían cambiado fuera de Crisol (p. ej. al verificar el juego en "
                               "Steam) y se han dejado como estaban:\n\n" + "\n".join(r.kept_changed[:30]))
            self.changed()

        def fail(e):
            task.done()
            self.win.error("No se pudieron aplicar los mods", e)
            self.changed()
        run_async(manager.apply, done, fail, self.ctx, task.update)

    def confirm_restore(self) -> None:
        d = Adw.AlertDialog(heading="¿Restaurar el juego sin mods?",
                            body="Se quitan del juego todos los archivos que puso Crisol y se devuelven los "
                                 "originales. Los mods siguen instalados en Crisol para volver a aplicarlos.")
        d.add_response("cancel", "Cancelar")
        d.add_response("restore", "Restaurar")
        d.set_response_appearance("restore", Adw.ResponseAppearance.DESTRUCTIVE)
        d.connect("response", lambda _d, r: r == "restore" and self.restore())
        d.present(self.win)

    def restore(self) -> None:
        task = self.win.taskbar.add(f"Restaurando {self.game.name}")

        def done(r):
            task.done()
            self.win.toast(f"Juego restaurado: {r.removed} archivos quitados, {r.restored} originales devueltos", 5)
            if r.kept_changed:
                self.win.error("Archivos cambiados por otro programa",
                               "No se han tocado porque ya no eran los que puso Crisol:\n\n"
                               + "\n".join(r.kept_changed[:30]))
            self.changed()

        def fail(e):
            task.done()
            self.win.error("No se pudo restaurar", e)
        run_async(manager.restore, done, fail, self.ctx, task.update)

    # ---------- perfiles ----------
    def _profile_selected(self, dd, _p):
        if getattr(self, "_loading_profiles", False):
            return
        item = dd.get_selected_item()
        if item and item.get_string() != self.ctx.state.active:
            self.ctx.state.switch(item.get_string())
            self.changed()
            self.win.toast("Perfil cambiado. Pulsa «Aplicar mods» para llevarlo al juego.")

    def _ask_name(self, heading: str, initial: str, cb) -> None:
        d = Adw.AlertDialog(heading=heading)
        entry = Gtk.Entry(text=initial, activates_default=True)
        d.set_extra_child(entry)
        d.add_response("cancel", "Cancelar")
        d.add_response("ok", "Aceptar")
        d.set_default_response("ok")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            name = entry.get_text().strip()
            if r == "ok" and name:
                if name in self.ctx.state.profiles and name != initial:
                    self.win.error("Nombre repetido", f"Ya hay un perfil «{name}».")
                    return
                cb(name)
        d.connect("response", resp)
        d.present(self.win)

    def profile_new(self) -> None:
        def make(name):
            self.ctx.state.add_profile(name, copy_from=self.ctx.state.active)
            self.ctx.state.switch(name)
            self.changed()
        self._ask_name("Nuevo perfil", f"{self.ctx.state.active} (copia)", make)

    def profile_rename(self) -> None:
        old = self.ctx.state.active

        def ren(name):
            self.ctx.state.rename_profile(old, name)
            self.changed()
        self._ask_name("Renombrar perfil", old, ren)

    def profile_delete(self) -> None:
        st = self.ctx.state
        if len(st.profiles) == 1:
            self.win.error("No se puede borrar", "Es el único perfil del juego.")
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
        handle.set_tooltip_text("Arrastra para cambiar el orden")
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
                                     f"{len(m.files)} archivos") if x)
        meta.append(Gtk.Label(label=sub, xalign=0, css_classes=["dim-label", "caption"],
                              ellipsize=Pango.EllipsizeMode.END))
        if m.update_available:
            meta.append(Gtk.Label(label=f"Actualización: v{m.latest_version}", css_classes=["chip", "accent"]))
        if wins and enabled:
            meta.append(Gtk.Label(label=f"Gana {wins}", css_classes=["chip", "success"],
                                  tooltip_text="Archivos en los que este mod pisa a otro"))
        if loses and enabled:
            meta.append(Gtk.Label(label=f"Pierde {loses}", css_classes=["chip", "warning"],
                                  tooltip_text="Archivos en los que otro mod (más abajo) pisa a este"))
        if missing:
            names = ", ".join(r.get("name") or str(r.get("mod_id")) for r in missing)
            internal = [r for r in missing if not r.get("external")]
            meta.append(Gtk.Label(label="Faltan requisitos" if internal else "Requisitos externos",
                                  css_classes=["chip", "error" if internal else "warning"],
                                  tooltip_text=f"Necesita: {names}"))
        if m.verified == "md5":
            meta.append(Gtk.Image(icon_name="emblem-ok-symbolic", tooltip_text="Descarga verificada con md5",
                                  css_classes=["dim-label"]))
        col.append(meta)
        box.append(col)
        sw = Gtk.Switch(active=enabled, valign=Gtk.Align.CENTER, tooltip_text="Activar o desactivar")
        sw.connect("notify::active", lambda s, _p: page.set_enabled(m.uid, s.get_active()))
        box.append(sw)
        menu = Gio.Menu()
        if m.update_available:
            menu.append("Actualizar…", "row.update")
        menu.append("Subir", "row.up")
        menu.append("Bajar", "row.down")
        menu.append("Al principio", "row.top")
        menu.append("Al final", "row.bottom")
        if m.archive:
            menu.append("Reinstalar desde la descarga", "row.reinstall")
        if m.mod_id:
            menu.append("Ver en Nexus Mods", "row.page")
        menu.append("Desinstalar", "row.remove")
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
        src.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(m.uid))
        src.connect("drag-begin", self._drag_begin)
        self.add_controller(src)
        tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        tgt.connect("motion", self._motion)
        tgt.connect("leave", lambda *_: self._mark(None))
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
        super().__init__(title=f"Ajustes de {page.game.name}")
        self.page = page
        st = page.ctx.state
        pg = Adw.PreferencesPage()
        g1 = Adw.PreferencesGroup(title="Nexus Mods",
                                  description="Juego de Nexus Mods del que se buscan los mods. Se detecta por el "
                                              "nombre; corrígelo si no es el correcto (el nombre corto de la "
                                              "dirección: nexusmods.com/<b>nombre</b>).")
        self.domain = Adw.EntryRow(title="Dominio en Nexus Mods", text=st.nexus_domain or "",
                                   show_apply_button=True)
        self.domain.connect("apply", self._apply_domain)
        g1.add(self.domain)
        pg.add(g1)
        g2 = Adw.PreferencesGroup(title="Tipo de juego",
                                  description="Cómo se colocan los mods y cómo se expresa el orden. Al cambiarlo "
                                              "se recoloca lo ya instalado (sin volver a descargar).")
        ids = [None] + list(LAYOUTS)
        labels = ["Automático"] + [LAYOUTS[i].label for i in LAYOUTS]
        self.layout = Adw.ComboRow(title="Tipo", model=Gtk.StringList.new(labels),
                                   selected=ids.index(st.layout) if st.layout in ids else 0)
        self.layout_ids = ids
        self.layout.connect("notify::selected", self._apply_layout)
        g2.add(self.layout)
        for i in LAYOUTS:
            r = Adw.ActionRow(title=LAYOUTS[i].label, subtitle=LAYOUTS[i].description)
            r.add_css_class("property")
            g2.add(r)
        pg.add(g2)
        self.add(pg)

    def _apply_domain(self, row):
        dom = row.get_text().strip().lower().strip("/").split("/")[-1]
        ctl = self.page.ctl

        def check():
            return ctl.nexus.game_id(dom) if dom else 0

        def ok(_gid):
            ctl.set_domain(self.page.game, dom)
            self.page.settings_changed()
            self.add_toast(Adw.Toast(title="Juego de Nexus actualizado" if dom else "Juego desenlazado de Nexus"))

        def bad(e):
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(e))))
        run_async(check, ok, bad)

    def _apply_layout(self, row, _p):
        st = self.page.ctx.state
        st.layout = self.layout_ids[row.get_selected()]
        failed = manager.remap(self.page.ctx)
        if failed:
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(
                "No encajan en este tipo: " + ", ".join(failed)), timeout=8))
        self.page.settings_changed()
