# Unbazarbot — текущее состояние

Проверено: 2026-09-18  
Ветка: `main`  
Проверенный HEAD до документационного обновления: `dc0dbba33d51cb88f595a31617e98e647349de2c`.

Этот файл — mutable status; SHA здесь допустим и должен обновляться при существенных изменениях.

## Реализовано в GitHub main

- Python/aiogram long polling entry point.
- RouterAI STT adapter через `/audio/transcriptions`.
- YAML config с несколькими STT model aliases.
- Telegram admin IDs из config.
- SQLite persistence.
- Заявка группы при добавлении бота.
- Admin approval: one-time / forever / reject.
- `/requests`, `/groups`, `/revoke`.
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

Production lifecycle через systemd следует считать настроенным только после отдельной runtime-проверки; наличие example unit в Git не доказывает, что service включён на VPS.

## Известные ограничения / расхождения

1. В текущем `main` нет отдельного LLM post-processing для пунктуации и абзацев.
2. В текущем `main` нет конфигурируемого запрета MP3; существующая media validation проверяет прежде всего duration/size.
3. Auto mode действует на весь `chat_id`, не на отдельный forum topic.
4. Нет automated test suite и CI, подтверждённых текущим деревом репозитория.
5. Нет rate/cost caps по группе/пользователю.
6. Runtime bundle `dist/projects/dilukhin__unbazarbot.md` в `github-connector-knowledge` на момент проверки отсутствовал; bootstrap обязан уметь работать без него.

## Документационный контур

С 2026-09-18 основная mutable документация должна жить в GitHub. Project Sources ChatGPT должен содержать минимальный bootstrap, а не копии status/roadmap.
