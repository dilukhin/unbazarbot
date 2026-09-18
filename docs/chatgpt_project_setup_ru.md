# Настройка ChatGPT Project для Unbazarbot

## Цель

Не дублировать изменяемую документацию между GitHub и Project Sources.

## Project Sources

Добавить в Project Sources содержимое:
- `github_project_bootstrap.md`.

Этого достаточно для навигации к актуальному GitHub. Mutable файлы `README.md`, `docs/current_status_ru.md`, `docs/work_plan_ru.md` и другие документы не копировать в Project Sources: копия быстро устаревает.

Если в будущем появятся действительно неизменные внешние спецификации, их можно добавить отдельно.

## Project Instructions

Скопировать содержимое `chatgpt_project_instructions_seed_ru.md` в поле Project Instructions.

Seed хранится в GitHub, чтобы изменения инструкций проходили через историю репозитория. После изменения seed в GitHub нужно вручную синхронизировать поле Project Instructions в интерфейсе ChatGPT Project.

## Новый диалог

Стартовый запрос может быть коротким:

```text
Продолжаем Unbazarbot. Прочитай Project Source github_project_bootstrap.md,
через GitHub Connector перечитай актуальный main dilukhin/unbazarbot и обязательные
документы из bootstrap. Затем выполни задачу: ...
```

## Почему так

- GitHub хранит актуальную изменяемую документацию и историю.
- Project Sources содержит только стабильную точку входа.
- Project Instructions задают постоянные правила работы.
- Новый диалог не зависит от старого handoff для установления фактического состояния.
