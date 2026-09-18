# Unbazarbot — STATUS

Дата снимка: 2026-09-18  
Канонический репозиторий: `dilukhin/unbazarbot`  
Ветка: `main`

Это краткий оперативный статус проекта. Подробные факты и расхождения ведутся в `docs/current_status_ru.md`, порядок работ — в `docs/work_plan_ru.md`.

## Состояние

MVP работоспособен по подтверждённому пользовательскому smoke-test:

- `@unbazarbot` запускается на VPS и отвечает в Telegram;
- группа проходит admin approval;
- после approval ручной `/tr` reply на старое голосовое успешно вызывает RouterAI и публикует расшифровку;
- конфигурация поддерживает несколько STT model aliases;
- в коде реализованы persistent/one-time approvals, revoke, SQLite cache и group auto-mode.

Не подтверждено отдельным production smoke-test:

- постоянный запуск через systemd после reboot/logout;
- автоматическое распознавание в production после `/auto_on`;
- backup/restore runtime SQLite.

## Принятые продуктовые решения

- Telegram transport — long polling.
- STT выполняется RouterAI, тяжёлый локальный ASR на VPS не используется.
- Группа по умолчанию запрещена и требует approval администратора бота.
- Старые сообщения распознаются через `/tr` в reply.
- `/auto_on` действует на весь Telegram `chat_id`; forum topics пока не являются отдельными scopes.
- Приватное использование non-admin пользователями по умолчанию запрещено.
- Кеширование привязано как минимум к `file_unique_id + model_alias`.

## Открытые задачи

1. [#3 — отделить runtime config от публичного Git-конфига](https://github.com/dilukhin/unbazarbot/issues/3)
2. [#4 — автоматические тесты и базовый CI](https://github.com/dilukhin/unbazarbot/issues/4)
3. [#2 — конфигурируемый запрет MP3 и media format policy](https://github.com/dilukhin/unbazarbot/issues/2)
4. [#1 — постобработка STT: пунктуация, предложения и абзацы](https://github.com/dilukhin/unbazarbot/issues/1)
5. [#5 — production runtime: systemd, health, логи, SQLite backup и зависимости](https://github.com/dilukhin/unbazarbot/issues/5)
6. [#6 — контроль расхода RouterAI и защита от повторной/параллельной оплаты](https://github.com/dilukhin/unbazarbot/issues/6)

## Подготовленные изменения

Для #3 подготовлена ветка `fix/3-local-runtime-config`: обезличенный шаблон конфигурации, исключение локального YAML из Git и инструкция сохранения настроек при обновлении. Изменения ещё не означают слияние в `main` или миграцию VPS.

## Ближайший приоритет

Сначала #3, затем базовый каркас #4, после чего #2 и #1. После этих изменений завершить #4 и перейти к #5 и #6.

## Документационный контур

ChatGPT Project настроен по GitHub-first схеме:

- Project Sources содержит `github_project_bootstrap.md`;
- Project Instructions дополнены пользователем и действуют в текущем проектном контексте;
- изменяемые baseline/status/plan документы читаются из актуального GitHub `main`.

Секреты, runtime DB, реальные расшифровки и аудиофайлы не должны попадать в Git.
