# Unbazarbot — текущее состояние

Проверено: 2026-10-01  
Ветка: `main`  
Меню доступа и проверки включены в изменения запроса на слияние #10.

Этот файл — подробный mutable status. Краткий обзор находится в корневом `STATUS.md`.

## Реализовано в GitHub main

- Python/aiogram long polling entry point.
- RouterAI STT adapter через `/audio/transcriptions`.
- YAML config с несколькими STT model aliases.
- Telegram admin IDs из config.
- SQLite persistence.
- Заявка группы при добавлении бота.
- Admin approval: one-time / forever / reject.
- `/access`: заявки людей и групп, поиск, страницы и карточки.
- `/requests`, `/groups`, `/revoke` с подтверждением отзыва.
- Отдельные личные разрешения обычных пользователей, без административных прав.
- Ручная расшифровка старого доступного сообщения через `/tr` reply.
- `/auto_on` / `/auto_off` для всего Telegram `chat_id`.
- Private media path с access policy.
- Cache по `file_unique_id + model_alias`.
- Лимиты размера и длительности.
- systemd unit example.
- `.env`, runtime DB/logs и virtualenv исключены из Git.

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
- Конфигурация отделена от Git в PR #7: безопасный шаблон, локальный рабочий YAML и инструкция миграции. Миграция работающего экземпляра ещё не подтверждена.
- [#4](https://github.com/dilukhin/unbazarbot/issues/4) — добавлены автоматические проверки доступа и GitHub Actions; остаётся покрыть прочие сценарии, ограничения форматов и форматирование.
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


## Меню доступа Telegram (#10, 2026-10-01)

Реализованы заявки обычных пользователей, независимый личный доступ, меню `/access`,
поиск и страницы, подтверждение отзыва и защита старых кнопок. Проверки находятся
в `tests/test_access.py`; добавлен процесс GitHub Actions. Существующая база обновляется
с сохранением разрешений групп. Миграция новой схемы и закрытие прежних заявок
выполняются вместе в одной транзакции. Ошибка редактирования старого сообщения
не мешает уведомлению пользователя о сохранённом решении.

Обновление рабочего VPS ещё не выполнялось. Подробности:
[управление доступом](access_management_ru.md).
Работа Max в отдельном запросе на слияние #9 и перенос конфигурации в #7 не изменены.

## Голосовые сообщения (2026-10-06)

Распознавание в коде ограничено типом Telegram `voice`: `/tr` отвечает отказом на обычное аудио и документ, а автоматическая обработка и личка тихо пропускают их. Это правило основано на типе сообщения, а не на расширении файла. Рабочее развёртывание на vserv ещё предстоит.

