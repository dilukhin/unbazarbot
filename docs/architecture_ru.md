# Unbazarbot — архитектура

Дата актуализации: 2026-10-08.

## Компоненты

```text
Telegram
   |
   v
aiogram handlers
   |
   +--> access checks / approvals ----> SQLite
   |
   +--> media extraction / limits
   |
   +--> Telegram getFile/download
   |
   v
RouterAITranscriber
   |
   v
RouterAI /audio/transcriptions
   |
   v
result + usage
   |
   +--> SQLite job/cache
   |
   v
Telegram reply
```

### Entry point

`bot.py`:
- загружает `.env`;
- читает YAML config;
- открывает SQLite;
- синхронизирует config-admins;
- создаёт RouterAI transcriber;
- запускает aiogram long polling;
- перед стартом удаляет webhook с `drop_pending_updates=False`, сохраняя накопленные обновления Telegram.

### Telegram layer

`voicebot/handlers.py` владеет:
- командами `/start`, `/help`, `/status`, `/model`, `/requests`, `/groups`, `/revoke`, `/auto_on`, `/auto_off`, `/tr`, `/tr_raw`, `/about`, `/system`, `/stats`;
- group approval callbacks;
- auto/private media handler;
- событием добавления бота в группу.

`voicebot/access_ui.py` содержит меню `/access`, списки, карточки, поиск и ввод
пояснения к личной заявке. Этот обработчик подключается перед общими обработчиками.
`voicebot/access_store.py` расширяет хранилище отдельными разрешениями пользователей,
миграцией схемы и транзакциями решений. Состояние ввода хранится в памяти aiogram,
а заявки и разрешения — в SQLite.

### Config

`voicebot/config.py` преобразует YAML в typed application config. Модели задаются alias -> provider_model. Новая STT-модель не должна требовать изменения Telegram handlers, если её protocol совместим с текущим transcriber.

Публичный шаблон — `config.example.yaml`; рабочий YAML исключён из Git.
`CONFIG_PATH` может указывать на внешний файл. Шаблон не загружается автоматически.

### Persistence

`voicebot/db.py` содержит runtime schema и операции над:
- admins;
- groups;
- private_users;
- access requests;
- transcription jobs;
- cache/audit data.

SQLite хранится вне Git.

### Media

`voicebot/media.py` извлекает метаданные только сообщений Telegram типа `voice`, скачивает файл во временное хранилище и определяет формат для STT. Сообщения `audio` и аудиофайлы типа `document` не попадают в этот путь.

### STT

`voicebot/stt_routerai.py`:
- base64-кодирует временный файл;
- вызывает RouterAI `/audio/transcriptions`;
- извлекает текст;
- по возможности извлекает cost/duration из usage.

## Основные потоки

### Ручной /tr

```text
/tr reply -> voice check -> access check -> extract media -> limits -> cache lookup
          -> download -> RouterAI -> store -> reply
```

### Group approval

```text
bot added or denied /tr
 -> access_request(pending)
 -> admin private chat
 -> approve once / approve forever / reject
 -> group state
```

### Auto mode

```text
new media in group
 -> group.status == approved
 -> group.auto_enabled == true
 -> transcribe
```

Текущий auto scope — весь `chat_id`; Telegram forum `message_thread_id` отдельно не хранится.

## Расширения

Предпочтительные extension points:
- text post-processing — отдельный provider/component после STT, а не внутри Telegram handler;
- media deny/allow rules — config + media validation;
- новые STT providers — provider interface/adapter;
- rate/cost policy — отдельный policy слой перед provider call.

Такие расширения не должны смешивать access decision, Telegram transport и внешнее распознавание в одну функцию.



## Резервирование платного вызова

`voicebot/paid_store.py` отделяет проверку бюджета от транспорта. После скачивания
и перед RouterAI одна транзакция SQLite повторно проверяет доступ, резервирует
файл и модель, лимиты и одноразовое право. Успешный текст и сведения об оплате
подтверждаются вместе. Кеш не создаёт нового вызова. При неизвестном исходе
повтор блокируется. Вход `bot.py` получает блокировку процесса до открытия базы;
только её владелец переводит старые активные резервы в прерванное состояние.

## Оформление и диагностика

`voicebot/formatter.py` — отдельный обработчик после распознавания. Исходный
текст сохраняется до него; оформленный текст, подпись настроек, ошибка и стоимость
хранятся отдельно. Дополнительный вызов повторно проверяет доступ и не повторяется
при ошибке. `/tr_raw` использует исходный кеш. Описание: [оформление](formatter_ru.md).

`voicebot/diagnostics.py` показывает публичную версию по `/about`; `/system`
проходит проверку администратора и личного чата. Выпуск без Git получает SHA
из файла `REVISION`, созданного серверным скриптом. Отсутствующая ревизия явно
обозначается; сетевой запрос для её определения не требуется.

`scripts/update_bot.py` подготовляет из точного `main` отдельный выпуск и
окружение, проверяет их до остановки службы, сохраняет SQLite и настройки,
переключает ссылку `current`. Проверки базы выполняет `scripts/maintenance.py`.
После запуска нового процесса база не подменяется старой автоматически.
