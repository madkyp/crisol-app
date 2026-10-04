"""Extracción de archivos de mods con bsdtar (zip, 7z, rar, tar…)."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path


class ArchiveError(Exception):
    pass


ARCHIVE_EXTS = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.zst")


def is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_EXTS)


def extract(archive: Path, dest: Path) -> None:
    """Extrae en dest (vacío). bsdtar rechaza rutas absolutas y «..» por defecto."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if not is_archive(archive):
        # Un .pak u otro archivo suelto: se trata como mod de un solo archivo.
        shutil.copy2(archive, dest / archive.name)
        return
    r = subprocess.run(["bsdtar", "-x", "--no-same-owner", "-f", str(archive), "-C", str(dest)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise ArchiveError(f"No se pudo extraer {archive.name}: {r.stderr.strip() or 'archivo dañado'}")
    # Permisos razonables (algunos zip traen carpetas sin permiso de lectura).
    for p in dest.rglob("*"):
        try:
            p.chmod(0o755 if p.is_dir() else 0o644)
        except OSError:
            pass


def md5sum(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()
