"""Lectura y escritura atómica de JSON."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        # Archivo dañado: se aparta para no perderlo y se sigue con el valor por defecto.
        try:
            path.rename(path.with_suffix(path.suffix + ".bad"))
        except OSError:
            pass
        return default


def save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    os.replace(tmp, path)
