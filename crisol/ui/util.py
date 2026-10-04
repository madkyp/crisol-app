"""Utilidades de interfaz: tareas en hilos e imágenes remotas en caché."""
from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

from .. import USER_AGENT, paths

log = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="crisol-img")
_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_textures: dict[tuple, Gdk.Texture] = {}


def run_async(fn: Callable, on_done: Callable | None = None, on_error: Callable[[Exception], None] | None = None,
              *args, **kwargs) -> threading.Thread:
    """Ejecuta fn en un hilo y entrega el resultado (o el error) en el hilo de GTK."""
    def worker():
        try:
            res = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — se muestra en la interfaz
            log.exception("tarea fallida: %s", getattr(fn, "__name__", fn))
            if on_error:
                GLib.idle_add(on_error, e)
            return
        if on_done:
            GLib.idle_add(on_done, res)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def idle(fn: Callable, *args) -> None:
    """Llama a fn en el hilo de GTK (una sola vez)."""
    GLib.idle_add(lambda: (fn(*args), False)[1])


def _cover_crop(pb, w: int, h: int):
    pw, ph = pb.get_width(), pb.get_height()
    k = max(w / pw, h / ph)
    sw, sh = max(w, round(pw * k)), max(h, round(ph * k))
    pb = pb.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR)
    return pb.new_subpixbuf((sw - w) // 2, (sh - h) // 2, w, h)


def _contain_on_blur(pb, w: int, h: int):
    """La imagen entera, centrada sobre un fondo desenfocado y oscurecido de sí misma (sin recortar
    portadas que no tienen la proporción de la tarjeta, p. ej. cuadradas o apaisadas)."""
    bg = _cover_crop(pb, w, h)
    tiny = bg.scale_simple(max(1, w // 24), max(1, h // 24), GdkPixbuf.InterpType.BILINEAR)
    bg = tiny.scale_simple(w, h, GdkPixbuf.InterpType.BILINEAR)
    dark = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, w, h)
    dark.fill(0x101217FF)
    bg.composite(dark, 0, 0, w, h, 0, 0, 1, 1, GdkPixbuf.InterpType.BILINEAR, 170)
    pw, ph = pb.get_width(), pb.get_height()
    # Apaisadas: más grandes, recortando un 20 % de cada lado (el logo suele ir en el centro).
    k = min(h / ph, (1.6 * w / pw) if pw > ph else w / pw)
    fw, fh = max(1, round(pw * k)), max(1, round(ph * k))
    fg = pb.scale_simple(fw, fh, GdkPixbuf.InterpType.HYPER)
    if fw > w:
        fg = fg.new_subpixbuf((fw - w) // 2, 0, w, fh)
        fw = w
    x, y = (w - fw) // 2, (h - fh) // 2
    fg.composite(dark, x, y, fw, fh, x, y, 1, 1, GdkPixbuf.InterpType.NEAREST, 255)
    return dark


def _texture_from_file(path: Path, w: int, h: int, smart: bool = False) -> Gdk.Texture | None:
    """Imagen a exactamente w×h (o escalada a lo ancho si h == 0). Por defecto se recorta al centro;
    con smart, si la proporción es muy distinta, se muestra entera sobre un fondo desenfocado."""
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file(str(path))
    except GLib.Error:
        return None
    pw, ph = pb.get_width(), pb.get_height()
    if not h:
        h = max(1, round(ph * w / pw))
    ratio = (pw / ph) / (w / h)
    if smart and not 0.88 <= ratio <= 1.14:
        out = _contain_on_blur(pb, w, h)
    else:
        out = _cover_crop(pb, w, h)
    return Gdk.Texture.new_for_pixbuf(out)


def local_texture(path: Path | None, w: int, h: int = 0, smart: bool = False) -> Gdk.Texture | None:
    if not path:
        return None
    key = (str(path), w, h, smart)
    if key not in _textures:
        tex = _texture_from_file(path, w, h, smart)
        if tex is None:
            return None
        _textures[key] = tex
    return _textures[key]


def load_remote(picture: Gtk.Picture, url: str, w: int, h: int = 0) -> None:
    """Pone en picture la imagen de url (descargada una vez a ~/.cache/crisol/thumbs), a w×h."""
    if not url:
        return
    key = (url, w, h)
    if key in _textures:
        picture.set_paintable(_textures[key])
        return

    def fetch():
        dest = paths.THUMBS_DIR / (hashlib.sha1(url.encode()).hexdigest() + Path(url.split("?")[0]).suffix[:5])
        if not dest.exists():
            r = _session.get(url, timeout=20)
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
        return _texture_from_file(dest, w, h)

    def done(fut):
        try:
            tex = fut.result()
        except Exception as e:  # noqa: BLE001
            log.debug("imagen no disponible %s: %s", url, e)
            return
        if tex:
            def put():
                _textures[key] = tex
                picture.set_paintable(tex)
            idle(put)
    _pool.submit(fetch).add_done_callback(done)


def human_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} M".replace(".", ",")
    if n >= 1_000:
        return f"{n / 1_000:.1f} k".replace(".", ",")
    return str(n)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return str(n)


def placeholder(icon: str = "applications-games-symbolic", size: int = 64) -> Gtk.Widget:
    img = Gtk.Image.new_from_icon_name(icon)
    img.set_pixel_size(size)
    img.add_css_class("placeholder-icon")
    return img
