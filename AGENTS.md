# Unbazarbot — Agent Seed

Project: `dilukhin/unbazarbot`.

Перед существенной локальной работой прочитай:
1. `docs/project_baseline_ru.md`;
2. `docs/current_status_ru.md`;
3. `docs/architecture_ru.md`;
4. `docs/security_model_ru.md`;
5. `docs/work_plan_ru.md`;
6. точное задание пользователя.

## Роль агента

Локальный агент — исполнитель ограниченной задачи, а не источник продуктовых или архитектурных решений.

- Не расширяй scope без явного указания.
- Не считай README, roadmap или старый отчёт доказательством фактического состояния кода: проверяй текущий checkout.
- Перед правками проверь repository, branch, HEAD и dirty state.
- Не изменяй production VPS, Telegram BotFather, RouterAI account, systemd service, SQLite runtime DB или реальные группы без явного разрешения.
- Секреты из `.env`, токены Telegram и RouterAI API key никогда не печатай, не коммить и не копируй в отчёты.
- Runtime-данные `data/`, логи и временные аудиофайлы не являются repository artifacts.
- При неожиданном состоянии останови затронутый mutation path и верни evidence; не применяй reset/clean/force/delete как обход.
- После изменения запускай самые узкие относящиеся проверки.
- Архитектурные, security и access-control решения эскалируй в ChatGPT Web, если они не определены нормативными документами.

## Инварианты реализации

- Telegram long polling и RouterAI STT — текущая базовая архитектура.
- Группа по умолчанию не имеет права тратить RouterAI-баланс.
- Доступ группы появляется только через approval администратора бота.
- Администраторы идентифицируются устойчивым Telegram `user_id`, а не username.
- Секреты находятся вне Git.
- Кеширование расшифровок должно предотвращать повторную оплату одного и того же файла для той же модели.
- Поведение, которого нет в текущем `main`, не описывай как реализованное.

Рабочие документы проекта — преимущественно по-русски; код, identifiers и machine-readable fields — по-английски.
