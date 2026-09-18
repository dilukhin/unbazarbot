# Unbazarbot — Work Plan

Дата актуализации: 2026-09-18.

Issues являются исполнимым backlog. Этот документ задаёт порядок, зависимости и критерии перехода между этапами.

## Этап 0 — привести границы репозитория в порядок

### 0.1. Issue #3 — отделить runtime config от публичного Git-конфига

https://github.com/dilukhin/unbazarbot/issues/3

Сделать первым, потому что публичный tracked `config.yaml` сейчас содержит локальную Telegram admin-привязку.

Результат:
- tracked `config.example.yaml` без реальных user/chat IDs;
- локальный `config.yaml` исключён из Git;
- существующий VPS не теряет рабочие настройки;
- README/security docs согласованы.

Gate: fresh clone можно настроить по example, а production config не находится под version control.

## Этап 1 — создать регрессионную опору

### 1.1. Начать issue #4 — tests/CI для текущего поведения

https://github.com/dilukhin/unbazarbot/issues/4

До изменения пользовательского поведения создать тестовый каркас и покрыть минимум:
- admin/private access;
- pending/approve/revoke;
- one-time consumption;
- `/tr` reply;
- auto gate;
- cache hit;
- RouterAI error path.

На этом шаге CI может быть минимальным; issue #4 окончательно закрывается после добавления тестов для #2 и #1.

Gate: основные access-control paths защищены тестами без реальных Telegram/RouterAI вызовов.

## Этап 2 — ближайшие пользовательские требования

### 2.1. Issue #2 — запрет MP3 / media format policy

https://github.com/dilukhin/unbazarbot/issues/2

Причина приоритета: это явное требование пользователя и одновременно ограничение расхода RouterAI.

Нужно:
- конфигурируемый deny list;
- MP3 не отправляется provider;
- explicit flow отвечает понятным отказом;
- auto flow имеет явно определённое поведение;
- OGG/Opus Telegram voice не ломается.

Gate: тестами доказано, что MP3 не создаёт STT job/provider cost.

### 2.2. Issue #1 — читаемое форматирование STT

https://github.com/dilukhin/unbazarbot/issues/1

Добавить отдельный optional post-processing component:
- пунктуация;
- предложения;
- абзацы;
- отсутствие смыслового пересказа;
- fallback на raw transcript;
- отдельная конфигурация и учёт дополнительного usage/cost.

Gate: formatter можно выключить, а его отказ не уничтожает успешный STT.

### 2.3. Завершить issue #4

Добавить regression cases для media policy и formatter, включить стабильный CI для push/PR.

Gate: #4 закрыт и `main` имеет воспроизводимый зелёный test gate.

## Этап 3 — эксплуатационная устойчивость

### 3.1. Issue #5 — production runtime hardening

https://github.com/dilukhin/unbazarbot/issues/5

После стабилизации P0:
- проверить фактический systemd deployment;
- исключить два параллельных long-polling процесса;
- health/status runbook;
- journald retention;
- SQLite backup/restore с WAL;
- dependency pinning;
- deploy/restart/rollback procedure.

Production VPS менять только по явному поручению пользователя.

Gate: бот переживает logout/reboot, диагностика и восстановление описаны и проверяемы.

## Этап 4 — защита бюджета

### 4.1. Issue #6 — cost/rate controls

https://github.com/dilukhin/unbazarbot/issues/6

Добавить:
- limits per chat/user;
- daily minutes/cost caps;
- usage statistics;
- deduplication concurrent jobs для одного `file_unique_id + model_alias`;
- понятный отказ при достижении лимита.

Gate: новый provider call не может обойти access/cost policy, а админ может увидеть агрегированное использование.

## Отложенные расширения

Не начинать без отдельной потребности:
- forum-topic scoped auto-mode;
- webhook;
- MTProto history importer;
- web admin UI;
- дополнительные STT providers.

Текущий scope `/auto_on` на весь `chat_id` принят пользователем и не считается блокирующим дефектом.
