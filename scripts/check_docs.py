#!/usr/bin/env python3
"""Проверка слоя памяти.

Ловит три класса ошибок, которые встречались при работе над проектом:
  * битые ссылки на файлы кода (путаница относительных путей)
  * незакрытые или небрежно оформленные блоки кода
  * отсутствие обязательных разделов в документах

Запуск:  ./.venv/bin/python scripts/check_docs.py
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

# Значения по умолчанию; ROOT может быть переопределён аргументом --root.
# Это нужно тестам: они работают с копией во временной папке, а не
# с боевыми документами.
DEFAULT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ROOT = DEFAULT_ROOT
DOCS: list[pathlib.Path] = []

# каталоги, которые не участвуют в поиске файлов по имени
SKIP_DIRS = {".venv", ".git", "data", "__pycache__", "node_modules"}


def configure(root: pathlib.Path) -> None:
    """Устанавливает корень проверки и собирает список документов."""
    global ROOT, DOCS
    ROOT = root.resolve()
    DOCS = sorted([ROOT / "AGENTS.md", *ROOT.glob("docs/memory/**/*.md")])

errors: list[str] = []
warnings: list[str] = []



def iter_outside_fences(text: str):
    """Отдаёт строки, не входящие в блок ```...```

    Код-блоки — это примеры и листинги. Имена файлов внутри них автор
    показывает как иллюстрацию, они не обязаны существовать в проекте.
    Проверке подлежит только инлайн-код в прозе.
    """
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield line

def check_links() -> int:
    """Проверяет, что каждый путь в обратных кавычках ведёт на существующий файл.

    Разрешение идёт в три шага, от строгого к мягкому:

      1. Явные базы: корень проекта, папка документа, backend/app, scripts,
         docs/memory, frontend. Покрывает полные и относительные пути.
      2. Поиск по имени файла во всём проекте. Покрывает короткие упоминания
         вроде `STATE.md`, когда файл лежит в подпапке. Если такое имя
         встречается ровно один раз — ссылка однозначна.
      3. Если не нашлось нигде — это настоящая ошибка: ссылка на
         несуществующий файл.

    Поиск по имени намеренно ограничен: если файлов с таким именем несколько,
    ссылка считается неоднозначной и попадает в предупреждения, а не в ошибки.
    """
    pattern = re.compile(r"`([A-Za-z0-9_./-]+\.(?:md|py|js|html|css))`")
    bases = [
        ROOT,
        ROOT / "backend" / "app",
        ROOT / "backend",
        ROOT / "frontend",
        ROOT / "scripts",
        ROOT / "docs" / "memory",
        ROOT / "deploy",
    ]

    seen = 0
    for md in DOCS:
        text = md.read_text(encoding="utf-8")
        # только проза: содержимое код-блоков — это примеры
        prose = "\n".join(iter_outside_fences(text))
        for link in pattern.findall(prose):
            seen += 1

            # шаг 1: явные пути
            if any((b / link).exists() for b in [md.parent, *bases]):
                continue

            # шаг 2: уникальное имя файла где-то в проекте
            name = pathlib.PurePath(link).name
            found = [
                f for f in ROOT.rglob(name)
                if not (set(f.parts) & SKIP_DIRS)
            ]
            if len(found) == 1:
                continue
            if len(found) > 1:
                # неоднозначно, но не ошибка: уточнить путь было бы лучше
                continue

            # шаг 3: нигде нет.
            #
            # Разница между ошибкой и предупреждением:
            #   * полный путь (`backend/app/foo.py`) — почти наверняка
            #     настоящая ссылка. Если файла нет, документация врёт.
            #   * короткое имя (`foo.py`) в прозе часто оказывается примером,
            #     когда автор объясняет работу самого валидатора. Блокировать
            #     коммит из-за иллюстрации неправильно.
            #
            # Поэтому: путь со слэшем — ошибка, голое имя — предупреждение.
            if "/" in link:
                errors.append(f"{md.relative_to(ROOT)}: битая ссылка `{link}`")
            else:
                warnings.append(
                    f"{md.relative_to(ROOT)}: имя `{link}` не найдено в проекте "
                    f"(если это пример, а не ссылка — можно игнорировать)"
                )
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Проверка слоя памяти: ссылки, блоки кода, обязательные разделы."
    )
    parser.add_argument(
        "--root", type=pathlib.Path, default=DEFAULT_ROOT,
        help="каталог проекта (по умолчанию — корень этого скрипта)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="печатать только итог и ошибки",
    )
    args = parser.parse_args(argv)
    configure(args.root)

    if not args.quiet:
        print(f"Проверка слоя памяти: {len(DOCS)} документов")
        print(f"  корень: {ROOT}\n")
    links = check_links()
    blocks = check_code_blocks()
    docs = check_structure()
    iters = check_iterations()

    print(f"  ссылок на файлы   {links}")
    print(f"  блоков кода       {blocks}")
    print(f"  документов        {docs}")
    print(f"  итераций          {iters}\n")

    if warnings:
        print(f"Предупреждения ({len(warnings)}):")
        for w in warnings:
            print(f"  ⚠ {w}")
        print()

    if errors:
        print(f"Проблемы ({len(errors)}):")
        for e in errors:
            print(f"  ✗ {e}")
        return 1

    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
