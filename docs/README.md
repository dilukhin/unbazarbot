# Документация Unbazarbot

GitHub — основной изменяемый источник истины проекта.

## Нормативные документы

- [project_baseline_ru.md](project_baseline_ru.md) — устойчивые цели, scope, инварианты и source-of-truth.
- [architecture_ru.md](architecture_ru.md) — компоненты, потоки данных и границы.
- [security_model_ru.md](security_model_ru.md) — доступ, секреты, приватность и расход внешнего API.
- [current_status_ru.md](current_status_ru.md) — текущее фактическое состояние и известные расхождения.
- [work_plan_ru.md](work_plan_ru.md) — приоритетный план следующих изменений.
- [chatgpt_project_setup_ru.md](chatgpt_project_setup_ru.md) — настройка ChatGPT Project.

В корне:
- `github_project_bootstrap.md` — единственный рекомендуемый mutable-aware bootstrap для Project Sources;
- `chatgpt_project_instructions_seed_ru.md` — seed поля Project Instructions;
- `AGENTS.md` — правила локального агента;
- `README.md` — пользовательская эксплуатационная документация.

Если документы расходятся с текущим кодом `main`, для факта реализации приоритет имеет код; расхождение должно быть зафиксировано в `current_status_ru.md`.
