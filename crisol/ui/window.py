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
from ..i18n import _

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
            b = Gtk.Button(icon_name="process-stop-symbolic", tooltip_text=_('Cancelar'), valign=Gtk.Align.CENTER,
                           css_classes=["flat", "circular"])
            b.connect("clicked", lambda *_u: self.cancel.set())
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
            badges = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, halign=Gtk.Align.END,
                             valign=Gtk.Align.START, margin_top=8, margin_end=8)
            badges.append(Gtk.Label(label=_("{0}/{1} mods").format(len(st.profile.enabled), n), css_classes=["chip", "accent"],
                                    halign=Gtk.Align.END))
            upd = sum(1 for m in st.mods.values() if m.update_available)
            if upd:
                badges.append(Gtk.Label(label=(_("{0} actualizaciones") if upd > 1 else _("1 actualización")).format(upd),
                                        css_classes=["chip", "success"], halign=Gtk.Align.END))
            cover.add_overlay(badges)
        self.append(cover)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=12, margin_end=12,
                       margin_top=10, margin_bottom=12)
        name = Gtk.Label(label=game.name, xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=18,
                         css_classes=["card-name"], tooltip_text=game.name)
        meta = Gtk.Box(spacing=6)
        meta.append(Gtk.Label(label="Steam" if game.source == "steam" else "Umbral", css_classes=["chip", game.source]))
        if not ctl.compatible(game):
            meta.append(Gtk.Label(label=_('Sin Nexus'), css_classes=["chip"]))
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
        self.search = Gtk.SearchEntry(placeholder_text=_('Buscar juego…'), width_chars=26)
        self.search.connect("search-changed", lambda *_u: self.grid.invalidate_filter())
        hb.set_title_widget(self.search)
        menu = Gio.Menu()
        menu.append(_('Preferencias'), "win.prefs")
        menu.append(_('Volver a buscar juegos'), "win.rescan")
        menu.append(_('Acerca de {0}').format(APP_NAME), "win.about")
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text=_('Menú')))
        tv.add_top_bar(hb)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_start=32, margin_end=32,
                       margin_top=20, margin_bottom=24)
        head = Gtk.Box(spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        titles.append(Gtk.Label(label=_('Biblioteca'), xalign=0, css_classes=["library-title"]))
        self.sub = Gtk.Label(label=_('Buscando juegos…'), xalign=0, css_classes=["library-sub"])
        titles.append(self.sub)
        head.append(titles)
        self.source = Gtk.DropDown.new_from_strings([_('Todos'), "Steam", "Umbral"])
        self.source.set_valign(Gtk.Align.CENTER)
        self.source.connect("notify::selected", lambda *_u: self.grid.invalidate_filter())
        head.append(self.source)
        self.show_all = Gtk.ToggleButton(label=_('Mostrar todos'), valign=Gtk.Align.CENTER,
                                         active=self.ctl.cfg.show_all_games,
                                         tooltip_text=_('Mostrar también los juegos que no tienen mods en Nexus Mods'))
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
        self.empty = Adw.StatusPage(icon_name="applications-games-symbolic", title=_('No hay juegos compatibles'),
                                    description=_('No se ha encontrado ningún juego de Steam o Umbral con mods en Nexus Mods. Pulsa «Mostrar todos» para verlos igualmente.'))
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
        self.sub.set_label(_("{0} juegos detectados · {1} con mods en Nexus Mods").format(len(games), compat))
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
        self._waiting: dict[tuple[str, int, int], Task] = {}
        self._queue: dict | None = None   # colección en instalación: {game, domain, name, items, index}
        self._scanned = False
        self._open_game = open_game
        for name, cb in (("prefs", self.show_prefs), ("rescan", lambda: self.rescan()), ("about", self.show_about)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, cb=cb: cb())
            self.add_action(a)
        self.connect("close-request", self._on_close)
        self.rescan()
        run_async(self.ctl.validate_account, lambda _r: self._account_checked())
        from .. import selfupdate
        run_async(selfupdate.latest, self._new_version)

    def _on_close(self, *_u):
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
        d.add_response("ok", _('Aceptar'))
        d.present(self)

    def rescan(self) -> None:
        self.library.loading()

        def done(_games):
            self._scanned = True
            self.library.populate()
            self._auto_check_updates()
            g = next((g for g in self.ctl.games if g.key == self._open_game), None)
            if g:
                self._open_game = None
                self.open_game(g)
            for u in self._pending_nxm:
                self.handle_nxm(u)
            self._pending_nxm.clear()
        run_async(self.ctl.scan, done, lambda e: self.error(_('No se pudieron leer los juegos'), e))

    def _new_version(self, info: dict | None) -> None:
        """Hay una versión nueva de Crisol en GitHub: aviso con «Instalar»."""
        if not info:
            return
        toast = Adw.Toast(title=GLib.markup_escape_text(_("Hay una versión nueva de Crisol: {0}").format(info["version"])),
                          timeout=0, button_label=_("Instalar") if info.get("asset") else _("Ver"))
        toast.connect("button-clicked", lambda *_a: self._install_version(info))
        self.toasts.add_toast(toast)

    def _install_version(self, info: dict) -> None:
        import subprocess
        from .. import selfupdate
        if not info.get("asset"):
            Gtk.UriLauncher.new(info["url"]).launch(self, None, None)
            return
        task = self.taskbar.add(_("Descargando Crisol {0}").format(info["version"]))

        def work():
            path = selfupdate.download(info["asset"])
            # pkexec pide la contraseña con la ventana de polkit; pacman instala el paquete.
            r = subprocess.run(["pkexec", "pacman", "-U", "--noconfirm", path], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip()[-600:] or _("pacman no pudo instalar el paquete"))
            return path

        def done(_path):
            task.done()
            self.error(_("Crisol {0} instalado").format(info["version"]),
                       _("Cierra y vuelve a abrir Crisol para usar la versión nueva."))

        def fail(e):
            task.done()
            self.error(_("No se pudo instalar la versión nueva"), e)
        run_async(work, done, fail)

    def _auto_check_updates(self) -> None:
        """Al arrancar, como mucho cada 12 h por juego: ¿hay versiones nuevas de los mods instalados?"""
        import time
        stale = [g for g in self.ctl.games if manager.context(g).state.mods
                 and time.time() - manager.context(g).state.updates_checked > 12 * 3600]
        if not stale:
            return

        def work():
            total = 0
            for g in stale:
                try:
                    total += manager.check_updates(manager.context(g), self.ctl.nexus)
                except Exception as e:  # noqa: BLE001 — sin red no pasa nada, se reintenta otro día
                    log.info("no se pudieron comprobar actualizaciones de %s: %s", g.name, e)
            return total

        def done(total):
            self.refresh_library()
            page = self.current_game_page()
            if page:
                page.refresh_installed()
            if total:
                self.toast(_('Hay {0} mods con actualización').format(total), 6)
        run_async(work, done)

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
            self.error(_('Enlace no válido'), e)
            return
        games = self.ctl.games_for_domain(link.domain)
        if not games:
            self.error(_('Juego no encontrado'),
                       _('El enlace es de «{0}», pero no hay ningún juego de Steam o Umbral instalado enlazado con ese juego de Nexus Mods.').format(link.domain))
            return
        if not self.ctl.nexus.api_key:
            self.error(_('Falta la API key de Nexus Mods'),
                       _('Para descargar hace falta tu API key personal. Ponla en Preferencias → Nexus Mods y vuelve a pulsar «Mod Manager Download» en la web.'))
            return
        game = games[0]
        if len(games) > 1:
            page = self.current_game_page()
            if page and page.game in games:
                game = page.game
        self.install_nxm(game, link)

    def wait_for_nxm(self, game: Game, domain: str, mod_id: int, file_id: int, name: str) -> None:
        """Aviso mientras el usuario completa la descarga en la web (sin Premium)."""
        key = (domain, mod_id, file_id)
        if key in self._waiting:
            return
        task = self.taskbar.add(_('Esperando a la web: pulsa «Slow download» para «{0}»').format(name), cancellable=True)
        task.msg.set_label(_('el navegador pasará el enlace a Crisol'))
        self._waiting[key] = task

        def check():
            if task.cancel.is_set():
                self._waiting.pop(key, None)
                task.done()
                if self._queue and self._queue_key() == key:
                    self.toast(_('Instalación de la colección «{0}» detenida').format(self._queue['name']))
                    self._queue = None
                return False
            return key in self._waiting
        GLib.timeout_add(500, check)

    def install_nxm(self, game: Game, link) -> None:
        waiting = self._waiting.pop((link.domain, link.mod_id, link.file_id), None)
        if waiting:
            waiting.done()
        ctx = manager.context(game)
        task = self.taskbar.add(_('Descargando mod {0} para {1}').format(link.mod_id, game.name), cancellable=True)

        def work():
            with manager.game_lock(game):
                from .fomod_dialog import make_chooser
                return manager.install_from_nxm(ctx, self.ctl.nexus, link, task.update, task.cancel,
                                                chooser=make_chooser(self, game.install_dir))

        in_queue = bool(self._queue) and self._queue_key() == (link.domain, link.mod_id, link.file_id)

        def done(rec):
            task.done()
            if not self.ctl.cfg.keep_archives:
                manager.delete_archive(ctx, rec.uid)
            if not in_queue:
                self.toast(_('«{0}» instalado. Pulsa «Aplicar» para llevarlo al juego.').format(rec.name), 6)
            page = self.current_game_page(game)
            if page:
                page.refresh_installed()
            self.refresh_library()
            if in_queue:
                self._queue_next(advance=True)

        def fail(e):
            task.done()
            if in_queue:
                self.error(_('Colección «{0}» detenida').format(self._queue['name']), e)
                self._queue = None
                return
            if isinstance(e, manager.InstallCancelled):
                self.toast(_('Instalación cancelada'))
            elif task.cancel.is_set():
                self.toast(_('Descarga cancelada. Vuelve a pulsar «Slow download» y continuará donde se quedó.'), 6)
            else:
                self.error(_('No se pudo instalar el mod'), e)
        run_async(work, done, fail)

    # ---------- colecciones: instalar varios mods en orden ----------
    def install_collection(self, game: Game, domain: str, name: str, items: list) -> None:
        """items: CollectionMod en el orden de la colección (los que se quieren instalar)."""
        if self._queue:
            self.error(_('Ya hay una colección instalándose'), _('Espera a que termine «{0}».').format(self._queue['name']))
            return
        if not self.ctl.nexus.api_key:
            self.error(_('Falta la API key de Nexus Mods'), _('Ponla en Preferencias → Nexus Mods.'))
            return
        self._queue = {"game": game, "domain": domain, "name": name, "items": items, "index": 0}
        self._queue_next(advance=False)

    def _queue_key(self):
        q = self._queue
        it = q["items"][q["index"]]
        return (q["domain"], it.mod_id, it.file_id)

    def _queue_next(self, advance: bool) -> None:
        from ..providers.nexus import Nexus, NxmLink
        q = self._queue
        if not q:
            return
        if advance:
            q["index"] += 1
        if q["index"] >= len(q["items"]):
            self.toast(_('«{0}»: {1} mods instalados. Pulsa «Aplicar» para llevarlos al juego.').format(q['name'], len(q['items'])), 8)
            self._queue = None
            return
        it = q["items"][q["index"]]
        n, total = q["index"] + 1, len(q["items"])
        if self.ctl.is_premium:
            self.install_nxm(q["game"], NxmLink(q["domain"], it.mod_id, it.file_id, None, None))
        else:
            Gtk.UriLauncher.new(Nexus.file_page(q["domain"], it.mod_id, it.file_id) + "&nmm=1").launch(self, None, None)
            self.wait_for_nxm(q["game"], q["domain"], it.mod_id, it.file_id, f"{it.name} ({n}/{total})")

    # ---------- diálogos ----------
    def show_prefs(self) -> None:
        from .prefs import PrefsDialog
        PrefsDialog(self).present(self)

    def show_about(self) -> None:
        from .. import VERSION
        d = Adw.AboutDialog(application_name=APP_NAME, version=VERSION, developer_name="madky",
                            application_icon="dev.madky.Crisol", license_type=Gtk.License.MIT_X11,
                            comments=_('Gestor de mods para juegos de Steam y Umbral, con Nexus Mods como fuente.'))
        d.present(self)
