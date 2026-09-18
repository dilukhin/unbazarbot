# Unbazarbot — Work Plan

Дата актуализации: 2026-09-18.

Порядок ориентирован на ближайшие фактические потребности, а не на абстрактное расширение продукта.

## P0 — привести текущий MVP к устойчивой эксплуатации

### 1. Форматирование расшифровки

Добавить отдельный optional post-processing step после STT:
- пунктуация;
- предложения;
- абзацы;
- запрет смыслового переписывания;
- отдельная модель/alias/config;
- fallback на raw transcript при ошибке formatter;
- учёт дополнительной стоимости.

Acceptance:
- raw STT не теряется;
- formatter можно выключить config;
- Telegram получает читаемый текст;
- ошибка formatter не ломает саму расшифровку.

### 2. Запрет MP3 через config

Добавить media policy, например:
```yaml
limits:
  disabled_audio_formats:
    - mp3
```

Поведение:
- explicit `/tr` на MP3 — понятный отказ;
- private MP3 — понятный отказ;
- auto-mode MP3 — тихо игнорировать или применять единообразную явно принятую policy;
- MP3 не отправляется RouterAI и не создаёт расход.

Проверять extension/MIME/Telegram metadata без доверия только к одному признаку.

### 3. Tests

Минимальный набор:
- admin/private access;
- pending/approve-once/approve-forever/revoke;
- `/tr` reply;
- one-time job consumption;
- auto gate;
- cache hit;
- media limits / disabled format;
- RouterAI error path;
- formatter fallback.

## P1 — эксплуатация

- проверить и зафиксировать реальный systemd deployment;
- health/status procedure;
- log rotation или journald policy;
- backup/restore policy для SQLite;
- dependency pinning/lock strategy;
- простой CI: syntax/import/tests.

## P2 — контроль стоимости

- rate limit per chat/user;
- daily minutes/cost caps;
- агрегированная статистика по model/group;
- команды admin statistics;
- guard от параллельной повторной обработки одного file/model.

## P3 — расширения только по потребности

- отдельные providers;
- forum-topic scoped auto mode;
- webhook вместо polling;
- MTProto history importer;
- web admin UI.

Эти пункты не начинать только ради полноты продукта: сначала закрывать P0/P1 и реальные проблемы эксплуатации.
