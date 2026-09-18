# Unbazarbot — Security & Privacy Model

Дата актуализации: 2026-09-18.

## 1. Активы

Защищаем:
- `TELEGRAM_BOT_TOKEN`;
- `ROUTERAI_API_KEY`;
- RouterAI balance/quota;
- Telegram user/chat identifiers;
- тексты расшифровок;
- runtime SQLite DB;
- исходные аудиофайлы во время обработки.

## 2. Trust boundaries

```text
Telegram users/groups
        |
        v
Telegram Bot API
        |
        v
Unbazarbot VPS ----> RouterAI
        |
        v
SQLite/runtime files
```

Telegram group membership сама по себе не означает разрешение тратить RouterAI quota.

## 3. Admin identity

В Git хранится только обезличенный `config.example.yaml` с пустым списком администраторов. Рабочий `config.yaml` исключён из Git; альтернативный путь задаётся через `CONFIG_PATH`. Credentials остаются в `.env`, а не в YAML.

Bot admins задаются Telegram `user_id`. Username не является надёжным идентификатором полномочий.

Админ должен инициировать личный чат с ботом, чтобы бот мог сохранить private chat target для уведомлений.

## 4. Group authorization

Default policy — deny.

Группа может иметь:
- pending;
- one-time approval с ограниченным количеством jobs;
- persistent approval;
- rejected/revoked state.

Auto transcription допустим только для persistent approved group и отдельного `auto_enabled`.

Отзыв доступа должен выключать возможность новых платных вызовов; auto mode при revoke не должен сохранять фактическое право вызова provider.

## 5. Private chat

По умолчанию приватная расшифровка должна быть разрешена только bot admins. Разрешение non-admin private users — отдельное cost/security решение и не должно включаться случайно.

## 6. Secrets

Secrets:
- только в `.env` или эквивалентном runtime secret store;
- не в `config.yaml`;
- не в GitHub issues/PR/comments;
- не в ChatGPT Project Sources;
- не в логах.

При подозрении на утечку: revoke/rotate credential, затем обновить runtime.

## 7. Runtime data

`data/`, SQLite WAL/SHM, `logs/` и временные media files не коммитятся.

Публичная документация не должна содержать расшифровки реальных пользователей или дампы БД. Для отладки использовать минимальные обезличенные фрагменты.

## 8. Cost controls

Любой новый путь, который может вызвать STT, обязан проходить access check и media limits.

Рекомендуемые дальнейшие защиты:
- configurable deny/allow list форматов;
- rate limits per group/user;
- cost/day or minutes/day caps;
- explicit metrics по provider/model/group;
- защита от повторной оплаты через cache.

## 9. Error handling

Не делать неограниченный retry внешнего STT. Ошибка Telegram/RouterAI должна завершать job контролируемо и быть видимой оператору без утечки токенов или полного request payload.

## 10. External processing

Аудио отправляется внешнему STT provider. Это должно учитываться при использовании бота в группах с чувствительными данными. До добавления других провайдеров требуется проверить их privacy/data-retention условия и зафиксировать решение.

## 11. Миграция конфигурации

Перед обновлением старого развёртывания сохраните рабочий YAML вне репозитория по [инструкции](config_migration_ru.md). Добавление файла в `.gitignore` само по себе не защищает отслеживаемый файл от удаления при обновлении.

Удаление персональной привязки из текущего дерева не удаляет её из прежних коммитов. История Git не переписывается в рамках #3. Реальные IDs нельзя возвращать в примеры, отчёты и тестовые фикстуры.
