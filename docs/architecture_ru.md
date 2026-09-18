# Unbazarbot — архитектура

Дата актуализации: 2026-09-18.

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
- перед стартом удаляет webhook с `drop_pending_updates=True`.

### Telegram layer

`voicebot/handlers.py` владеет:
- командами `/start`, `/help`, `/status`, `/model`, `/requests`, `/groups`, `/revoke`, `/auto_on`, `/auto_off`, `/tr`;
- group approval callbacks;
- auto/private media handler;
- событием добавления бота в группу.

### Config

`voicebot/config.py` преобразует YAML в typed application config. Модели задаются alias -> provider_model. Новая STT-модель не должна требовать изменения Telegram handlers, если её protocol совместим с текущим transcriber.

### Persistence

`voicebot/db.py` содержит runtime schema и операции над:
- admins;
- groups;
- access requests;
- transcription jobs;
- cache/audit data.

SQLite хранится вне Git.

### Media

`voicebot/media.py` извлекает Telegram media metadata, скачивает файл во временное хранилище и определяет формат для STT.

### STT

`voicebot/stt_routerai.py`:
- base64-кодирует временный файл;
- вызывает RouterAI `/audio/transcriptions`;
- извлекает текст;
- по возможности извлекает cost/duration из usage.

## Основные потоки

### Ручной /tr

```text
/tr reply -> access check -> extract media -> limits -> cache lookup
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
