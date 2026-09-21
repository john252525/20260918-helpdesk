#!/usr/bin/env python3
"""Проверка слоя памяти.

Ловит три класса ошибок, которые встречались при работе над проектом:
  * битые ссылки на файлы кода (путаница относительных путей)
  * незакрытые или небрежно оформленные блоки кода
  * отсутствие обязательных разделов в документах

Запуск:  ./.venv/bin/python scripts/check_docs.py
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = sorted([ROOT / "AGENTS.md", *ROOT.glob("docs/memory/**/*.md")])

errors: list[str] = []


def check_links() -> int:
    """Каждая ссылка должна разрешаться от корня, от папки документа или от backend/app."""
    pattern = re.compile(r"`([A-Za-z0-9_./-]+\.(?:md|py|js|html|css))`")
    seen = 0
    for md in DOCS:
        for link in pattern.findall(md.read_text(encoding="utf-8")):
            seen += 1
            candidates = [
                ROOT / link,
                md.parent / link,
                ROOT / "backend" / "app" / link,
            ]
            if not any(c.exists() for c in candidates):
                errors.append(f"{md.relative_to(ROOT)}: битая ссылка `{link}`")
    return seen


def check_code_blocks() -> int:
    """Блоки должны быть закрыты и не заканчиваться пустой строкой."""
    blocks = 0
    for md in DOCS:
        lines = md.read_text(encoding="utf-8").splitlines()
        opened: int | None = None
        for i, line in enumerate(lines):
            if not line.strip().startswith("```"):
                continue
            if opened is None:
                opened = i
                continue
            blocks += 1
            if i > 0 and not lines[i - 1].strip():
                errors.append(f"{md.relative_to(ROOT)}:{i}: пустая строка перед закрывающим ```")
            opened = None
        if opened is not None:
            errors.append(f"{md.relative_to(ROOT)}: незакрытый блок кода на строке {opened + 1}")
    return blocks


def check_structure() -> int:
    """Ключевые разделы не должны пропадать при правках."""
    required = {
        "AGENTS.md": ["Порядок чтения", "Проверяй ПЕРЕД", "После завершения итерации"],
        "docs/memory/PROJECT.md": ["Что это", "Стек", "Архитектура"],
        "docs/memory/STATE.md": ["Работает", "Следующее", "Блокеры"],
        "docs/memory/DECISIONS.md": ["ADR-"],
        "docs/memory/CONVENTIONS.md": ["Проверка перед перезапуском"],
        "docs/memory/BACKLOG.md": ["Продукт", "Интерфейс"],
        "docs/memory/GLOSSARY.md": ["Мультитенант", "Метрики"],
    }
    checked = 0
    for rel, markers in required.items():
        path = ROOT / rel
        if not path.exists():
            errors.append(f"отсутствует документ {rel}")
            continue
        checked += 1
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            if marker not in text:
                errors.append(f"{rel}: пропал раздел «{marker}»")
    return checked


def check_iterations() -> int:
    """Имя файла итерации сортируется и содержит дату; шапка единообразна."""
    name_re = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{3}-[a-z0-9-]+\.md$")
    iters = sorted((ROOT / "docs/memory/iterations").glob("*.md"))
    for md in iters:
        if not name_re.match(md.name):
            errors.append(f"итерация с неверным именем: {md.name}")
        head = md.read_text(encoding="utf-8").splitlines()[:6]
        if not any(line.startswith("# Итерация") for line in head):
            errors.append(f"{md.name}: нет заголовка «# Итерация NNN»")
    return len(iters)


def main() -> int:
    print(f"Проверка слоя памяти: {len(DOCS)} документов\n")
    links = check_links()
    blocks = check_code_blocks()
    docs = check_structure()
    iters = check_iterations()

    print(f"  ссылок на файлы   {links}")
    print(f"  блоков кода       {blocks}")
    print(f"  документов        {docs}")
    print(f"  итераций          {iters}\n")

    if errors:
        print(f"Проблемы ({len(errors)}):")
        for e in errors:
            print(f"  ✗ {e}")
        return 1

    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
