"""Ventana principal: biblioteca → detalle del juego, barra de tareas y avisos."""
from __future__ import annotations

import logging
import threading

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from .. import APP_NAME, manager
from ..controller import Controller
from ..games import Game
from ..providers.base import ProviderError
from ..providers.nexus import parse_nxm
from .util import local_texture, placeholder, run_async

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- tareas en curso

class Task:
    def __init__(self, bar: "TaskBar", title: str, cancellable: bool = False):
        self.bar = bar
        self.cancel = threading.Event()
        self.row = Gtk.Box(spacing=12)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        top = Gtk.Box(spacing=8)
        self.title = Gtk.Label(label=title, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("heading")
        self.msg = Gtk.Label(xalign=1, css_classes=["dim-label", "caption"])
        top.append(self.title)
        top.append(self.msg)
        self.progress = Gtk.ProgressBar()
        col.append(top)
        col.append(self.progress)
        self.row.append(col)
        if cancellable:
            b = Gtk.Button(icon_name="process-stop-symbolic", tooltip_text="Cancelar", valign=Gtk.Align.CENTER,
                           css_classes=["flat", "circular"])
            b.connect("clicked", lambda *_: self.cancel.set())
            self.row.append(b)
        self._pulse = GLib.timeout_add(120, self._do_pulse)
        self._known = False

    def _do_pulse(self):
        if not self._known:
            self.progress.pulse()
        return True

    def update(self, frac: float, msg: str = "") -> None:
        """Seguro desde cualquier hilo."""
        def set_():
            self._known = True
            self.progress.set_fraction(max(0.0, min(1.0, frac)))
            if msg:
                self.msg.set_label(msg)
            return False
        GLib.idle_add(set_)

    def done(self) -> None:
        GLib.source_remove(self._pulse)
        self.bar.remove(self)


class TaskBar(Gtk.Revealer):
    def __init__(self):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["taskbar"])
        self.set_child(self.box)
        self.tasks: list[Task] = []

    def add(self, title: str, cancellable: bool = False) -> Task:
        t = Task(self, title, cancellable)
        self.tasks.append(t)
        self.box.append(t.row)
        self.set_reveal_child(True)
        return t

    def remove(self, t: Task) -> None:
        if t in self.tasks:
            self.tasks.remove(t)
            self.box.remove(t.row)
        self.set_reveal_child(bool(self.tasks))


# ---------------------------------------------------------------- tarjeta de juego

class GameCard(Gtk.Box):
    WIDTH, HEIGHT = 190, 285

    def __init__(self, ctl: Controller, game: Game):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["game-card"])
        self.game = game
        tex = local_texture(game.cover, self.WIDTH, self.HEIGHT) or local_texture(game.hero, self.WIDTH, self.HEIGHT)
        cover = Gtk.Overlay()
        frame = Gtk.Box(width_request=self.WIDTH, height_request=self.HEIGHT, css_classes=["cover"],
                        overflow=Gtk.Overflow.HIDDEN)
        if tex:
            pic = Gtk.Picture(paintable=tex, content_fit=Gtk.ContentFit.COVER, hexpand=True, vexpand=True)
            frame.append(pic)
        else:
            ph = placeholder(size=56)
            ph.set_hexpand(True)
            ph.set_vexpand(True)
            frame.append(ph)
        cover.set_child(frame)
        st = manager.context(game).state
        n = len(st.mods)
        if n:
            badge = Gtk.Label(label=f"{len(st.profile.enabled)}/{n} mods", css_classes=["chip", "accent"],
                              halign=Gtk.Align.END, valign=Gtk.Align.START, margin_top=8, margin_end=8)
            cover.add_overlay(badge)
        self.append(cover)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=12, margin_end=12,
                       margin_top=10, margin_bottom=12)
        name = Gtk.Label(label=game.name, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=18,
                         css_classes=["card-name"], tooltip_text=game.name)
        meta = Gtk.Box(spacing=6)
        meta.append(Gtk.Label(label="Steam" if game.source == "steam" else "Umbral", css_classes=["chip", game.source]))
        if not ctl.compatible(game):
            meta.append(Gtk.Label(label="Sin Nexus", css_classes=["chip"]))
        info.append(name)
        info.append(meta)
        self.append(info)


# ---------------------------------------------------------------- biblioteca

class LibraryPage(Adw.NavigationPage):
    def __init__(self, win: "MainWindow"):
        super().__init__(title=APP_NAME, tag="library")
        self.win = win
        self.ctl = win.ctl
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.search = Gtk.SearchEntry(placeholder_text="Buscar juego…", width_chars=26)
        self.search.connect("search-changed", lambda *_: self.grid.invalidate_filter())
        hb.set_title_widget(self.search)
        menu = Gio.Menu()
        menu.append("Preferencias", "win.prefs")
        menu.append("Volver a buscar juegos", "win.rescan")
        menu.append(f"Acerca de {APP_NAME}", "win.about")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menú"))
        tv.add_top_bar(hb)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=32, margin_end=32,
                       margin_top=20, margin_bottom=24)
        head = Gtk.Box(spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        titles.append(Gtk.Label(label="Biblioteca", xalign=0, css_classes=["library-title"]))
        self.sub = Gtk.Label(label="Buscando juegos…", xalign=0, css_classes=["library-sub"])
        titles.append(self.sub)
        head.append(titles)
        self.source = Gtk.DropDown.new_from_strings(["Todos", "Steam", "Umbral"])
        self.source.set_valign(Gtk.Align.CENTER)
        self.source.connect("notify::selected", lambda *_: self.grid.invalidate_filter())
        head.append(self.source)
        self.show_all = Gtk.ToggleButton(label="Mostrar todos", valign=Gtk.Align.CENTER,
                                         active=self.ctl.cfg.show_all_games,
                                         tooltip_text="Mostrar también los juegos que no tienen mods en Nexus Mods")
        self.show_all.connect("toggled", self._toggle_all)
        head.append(self.show_all)
        body.append(head)

        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False, column_spacing=18,
                                row_spacing=18, valign=Gtk.Align.START, max_children_per_line=12,
                                css_classes=["game-grid"], activate_on_single_click=True)
        self.grid.set_filter_func(self._filter)
        self.grid.connect("child-activated", lambda _g, child: self.win.open_game(child.get_child().game))
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.stack.add_named(Adw.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                         width_request=48, height_request=48), "loading")
        self.stack.add_named(self.grid, "grid")
        self.empty = Adw.StatusPage(icon_name="applications-games-symbolic", title="No hay juegos compatibles",
                                    description="No se ha encontrado ningún juego de Steam o Umbral con mods en "
                                                "Nexus Mods. Pulsa «Mostrar todos» para verlos igualmente.")
        self.stack.add_named(self.empty, "empty")
        body.append(self.stack)
        scroll = Gtk.ScrolledWindow(child=body, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        tv.set_content(scroll)
        self.set_child(tv)

    def _toggle_all(self, btn):
        self.ctl.cfg.show_all_games = btn.get_active()
        self.ctl.cfg.save()
        self.populate()

    def _filter(self, child) -> bool:
        g: Game = child.get_child().game
        src = self.source.get_selected()
        if src == 1 and g.source != "steam" or src == 2 and g.source != "umbral":
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in g.name.lower()

    def populate(self) -> None:
        self.grid.remove_all()
        games = self.ctl.games
        shown = [g for g in games if self.ctl.cfg.show_all_games or self.ctl.compatible(g)]
        for g in shown:
            self.grid.append(GameCard(self.ctl, g))
        compat = sum(1 for g in games if self.ctl.compatible(g))
        self.sub.set_label(f"{len(games)} juegos detectados · {compat} con mods en Nexus Mods")
        self.stack.set_visible_child_name("grid" if shown else "empty")

    def loading(self) -> None:
        self.stack.set_visible_child_name("loading")


# ---------------------------------------------------------------- ventana

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, ctl: Controller, open_game: str | None = None):
        super().__init__(application=app, title=APP_NAME, default_width=ctl.cfg.window_width,
                         default_height=ctl.cfg.window_height)
        self.add_css_class("crisol")
        self.ctl = ctl
        self.toasts = Adw.ToastOverlay()
        self.nav = Adw.NavigationView(vexpand=True)
        self.library = LibraryPage(self)
        self.nav.add(self.library)
        self.taskbar = TaskBar()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.nav)
        box.append(self.taskbar)
        self.toasts.set_child(box)
        self.set_content(self.toasts)
        self._pending_nxm: list[str] = []
        self._scanned = False
        self._open_game = open_game
        for name, cb in (("prefs", self.show_prefs), ("rescan", lambda: self.rescan()), ("about", self.show_about)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, cb=cb: cb())
            self.add_action(a)
        self.connect("close-request", self._on_close)
        self.rescan()
        run_async(self.ctl.validate_account, lambda _r: self._account_checked())

    def _on_close(self, *_):
        w, h = self.get_default_size()
        self.ctl.cfg.window_width, self.ctl.cfg.window_height = w, h
        self.ctl.cfg.save()
        if any(t for t in self.taskbar.tasks):
            log.warning("cerrando con tareas en curso")
        return False

    def toast(self, text: str, timeout: int = 4) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout))

    def error(self, title: str, err: Exception | str) -> None:
        d = Adw.AlertDialog(heading=title, body=str(err))
        d.add_response("ok", "Aceptar")
        d.present(self)

    def rescan(self) -> None:
        self.library.loading()

        def done(_games):
            self._scanned = True
            self.library.populate()
            g = next((g for g in self.ctl.games if g.key == self._open_game), None)
            if g:
                self._open_game = None
                self.open_game(g)
            for u in self._pending_nxm:
                self.handle_nxm(u)
            self._pending_nxm.clear()
        run_async(self.ctl.scan, done, lambda e: self.error("No se pudieron leer los juegos", e))

    def _account_checked(self) -> None:
        if self.ctl.account_error:
            self.toast(self.ctl.account_error, 8)
        page = self.nav.get_visible_page()
        if hasattr(page, "account_changed"):
            page.account_changed()

    def open_game(self, game: Game) -> None:
        from .game_page import GamePage
        self.nav.push(GamePage(self, game))

    def current_game_page(self, game: Game | None = None):
        from .game_page import GamePage
        page = self.nav.get_visible_page()
        if isinstance(page, GamePage) and (game is None or page.game.key == game.key):
            return page
        return None

    def refresh_library(self) -> None:
        self.library.populate()

    # ---------- enlaces nxm:// ----------
    def handle_nxm(self, uri: str) -> None:
        if not self._scanned:
            self._pending_nxm.append(uri)
            return
        try:
            link = parse_nxm(uri)
        except ProviderError as e:
            self.error("Enlace no válido", e)
            return
        games = self.ctl.games_for_domain(link.domain)
        if not games:
            self.error("Juego no encontrado",
                       f"El enlace es de «{link.domain}», pero no hay ningún juego de Steam o Umbral instalado "
                       "enlazado con ese juego de Nexus Mods.")
            return
        if not self.ctl.nexus.api_key:
            self.error("Falta la API key de Nexus Mods",
                       "Para descargar hace falta tu API key personal. Ponla en Preferencias → Nexus Mods "
                       "y vuelve a pulsar «Mod Manager Download» en la web.")
            return
        game = games[0]
        if len(games) > 1:
            page = self.current_game_page()
            if page and page.game in games:
                game = page.game
        self.install_nxm(game, link)

    def install_nxm(self, game: Game, link) -> None:
        ctx = manager.context(game)
        task = self.taskbar.add(f"Descargando mod {link.mod_id} para {game.name}", cancellable=True)

        def work():
            with manager.game_lock(game):
                return manager.install_from_nxm(ctx, self.ctl.nexus, link, task.update, task.cancel)

        def done(rec):
            task.done()
            self.toast(f"«{rec.name}» instalado. Pulsa «Aplicar» para llevarlo al juego.", 6)
            page = self.current_game_page(game)
            if page:
                page.refresh_installed()
            self.refresh_library()

        def fail(e):
            task.done()
            self.error("No se pudo instalar el mod", e)
        run_async(work, done, fail)

    # ---------- diálogos ----------
    def show_prefs(self) -> None:
        from .prefs import PrefsDialog
        PrefsDialog(self).present(self)

    def show_about(self) -> None:
        from .. import VERSION
        d = Adw.AboutDialog(application_name=APP_NAME, version=VERSION, developer_name="madky",
                            application_icon="dev.madky.Crisol", license_type=Gtk.License.MIT_X11,
                            comments="Gestor de mods para juegos de Steam y Umbral, con Nexus Mods como fuente.")
        d.present(self)
