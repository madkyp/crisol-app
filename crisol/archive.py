"""Extracción de archivos de mods con bsdtar (zip, 7z, rar, tar…)."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from .i18n import _


class ArchiveError(Exception):
    pass


ARCHIVE_EXTS = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.zst")


_MAGIC = [(b"PK\x03\x04", ".zip"), (b"7z\xbc\xaf\x27\x1c", ".7z"), (b"Rar!\x1a\x07", ".rar"),
          (b"\x1f\x8b", ".tar.gz"), (b"\xfd7zXZ\x00", ".tar.xz"), (b"\x28\xb5\x2f\xfd", ".tar.zst")]


def sniff(path: Path) -> str:
    """Extensión según el contenido (algunos archivos de Nexus llegan con un nombre sin extensión)."""
    try:
        with path.open("rb") as f:
            head = f.read(8)
    except OSError:
        return ""
    return next((ext for magic, ext in _MAGIC if head.startswith(magic)), "")


def is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_EXTS) or bool(sniff(path))


def extract(archive: Path, dest: Path) -> None:
    """Extrae en dest (vacío). bsdtar rechaza rutas absolutas y «..» por defecto."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if archive.is_dir():
        # Una carpeta (p. ej. los archivos incluidos en una colección de Nexus): se copia tal cual.
        shutil.copytree(archive, dest, dirs_exist_ok=True)
        return
    if not is_archive(archive):
        # Un .pak u otro archivo suelto: se trata como mod de un solo archivo.
        shutil.copy2(archive, dest / archive.name)
        return
    r = subprocess.run(["bsdtar", "-x", "--no-same-owner", "-f", str(archive), "-C", str(dest)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise ArchiveError(_('No se pudo extraer {0}: {1}').format(archive.name, r.stderr.strip() or _('archivo dañado')))
    # Permisos razonables (algunos zip traen carpetas sin permiso de lectura); se conserva el de ejecución.
    for p in dest.rglob("*"):
        try:
            mode = p.stat().st_mode
            p.chmod(0o755 if p.is_dir() else (0o755 if mode & 0o111 else 0o644))
        except OSError:
            pass


def md5sum(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()
