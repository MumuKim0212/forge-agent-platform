"""importing `app.models` alone must register every mapper.

`python -m app.seed` imports `app.models.template`/`user`, which loads the
package `__init__`; when that `__init__` skipped a model module, the first
query tripped `configure_mappers()` on a relationship naming a class that was
never imported (`PolicyVerdict`). The test session itself imports every model
through the app and the fixtures, so the check has to run in a fresh
interpreter to be able to fail.
"""

import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = BACKEND_ROOT / "app" / "models"


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_importing_app_models_alone_configures_every_mapper() -> None:
    result = _run("import app.models\nfrom sqlalchemy.orm import configure_mappers\nconfigure_mappers()\n")

    assert result.returncode == 0, result.stderr[-2000:]


def test_importing_app_models_imports_every_model_module() -> None:
    expected = sorted(
        f"app.models.{p.stem}" for p in MODELS_DIR.glob("*.py") if p.stem != "__init__"
    )
    # Guard against a vacuous glob: a missing directory would "pass" with [].
    assert len(expected) >= 9, expected

    result = _run(
        "import sys\n"
        "import app.models\n"
        f"missing = [m for m in {expected!r} if m not in sys.modules]\n"
        "print(missing)\n"
        "sys.exit(1 if missing else 0)\n"
    )

    assert result.returncode == 0, result.stdout + result.stderr[-2000:]
