"""Las pruebas nunca usan las carpetas reales de Crisol: se fijan aquí, antes de importar nada de crisol."""
import os
import tempfile

_TMP = tempfile.TemporaryDirectory(prefix="crisol-tests-")
for _var, _sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache")):
    os.environ[_var] = os.path.join(_TMP.name, _sub)
