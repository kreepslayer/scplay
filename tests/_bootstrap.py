#!/usr/bin/env python3
"""Общий запуск тестов scplay на нужном интерпретаторе.

Тесты требуют numpy, textual, yt-dlp и python-mpv-jsonipc. Они стоят либо в
окружении самого scplay, либо в `venv` проекта — системный python их обычно не
видит. Поэтому при запуске не тем интерпретатором тесты перезапускают себя через
`venv/bin/python` и только потом импортируют scplay.

Подключается первой строкой теста:

    from _bootstrap import ensure_deps, ROOT   # noqa: F401
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
VENV_PY = ROOT / "venv" / "bin" / "python"
_FLAG = "SCPLAY_TESTS_REEXEC"

REQUIRED = ("numpy", "textual", "rich", "yt_dlp", "python_mpv_jsonipc")


def missing_deps() -> list[str]:
    return [name for name in REQUIRED if importlib.util.find_spec(name) is None]


def ensure_deps() -> None:
    """Проверяет зависимости и при необходимости перезапускает тест другим python."""
    # scplay.py и scplay_media.py лежат в корне проекта
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    missing = missing_deps()
    if not missing:
        return

    if os.environ.get(_FLAG) != "1" and VENV_PY.exists():
        os.environ[_FLAG] = "1"
        print(
            f"[тесты] в {Path(sys.executable).name} нет {', '.join(missing)} — "
            f"перезапускаю через {VENV_PY}",
            file=sys.stderr,
        )
        os.execv(str(VENV_PY), [str(VENV_PY), *sys.argv])

    hint = (
        f"\nНе хватает пакетов: {', '.join(missing)}."
        f"\nЗапусти тесты интерпретатором scplay:\n"
        f"    {VENV_PY} {Path(sys.argv[0]).name}\n"
        f"или установи зависимости:  python3 -m pip install -e '{ROOT}[mpris]'"
    )
    sys.exit(hint)
