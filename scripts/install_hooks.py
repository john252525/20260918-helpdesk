#!/usr/bin/env python3
"""Установка git-хуков проекта.

Запуск:  ./.venv/bin/python scripts/install_hooks.py

Хуки кладутся в .git/hooks/ — эта папка не попадает в репозиторий, поэтому
установку приходится делать на каждой машине отдельно. Скрипт идемпотентен:
повторный запуск перезапишет хуки той же версией.
"""
from __future__ import annotations

import pathlib
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOKS_DIR = ROOT / ".git" / "hooks"

PRE_COMMIT = """#!/usr/bin/env bash
# Запускает проверки проекта перед коммитом.
# Настроен скриптом scripts/install_hooks.py, правится там же.
exec "$(git rev-parse --show-toplevel)/.venv/bin/python" \\
     "$(git rev-parse --show-toplevel)/scripts/pre_commit.py"
"""


def install(name: str, content: str) -> None:
    if not HOOKS_DIR.parent.exists():
        print(f"  {ROOT} не является git-репозиторием")
        raise SystemExit(1)

    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    path = HOOKS_DIR / name
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  {'обновлён' if existed else 'установлен'}: .git/hooks/{name}")


def main() -> int:
    print("Установка git-хуков\n")
    install("pre-commit", PRE_COMMIT)
    print(
        "\nГотово. Хук срабатывает при каждом коммите.\n"
        "Обойти проверки, если мешают:  git commit --no-verify\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
