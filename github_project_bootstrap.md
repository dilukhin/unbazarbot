---
document_type: github_project_bootstrap
version: 1.0
status: active
language: ru
updated_at: 2026-09-18
---

# Unbazarbot — GitHub bootstrap для ChatGPT Project

## Назначение

Этот файл предназначен для хранения в **Project Sources** ChatGPT Project. Он специально мал и стабилен: изменяемая документация проекта живёт в GitHub.

```yaml
target_repository:
  full_name: dilukhin/unbazarbot
  default_branch: main

knowledge_repository:
  full_name: dilukhin/github-connector-knowledge
  default_branch: main
  runtime_bundle_path: dist/projects/dilukhin__unbazarbot.md
```

Отсутствие runtime bundle для этого проекта не является ошибкой и не мешает работе.

## Источник истины

Перед существенной задачей заново читай актуальный `main` через GitHub Connector.

Минимальный набор:
1. `README.md`
2. `docs/project_baseline_ru.md`
3. `docs/current_status_ru.md`
4. `docs/architecture_ru.md`
5. `docs/security_model_ru.md`
6. `docs/work_plan_ru.md`
7. `AGENTS.md` — если задача включает локального агента или изменение кода.

`chatgpt_project_instructions_seed_ru.md` — канонический seed для поля Project Instructions, а не замена чтению актуального GitHub.

## Правила GitHub

1. GitHub Connector — первичный remote-транспорт.
2. Не использовать `git`/`gh` как пробу удалённого доступа.
3. Локальный Git допустим только для подтверждённого checkout: diff/history/tests или при доказанном gap Connector.
4. Для многофайловой публикации через Connector использовать Git Data flow `blob -> tree -> commit -> ref`, когда он доступен.
5. Перед изменением ref перечитать HEAD; не force-update `main`.
6. После значимой write-операции выполнить targeted read-back через Connector.
7. Roadmap, диалог, память или старый handoff не доказывают наличие реализации.
8. Не публиковать secrets, `.env`, SQLite runtime DB, расшифровки пользователей или чувствительные логи.

## Начало задачи

- подтвердить repository/default branch;
- прочитать нормативные документы;
- проверить текущий код/конфиг, относящийся к задаче;
- отделить факт `main` от пользовательского runtime-состояния и планов;
- только после этого изменять код или документацию.

## Завершение задачи

- проверить GitHub-side состояние;
- обновить `docs/current_status_ru.md` и/или `docs/work_plan_ru.md`, если фактическое состояние изменилось существенно;
- устойчивые архитектурные решения обновлять в baseline/architecture/security docs;
- не превращать baseline в журнал текущих SHA, процессов и временных экспериментов.

## Project Sources

Рекомендуемый состав Project Sources: **только этот bootstrap** и, при необходимости, действительно неизменяемые внешние материалы. Не копировать туда mutable README/status/roadmap: они должны перечитываться из GitHub.
