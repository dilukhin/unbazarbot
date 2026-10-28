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
- `STATUS.md` — краткий оперативный снимок состояния и links на активные issues;
- `github_project_bootstrap.md` — единственный рекомендуемый mutable-aware bootstrap для Project Sources;
- `chatgpt_project_instructions_seed_ru.md` — seed поля Project Instructions;
- `AGENTS.md` — правила локального агента;
- `README.md` — пользовательская эксплуатационная документация.

Если документы расходятся с текущим кодом `main`, для факта реализации приоритет имеет код; расхождение должно быть зафиксировано в `current_status_ru.md`.


## Эксплуатация и текущая очередь

- [access_management_ru.md](access_management_ru.md) — выдача и отзыв доступа.
- [budget_ru.md](budget_ru.md) — ограничения платных запросов и учёт расходов.
- [formatter_ru.md](formatter_ru.md) — необязательное оформление текста.
- [update_procedure_ru.md](update_procedure_ru.md) — проверка, подготовка, обновление и восстановление.
- [vserv_update_task_ru.md](vserv_update_task_ru.md) — историческое задание 0.2.0 и границы следующего отдельного выпуска.


## Базарики и расширенное аудио

- [bazariki_design_ru.md](bazariki_design_ru.md) — принятые требования #17–24.
- [tariffs_ru.md](tariffs_ru.md) — опубликованный каталог agent-toolchain и клиент.
- [money_ledger_ru.md](money_ledger_ru.md) — точный журнал, месяцы и миграция.
- [financial_costs_ru.md](financial_costs_ru.md) — резерв и фактические расходы RouterAI.
- [extended_audio_ru.md](extended_audio_ru.md) — отдельное право /trf и медиапроверка.
- [money_ui_ru.md](money_ui_ru.md) — баланс, справки и подтверждения администратора.
- [bzr_release_acceptance_ru.md](bzr_release_acceptance_ru.md) — PR/коммиты/файлы,
  репозиторные испытания и оставшиеся условия эксплуатации #24.
