# Unbazarbot — STATUS

Дата снимка: 2026-10-01  
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
- Личное использование обычными пользователями требует отдельного разрешения через `/access`.
- Кеширование привязано как минимум к `file_unique_id + model_alias`.

## Открытые задачи

1. [#3 — отделить runtime config от публичного Git-конфига](https://github.com/dilukhin/unbazarbot/issues/3)
2. [#4 — автоматические тесты и базовый CI](https://github.com/dilukhin/unbazarbot/issues/4)
3. [#1 — постобработка STT: пунктуация, предложения и абзацы](https://github.com/dilukhin/unbazarbot/issues/1)
4. [#5 — production runtime: systemd, health, логи, SQLite backup и зависимости](https://github.com/dilukhin/unbazarbot/issues/5)
5. [#6 — контроль расхода RouterAI и защита от повторной/параллельной оплаты](https://github.com/dilukhin/unbazarbot/issues/6)

## Ближайший приоритет

Сначала #3, затем расширить проверки #4 для остальных сценариев и выполнить #1. После этих изменений завершить #4 и перейти к #5 и #6.

## Документационный контур

ChatGPT Project настроен по GitHub-first схеме:

- Project Sources содержит `github_project_bootstrap.md`;
- Project Instructions дополнены пользователем и действуют в текущем проектном контексте;
- изменяемые baseline/status/plan документы читаются из актуального GitHub `main`.

Секреты, runtime DB, реальные расшифровки и аудиофайлы не должны попадать в Git.


## Меню доступа Telegram (#10)

Реализовано управление доступом людей и групп через `/access`: заявки пользователей,
поиск, страницы, карточки и подтверждение отзыва. Разрешения пользователей отделены
от административных полномочий. Добавлены автоматические проверки в GitHub Actions.
При ревью исправлены прерывание миграции базы и уведомления при ошибке редактирования
старого сообщения. Обновление рабочего VPS ещё не выполнялось.
[Инструкция](docs/access_management_ru.md).

## Голосовые сообщения

Код принимает для распознавания только Telegram `voice` в личке, в групповой автоматической обработке и по `/tr` в ответ. Обычные `audio` и аудиофайлы как `document` не создают задания и не вызывают RouterAI. Обновление рабочего бота ещё не выполнено.
