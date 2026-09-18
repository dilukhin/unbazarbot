# Unbazarbot — текущее состояние

Проверено: 2026-09-18  
Ветка: `main`  
Проверенный baseline перед текущим status/plan update: `6f2964200e2ba26689ec96975fcb24cc4524bc11`.

Этот файл — подробный mutable status. Краткий обзор находится в корневом `STATUS.md`.

## Реализовано в GitHub main

- Python/aiogram long polling entry point.
- RouterAI STT adapter через `/audio/transcriptions`.
- YAML config с несколькими STT model aliases.
- Telegram admin IDs из config.
- SQLite persistence.
- Заявка группы при добавлении бота.
- Admin approval: one-time / forever / reject.
- `/requests`, `/groups`, `/revoke`.
- Ручная расшифровка старого доступного сообщения через `/tr` reply.
- `/auto_on` / `/auto_off` для всего Telegram `chat_id`.
- Private media path с access policy.
- Cache по `file_unique_id + model_alias`.
- Лимиты размера и длительности.
- systemd unit example.
- `.env`, runtime DB/logs и virtualenv исключены из Git.

## Подготовлено по issue #3

В ветке `fix/3-local-runtime-config` подготовлены безопасный `config.example.yaml`, исключение рабочего `config.yaml` из Git и инструкция `docs/config_migration_ru.md`. До слияния PR раздел выше описывает прежний `main`. На VPS миграция не выполнялась; сохранность его активного конфига должна быть подтверждена оператором по инструкции.

## Подтверждено текущей эксплуатацией пользователем

В ходе первоначального развёртывания:
- бот запускался на VPS и отвечал в Telegram;
- group approval был получен администратором;
- после approval `/tr` reply успешно распознавал аудио.

Не считать подтверждённым без отдельного runtime smoke:
- systemd lifecycle/reboot;
- production auto-mode после `/auto_on`;
- SQLite backup/restore.

## ChatGPT Project

Пользователь выполнил настройку Project Sources и Project Instructions.

В текущем проектном контексте:
- доступен `github_project_bootstrap.md` как Project Source;
- активны дополненные Project Instructions, включая правила перехода в новый диалог и execution continuity;
- GitHub-файл `chatgpt_project_instructions_seed_ru.md` также содержит эти дополнения.

Mutable документация остаётся в GitHub и не дублируется в Project Sources.

## Известные ограничения / открытые issues

- [#1](https://github.com/dilukhin/unbazarbot/issues/1) — нет отдельного LLM post-processing для пунктуации и абзацев.
- [#2](https://github.com/dilukhin/unbazarbot/issues/2) — нет конфигурируемого запрета MP3/media format policy.
- [#3](https://github.com/dilukhin/unbazarbot/issues/3) — публичный tracked `config.yaml` содержит runtime/персональную admin-привязку; требуется example/local split.
- [#4](https://github.com/dilukhin/unbazarbot/issues/4) — нет automated test suite и CI.
- [#5](https://github.com/dilukhin/unbazarbot/issues/5) — production systemd/health/log/backup/dependency lifecycle не верифицирован как устойчивый.
- [#6](https://github.com/dilukhin/unbazarbot/issues/6) — нет rate/cost caps и защиты от параллельной повторной оплаты.

## Принятые ограничения, не являющиеся текущим дефектом

- Auto mode действует на весь `chat_id`, не на отдельный forum topic; это поведение пользователь пока принял.
- Старые сообщения не сканируются по истории: поддерживается `/tr` reply.
- Тяжёлый локальный ASR не планируется на текущей малой VPS без отдельного решения.
- Runtime bundle `dist/projects/dilukhin__unbazarbot.md` в `github-connector-knowledge` может отсутствовать; bootstrap обязан работать без него.

## Документационный контур

- корневой `STATUS.md` — краткий текущий снимок;
- этот файл — подробный current status;
- `docs/work_plan_ru.md` — порядок выполнения issues;
- baseline/architecture/security — устойчивые решения.
