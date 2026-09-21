#!/usr/bin/env python3
"""Pre-commit проверки проекта Helpdesk.

Запускается git-хуком .git/hooks/pre-commit перед каждым коммитом.
Задача — не дать закоммитить код, который сломан или рассинхронизирован с
документацией памяти.

Проверки:
  1. Слой памяти: ссылки, блоки кода, обязательные разделы (scripts/check_docs.py)
  2. Синтаксис Python: приложение импортируется
  3. Синтаксис JavaScript: frontend/app.js парсится
  4. STATE.md обновлён, если менялся код

Обход (когда хук мешает по делу):
    git commit --no-verify -m "..."
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"

# код, правка которого требует обновить STATE.md
CODE_GLOBS = ("backend/app/**/*.py", "frontend/*.js", "frontend/*.css", "frontend/*.html")
STATE_FILE = "docs/memory/STATE.md"

errors: list[str] = []
warnings: list[str] = []


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout


def staged_files() -> list[str]:
    out = git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    return [line.strip() for line in out.splitlines() if line.strip()]


def check_docs() -> None:
    """Слой памяти: ссылки, блоки кода, обязательные разделы."""
    script = ROOT / "scripts" / "check_docs.py"
    result = subprocess.run(
        [str(VENV_PYTHON), str(script)], cwd=ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        tail = "\n".join(result.stdout.splitlines()[-12:])
        errors.append(f"check_docs.py сообщает о проблемах:\n{tail}")


def check_python_imports() -> None:
    """Приложение должно импортироваться — иначе сервис не стартует."""
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", "from backend.app.main import app"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-10:])
        errors.append(f"Импорт приложения падает:\n{tail}")


def check_js_syntax() -> None:
    """Фронтенд должен парситься — иначе интерфейс мёртв для всех."""
    js = ROOT / "frontend" / "app.js"
    if not js.exists():
        return
    result = subprocess.run(
        ["node", "--check", str(js)], capture_output=True, text=True
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-8:])
        errors.append(f"frontend/app.js не парсится:\n{tail}")


def check_state_updated(staged: list[str]) -> None:
    """Правило проекта: правка кода без записи в STATE.md — потерянный контекст.

    Это единственное место, где написано «где мы сейчас». Если его не обновлять,
    следующий агент после обнуления контекста не поймёт, что происходило.
    """
    code_changed = any(
        any(pathlib.PurePath(f).match(glob) for glob in CODE_GLOBS)
        for f in staged
    )
    state_changed = STATE_FILE in staged

    if code_changed and not state_changed:
        warnings.append(
            "Изменён код, но docs/memory/STATE.md не тронут.\n"
            "    Коммит пройдёт, но следующий агент не узнает, что изменилось.\n"
            "    Обнови STATE.md или обойди проверку: git commit --no-verify"
        )


def main() -> int:
    staged = staged_files()

    if not staged:
        print("pre-commit: нет staged-изменений, пропускаю проверки")
        return 0

    print(f"pre-commit: проверяю {len(staged)} файл(ов)")

    check_docs()
    check_python_imports()
    check_js_syntax()
    check_state_updated(staged)

    if errors:
        print("\n" + "=" * 64)
        print("КОММИТ ОСТАНОВЛЕН — надо исправить:")
        print("=" * 64)
        for e in errors:
            print(f"\n  ✗ {e}")
        print("\n" + "=" * 64)
        print("После исправления: git add -A && git commit")
        print("Обойти проверки:    git commit --no-verify -m \"...\"")
        print("=" * 64)
        return 1

    for w in warnings:
        print(f"\n  ⚠ {w}")

    print("pre-commit: все проверки пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
