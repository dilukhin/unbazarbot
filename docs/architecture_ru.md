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


## Общий опубликованный каталог (#18)

`voicebot/tariffs.py` читает фиксированный HTTPS-документ agent-toolchain,
проверяет идентичность, hash, единицы, маршруты и свежесть, хранит последнюю
проверенную версию вне выпусков. Фоновая задача обновляет данные без нового
poller и перезапуска; обработчики получают типизированный Decimal Estimate.
Предварительная стоимость отделена от прежнего `usage.cost`. До отдельного
резерва #20 платное оформление не выполняется. [Контракт и границы](tariffs_ru.md).


## Точная бухгалтерия (#19)

`money.py` задаёт Decimal, атомарные доли 10^-9 Б и московские месячные границы.
`ledger_store.py` выполняет начисление, резерв, подтверждение и освобождение
в отдельных транзакциях либо в переданном `conn`. Таблицы money_* отделены
от прежнего REAL-учёта; старые расходы не списываются повторно. Middleware
фиксирует явное личное обращение, постоянное групповое разрешение создаёт
групповой якорь внутри транзакции решения. [Схема](money_ledger_ru.md).
Объединение с платным транспортом — #20, выбор бюджета — #21.


## Точный резерв и маршрутизация (#20–21)

Общая транзакция FinancialStore связывает доступ, одноразовую квоту, минутные
пределы, дубль фактической модели и файл, личный/групповой резерв и общий
денежный лимит. Точный расход RouterAI и Generation ID сохраняются отдельно
от каталожной оценки и прежнего REAL-учёта. Неизвестный исход держит резерв,
перерасход замораживает модель. Авто тратит групповые средства и молчит при
нуле; ручное /tr тратит личные. [Полный договор](financial_costs_ru.md).
По умолчанию money.global_daily_rub равен строке "0" и запрещает новую оплату;
производственное значение и выпуск требуют отдельного операторского поручения.


## Расширенное ручное аудио (#22)

/trf имеет отдельное личное разрешение, одноразовые кнопки с владельцем/сроком
и повторной проверкой поколения доступа. Автоматические audio/document не
обрабатываются. Денежный резерв выполняется до скачивания; фактический размер,
формат и декодированная длительность каждого нового платного аудио проверяются
ограниченными ffprobe/FFmpeg. Ошибка до отправки возвращает резерв, неизвестный
исход после отправки его сохраняет. [Подробности](extended_audio_ru.md).
