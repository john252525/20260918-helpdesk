Задача построить **memory layer** для stateless-агента. Ключевая идея: разделить память по **времени жизни** и **частоте чтения** — не всё мешать в один файл и не плодить сотни `.md` по итерациям.

## Базовая структура

```
/docs
  /memory
    PROJECT.md          ← что за проект, зачем, архитектура (медленно меняется)
    STATE.md            ← где мы сейчас, что в работе (быстро меняется)
    DECISIONS.md        ← ADR-стиль: почему сделали так, а не иначе
    CONVENTIONS.md      ← кодстайл, соглашения, паттерны
    GLOSSARY.md         ← доменные термины, чтобы агент не путался
    BACKLOG.md          ← что не сделано / отложено / known issues
    /iterations
      2026-09-17-001-auth.md
      2026-09-17-002-db-schema.md
```

Плюс `AGENTS.md` в корне — это **точка входа**, которую агент читает первым делом при старте сессии.

---

## Что реально нужно (по важности)

### 1. `AGENTS.md` в корне — манифест для агента
Короткий (1 экран). Что читать, в каком порядке, как обновлять память после итерации. Это "bootloader".

```markdown
# Agent instructions
При старте читай в порядке:
1. docs/memory/PROJECT.md — контекст
2. docs/memory/STATE.md — где мы
3. docs/memory/CONVENTIONS.md — как писать код
4. последние 2-3 файла из docs/memory/iterations/

После завершения итерации ОБЯЗАТЕЛЬНО:
- обнови STATE.md
- создай docs/memory/iterations/YYYY-MM-DD-NNN-<slug>.md
- если было архитектурное решение — допиши в DECISIONS.md
```

### 2. `PROJECT.md` — медленная память
Что за продукт, для кого, стек, верхнеуровневая архитектура, схема БД, внешние интеграции. Меняется редко, читается всегда. Если разрастается — режь на `ARCHITECTURE.md`, `DATA_MODEL.md`.

### 3. `STATE.md` — быстрая память (самое важное!)
Один файл, который всегда актуален. Это то, что ты читаешь после обнуления контекста, чтобы понять "где мы".

```markdown
# Current state
Last updated: 2026-09-17

## Done
- Auth (JWT, refresh tokens)
- Postgres schema v1

## In progress
- Payments integration (Stripe) — started, webhook не тестирован

## Next
- Email verification
- Rate limiting

## Blockers
- Нет тестового Stripe аккаунта
```

**Правило:** если файл не обновляется автоматически агентом каждый раз — он гниёт. Поэтому это явный шаг в чеклисте агента.

### 4. `DECISIONS.md` — ADR (Architecture Decision Records)
Формат: `## ADR-007: JWT вместо сессий в Redis` → Контекст / Решение / Последствия. Спасает от того, что агент через 5 обнулений начнёт предлагать то, что вы уже отвергли.

### 5. `CONVENTIONS.md`
Стиль именования, структура папок, как писать тесты, какие либы можно/нельзя, обработка ошибок. Чтобы агент не переизобретал.

### 6. `BACKLOG.md` / `KNOWN_ISSUES.md`
TODO, техдолг, отложенные фичи, известные баги. Чтобы он не "чинил" то, что вы сознательно отложили.

### 7. `iterations/` — журнал

**Да, отдельный файл на итерацию — правильно.** Но:

- **Именование:** `YYYY-MM-DD-NNN-slug.md` (дата + порядковый номер + краткий слаг). Сортируется само.
- **Шаблон короткий**, не эссе:
  ```markdown
  # Iteration 042 — Payments webhook
  Date: 2026-09-17
  Commit: a1b2c3d

  ## Goal
  ...
  ## Changes
  - file X: added ...
  - file Y: refactored ...
  ## Tests
  - ...
  ## Notes / next
  - ...
  ```
- **Не превращай в дневник.** Итерации — это append-only лог для аудита и восстановления контекста. Реальную "текущую картину" держи в `STATE.md`.

---

## Ключевые принципы

1. **Три горизонта памяти:**
   - **Cold** (PROJECT, DECISIONS, CONVENTIONS) — редко меняется, всегда читается
   - **Warm** (STATE, BACKLOG) — меняется каждую итерацию
   - **Hot/append-only** (iterations/) — только дописывается

2. **Один источник правды.** STATE.md — единственное место, где написано "где мы сейчас". Не дублируй это в PROJECT.md.

3. **Ссылки на код.** В iteration-файлах указывай конкретные файлы/функции — это ускоряет навигацию агенту после обнуления.

4. **MEMORY vs SPEC.** Не путай "как есть сейчас" (STATE) и "как должно быть" (спека/тикеты). Если есть отдельная спека — держи её отдельно в `docs/spec/`.

---

## Бонус: микро-шаблон конца итерации

Вставь это в `AGENTS.md`, чтобы агент не забывал:

```
## End of iteration checklist
1. All tests pass
2. Update docs/memory/STATE.md (Done / In progress / Next / Blockers)
3. Create docs/memory/iterations/YYYY-MM-DD-NNN-<slug>.md from template
4. If architectural choice made — append to DECISIONS.md
5. If new TODO/debt — append to BACKLOG.md
6. git add -A && git commit -m "iter NNN: <summary>" && git push
```
