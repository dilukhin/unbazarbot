# Unbazarbot

Telegram-бот для расшифровки голосовых сообщений и аудиофайлов через RouterAI. Бот работает в личных чатах и группах, поддерживает ручное распознавание по команде `/tr`, автоматическое распознавание новых voice/audio в разрешенных группах, кеширование результатов и админское подтверждение доступа для групп.

## Возможности

- Расшифровка `voice`, `audio` и аудиофайлов, отправленных как `document`.
- Команда `/tr [model]` для распознавания аудио из reply.
- Автоматическое распознавание новых voice/audio в группе после включения `/auto_on`.
- Админское подтверждение доступа группы: на один раз или навсегда.
- Настраиваемые модели RouterAI через `config.yaml`.
- Лимиты на длительность аудио и размер файла.
- SQLite-хранилище для групп, заявок, админов, аудита, задач распознавания и кеша расшифровок.
- Пример systemd unit для запуска на Linux-сервере.

## Как это работает

1. Бот получает voice/audio из Telegram.
2. Скачивает файл во временный файл ОС.
3. Отправляет аудио в RouterAI endpoint `/audio/transcriptions`.
4. Сохраняет результат в SQLite и отвечает в чат текстом расшифровки.
5. При повторной обработке того же Telegram-файла может вернуть результат из кеша.

## Требования

- Python 3.8 или новее.
- Telegram bot token от BotFather.
- RouterAI API key.
- Доступ в интернет с сервера, где запущен бот.

Зависимости указаны в `requirements.txt`:

```text
aiogram
httpx
python-dotenv
PyYAML
aiosqlite
```

## Где взять Telegram bot token

1. Откройте Telegram и найдите `@BotFather`.
2. Выполните команду `/newbot`.
3. Задайте отображаемое имя бота.
4. Задайте username бота. Он должен заканчиваться на `bot`, например `my_voice_helper_bot`.
5. BotFather выдаст token вида `1234567890:AA...`.
6. Сохраните token в `.env` как `TELEGRAM_BOT_TOKEN`.

Не публикуйте Telegram bot token в GitHub, логах, скриншотах и чатах.

## Где взять RouterAI API key

1. Зарегистрируйтесь или войдите в аккаунт RouterAI: <https://routerai.ru/>.
2. Откройте раздел с API-ключами в личном кабинете.
3. Создайте новый API key.
4. Пополните баланс или подключите оплату, если это требуется для выбранной модели.
5. Сохраните ключ в `.env` как `ROUTERAI_API_KEY`.

По умолчанию бот использует RouterAI base URL `https://routerai.ru/api/v1`. Его можно изменить в `config.yaml` через `stt.routerai_base_url`.

Не коммитьте API key в репозиторий. Файл `.env` уже добавлен в `.gitignore`.

## Установка

Склонируйте репозиторий:

```bash
git clone https://github.com/dilukhin/unbazarbot.git
cd unbazarbot
```

Создайте виртуальное окружение и установите зависимости.

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Настройка `.env`

Создайте файл `.env` в корне проекта:

```env
TELEGRAM_BOT_TOKEN=put_telegram_bot_token_here
ROUTERAI_API_KEY=put_routerai_api_key_here
CONFIG_PATH=config.yaml
DB_PATH=data/bot.sqlite3
```

Обязательные переменные:

- `TELEGRAM_BOT_TOKEN` - token Telegram-бота от BotFather.
- `ROUTERAI_API_KEY` - API key RouterAI.

Опциональные переменные:

- `CONFIG_PATH` - путь к YAML-конфигу. По умолчанию `config.yaml`.
- `DB_PATH` - путь к SQLite-базе. По умолчанию `data/bot.sqlite3`.

## Настройка `config.yaml`

Основные разделы:

- `telegram.bot_username` - username бота без `@` или с ним, как принято в вашей конфигурации.
- `telegram.admin_user_ids` - Telegram user ID администраторов бота.
- `access` - правила доступа в личных чатах и группах.
- `limits` - ограничения на аудио.
- `stt` - настройки распознавания и список моделей.

Пример структуры:

```yaml
telegram:
  bot_username: your_bot_username
  admin_user_ids:
    - 123456789

access:
  allow_private_transcription_for_admins: true
  allow_private_transcription_for_non_admins: false
  one_time_approval_jobs: 1
  notify_admins_on_new_request: true

limits:
  max_audio_seconds: 600
  max_file_mb: 20
  admin_notify_cooldown_seconds: 300

stt:
  routerai_base_url: https://routerai.ru/api/v1
  default_model: whisper
  language: ru
  temperature: 0
  models:
    whisper:
      provider_model: openai/whisper-1
      description: Базовая модель распознавания
      format: ogg
```

### Как узнать свой Telegram user ID

Варианты:

- Написать любому боту, который показывает ваш Telegram ID, например `@userinfobot`.
- Временно добавить логирование входящих сообщений и посмотреть `from_user.id`.
- Использовать Telegram API tooling, если он уже есть в вашей инфраструктуре.

`admin_user_ids` не является секретом, но это персональные идентификаторы. Не публикуйте их, если не хотите раскрывать связь аккаунтов с этим ботом.

## Запуск вручную

Активируйте виртуальное окружение и запустите:

```bash
python bot.py
```

При успешном запуске бот начнет long polling. Webhook при старте удаляется с `drop_pending_updates=True`, чтобы не обрабатывать старые события после экспериментов.

## Запуск через systemd

В репозитории есть пример unit-файла: `systemd/unbazarbot.service.example`.

Пример установки на Linux-сервере, если проект лежит в `/home/dilukhin/unbazarbot`:

```bash
sudo cp systemd/unbazarbot.service.example /etc/systemd/system/unbazarbot.service
sudo systemctl daemon-reload
sudo systemctl enable unbazarbot
sudo systemctl start unbazarbot
sudo systemctl status unbazarbot
```

Если проект лежит в другом каталоге, измените в unit-файле:

- `WorkingDirectory`
- `EnvironmentFile`
- `ExecStart`

Логи systemd:

```bash
journalctl -u unbazarbot -f
```

## Команды бота

Общие команды:

- `/start` - регистрация админского личного чата или краткое описание бота.
- `/help` - список команд.
- `/status` - статус текущего личного чата или группы.
- `/model` - текущая модель и список доступных моделей.
- `/model set <alias>` - выбрать модель для текущей группы.
- `/tr [model]` - распознать voice/audio из reply.
- `/auto_on` - включить автоматическое распознавание новых voice/audio в группе.
- `/auto_off` - выключить автоматическое распознавание в группе.

Админские команды:

- `/requests` - показать pending-заявки групп.
- `/groups` - показать известные группы и их статус.
- `/revoke <chat_id>` - отозвать доступ у группы.

## Сценарий подключения группы

1. Администратор бота пишет боту `/start` в личном чате. Так бот запоминает, куда отправлять заявки.
2. Бота добавляют в группу.
3. Бот создает заявку и отправляет ее администраторам в личку.
4. Администратор выбирает действие inline-кнопкой:
   - `Разрешить 1 раз`
   - `Разрешить навсегда`
   - `Отклонить`
5. После разрешения участники группы могут использовать `/tr` reply на voice/audio.
6. Для постоянного автоматического режима в группе выполните `/auto_on`.

## Хранилище данных

По умолчанию база находится в `data/bot.sqlite3`. SQLite работает в WAL-режиме, поэтому рядом могут появляться файлы:

- `data/bot.sqlite3`
- `data/bot.sqlite3-wal`
- `data/bot.sqlite3-shm`

Эти файлы являются runtime-данными и не входят в Git. В базе хранятся:

- администраторы;
- группы и их статусы доступа;
- заявки на доступ;
- задачи распознавания;
- кеш расшифровок;
- audit log.

## Что не нужно коммитить

Уже исключено через `.gitignore`:

- `.env` и `.env.*` - секреты и локальные настройки;
- `data/` - SQLite runtime-данные;
- `logs/` - логи;
- `.venv/`, `venv/`, `env/` - виртуальные окружения;
- `__pycache__/`, `*.pyc` - Python cache;
- `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/` - локальные кеши инструментов.

## Обновление зависимостей

Для обновления зависимостей вручную:

```bash
source .venv/bin/activate
python -m pip install --upgrade -r requirements.txt
```

На Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade -r requirements.txt
```

После обновления проверьте запуск бота и совместимость выбранных моделей RouterAI.

## Диагностика

### `TELEGRAM_BOT_TOKEN is missing in .env`

Файл `.env` не найден или в нем нет `TELEGRAM_BOT_TOKEN`.

### `ROUTERAI_API_KEY is missing in .env`

Файл `.env` не найден или в нем нет `ROUTERAI_API_KEY`.

### `Config file not found: config.yaml`

Проверьте `CONFIG_PATH` в `.env` и наличие `config.yaml`.

### Группа не получает доступ

Проверьте, что администратор бота написал `/start` в личке. Без этого бот может создать заявку, но ему некуда отправить уведомление.

### Аудио не распознается автоматически

Проверьте, что группа имеет статус `approved`, а в группе выполнена команда `/auto_on`.

### RouterAI возвращает HTTP-ошибку

Проверьте:

- корректность `ROUTERAI_API_KEY`;
- баланс или лимиты аккаунта RouterAI;
- доступность `stt.routerai_base_url`;
- корректность `provider_model` в `config.yaml`;
- формат аудио и размер файла.

## Безопасность

- Не публикуйте `.env`.
- Не отправляйте Telegram token и RouterAI key в чаты.
- При утечке Telegram token перевыпустите его у `@BotFather`.
- При утечке RouterAI key удалите или перевыпустите ключ в личном кабинете RouterAI.
- Не добавляйте SQLite-базу в публичный репозиторий: там могут быть chat ID, user ID, тексты заявок и расшифровки.

## Лицензия

Лицензия в репозитории не указана. Если проект планируется использовать публично, добавьте `LICENSE`.
