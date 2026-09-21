#!/usr/bin/env python3
"""Тесты валидатора слоя памяти.

Работают на копии проекта во временной папке, а не на боевых документах —
см. ADR-017 и ADR-018.

Запуск:  ./.venv/bin/python scripts/test_check_docs.py
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
CHECKER = PROJECT_ROOT / "scripts" / "check_docs.py"
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

# что копируется в песочницу: всё, на что ссылаются документы
COPY = ("AGENTS.md", "docs", "scripts", "backend", "frontend", "deploy", "run.sh")


def make_sandbox():
    """Копирует проект во временный каталог."""
    tmp = tempfile.TemporaryDirectory(prefix="helpdesk-docs-")
    root = pathlib.Path(tmp.name)
    for entry in COPY:
        src = PROJECT_ROOT / entry
        if not src.exists():
            continue
        dst = root / entry
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return root, tmp


def run(root):
    """Запускает валидатор на копии, возвращает код выхода и вывод."""
    r = subprocess.run(
        [str(PYTHON), str(CHECKER), "--root", str(root), "--quiet"],
        capture_output=True, text=True,
    )
    return r.returncode, r.stdout + r.stderr


def main() -> int:
    passed = failed = 0
    root, guard = make_sandbox()
    state = root / "docs" / "memory" / "STATE.md"
    original = state.read_text(encoding="utf-8")

    def case(label, expected):
        nonlocal passed, failed
        code, out = run(root)
        ok = code == expected
        if ok:
            passed += 1
            print(f"  [OK ] {label}")
            return
        failed += 1
        print(f"  [!!!] {label} — код {code}, ожидался {expected}")
        for line in out.splitlines()[-4:]:
            print(f"        {line}")

    try:
        case("чистый проект проходит", 0)

        state.write_text(original + chr(10) + "```text" + chr(10)
                         + "backend/app/ghost/not_real.py" + chr(10) + "```" + chr(10),
                         encoding="utf-8")
        case("пример пути в код-блоке пропущен", 0)

        state.write_text(original + chr(10) + "Битая: `backend/app/routers/ghost.py`." + chr(10),
                         encoding="utf-8")
        case("битая ссылка со слэшем блокирует", 1)

        state.write_text(original + chr(10) + "Упоминание: `some_unknown.py`." + chr(10),
                         encoding="utf-8")
        case("короткое имя в прозе проходит", 0)

        state.write_text(original + chr(10) + "```text" + chr(10) + "без закрытия" + chr(10),
                         encoding="utf-8")
        case("незакрытый блок кода ловится", 1)

        state.write_text(original.replace("## Блокеры", "## Иное"), encoding="utf-8")
        case("пропажа раздела ловится", 1)
    finally:
        guard.cleanup()

    print()
    print(f"Пройдено: {passed}, провалено: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
