"""MAX webhook entry point. A separate process and SQLite file from Telegram.

Run behind a TLS reverse proxy on port 443. Subscription creation is an operator
action and is deliberately not performed at startup.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import logging
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from dotenv import load_dotenv

from .config import MaxConfig, load_max_config
from .storage import MaxStorage
from voicebot.stt_routerai import RouterAITranscriber

LOG = logging.getLogger(__name__)
API = "https://platform-api2.max.ru"
FORMATS = {"audio/ogg": "ogg", "audio/opus": "opus", "audio/wav": "wav",
           "audio/x-wav": "wav", "audio/mp4": "m4a", "audio/m4a": "m4a",
           "audio/x-m4a": "m4a", "audio/webm": "webm", "audio/flac": "flac"}
EXTENSIONS = {".ogg": "ogg", ".oga": "ogg", ".opus": "opus", ".wav": "wav",
              ".m4a": "m4a", ".webm": "webm", ".flac": "flac"}


def audio_attachment(message: dict) -> dict | None:
    for item in (message.get("body") or {}).get("attachments") or []:
        if item.get("type") == "audio":
            return item
        if item.get("type") == "file" and Path(str(item.get("filename") or "").lower()).suffix in EXTENSIONS:
            return item
    return None


def media_url(attachment: dict, config: MaxConfig) -> str:
    url = str((attachment.get("payload") or {}).get("url") or "")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or
            not parsed.hostname or parsed.hostname.lower() not in config.download_hosts):
        raise ValueError("Недопустимый адрес аудиовложения Max")
    return url


def infer_format(attachment: dict, content_type: str) -> str:
    filename = str(attachment.get("filename") or "").lower()
    suffix = Path(filename).suffix
    if suffix == ".mp3" or content_type.split(";")[0].lower() in {"audio/mpeg", "audio/mp3"}:
        raise ValueError("MP3 отключён")
    result = EXTENSIONS.get(suffix) or FORMATS.get(content_type.split(";")[0].lower())
    if not result:
        raise ValueError("Формат аудио не поддерживается")
    return result


class MaxBot:
    def __init__(self, config: MaxConfig, storage: MaxStorage, token: str, routerai_key: str):
        self.config, self.storage, self.token = config, storage, token
        self.stt = RouterAITranscriber(routerai_key, base_url=config.base_url)
        self.http = httpx.AsyncClient(timeout=30, headers={"Authorization": token})
        self.send_times: dict[int, float] = {}
        self.dialogs: dict[int, int] = {}

    async def close(self) -> None:
        await self.http.aclose()

    async def api(self, method: str, path: str, **kwargs) -> dict:
        response = await self.http.request(method, API + path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def send(self, text: str, *, chat_id: int | None = None,
                   user_id: int | None = None, reply_mid: str | None = None) -> None:
        target = chat_id if chat_id is not None else user_id
        if target is None:
            return
        direct_user = user_id if user_id is not None else self.dialogs.get(target)
        for index in range(0, len(text), 3800):
            # MAX allows at most two messages per second per chat.
            now = asyncio.get_running_loop().time()
            await asyncio.sleep(max(0, self.send_times.get(target, 0) - now))
            body: dict = {"text": text[index:index + 3800]}
            if reply_mid:
                body["link"] = {"type": "reply", "mid": reply_mid}
            await self.api("POST", "/messages", params={"user_id" if direct_user is not None else "chat_id": direct_user if direct_user is not None else target}, json=body)
            self.send_times[target] = asyncio.get_running_loop().time() + 0.55

    async def download(self, attachment: dict) -> tuple[Path, str]:
        url = media_url(attachment, self.config)
        size = attachment.get("size")
        if size is not None and int(size) > self.config.max_bytes:
            raise ValueError("Аудио превышает лимит размера")
        # Never send our MAX token to a CDN. Do not follow redirects to other hosts.
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                if response.is_redirect:
                    raise ValueError("Перенаправление аудиовложения запрещено")
                declared = response.headers.get("content-length")
                if declared and int(declared) > self.config.max_bytes:
                    raise ValueError("Аудио превышает лимит размера")
                fmt = infer_format(attachment, response.headers.get("content-type", ""))
                with tempfile.NamedTemporaryFile(prefix="unbazar_max_", suffix="." + fmt, delete=False) as stream:
                    path = Path(stream.name)
                    try:
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > self.config.max_bytes:
                                raise ValueError("Аудио превышает лимит размера")
                            stream.write(chunk)
                    except BaseException:
                        path.unlink(missing_ok=True)
                        raise
                return path, fmt

    async def transcribe(self, event_message: dict, target: dict, model: str, private: bool) -> None:
        sender = event_message.get("sender") or {}
        recipient = event_message.get("recipient") or {}
        chat_id = int(recipient.get("chat_id") or sender["user_id"])
        mid = str((target.get("body") or {})["mid"])
        attachment = audio_attachment(target)
        if not attachment:
            await self.send("В сообщении нет поддерживаемого аудиовложения.", chat_id=chat_id)
            return
        if model not in self.config.models:
            await self.send("Неизвестная модель. Список: /model", chat_id=chat_id)
            return
        if attachment.get("duration") and int(attachment["duration"]) > self.config.max_seconds:
            await self.send("Аудио превышает лимит длительности.", chat_id=chat_id)
            return
        if not private:
            group = await self.storage.group(chat_id)
            if not group or (group["state"] != "approved" and not (group["state"] == "once" and group["remaining"] > 0)):
                if await self.storage.request(chat_id, sender.get("user_id")):
                    for admin in self.config.admin_ids:
                        try:
                            await self.send(f"Заявка чата {chat_id}. /approve_once {chat_id}, /approve_forever {chat_id} или /reject {chat_id}", user_id=admin)
                        except httpx.HTTPError:
                            LOG.warning("MAX admin has not started a dialog or API is unavailable")
                await self.send("Чат ожидает разрешения администратора бота.", chat_id=chat_id)
                return
        cached = await self.storage.cached(chat_id, mid, model)
        if cached:
            await self.send("Расшифровка из кеша:\n\n" + cached, chat_id=chat_id, reply_mid=mid)
            return
        # Check format and source before consuming the one-time approval.
        try:
            url = media_url(attachment, self.config)
            if Path(urlsplit(url).path.lower()).suffix == ".mp3" or Path(str(attachment.get("filename") or "").lower()).suffix == ".mp3":
                raise ValueError("MP3 отключён")
        except ValueError as exc:
            await self.send(str(exc), chat_id=chat_id)
            return
        path: Path | None = None
        reserved = False
        try:
            path, fmt = await self.download(attachment)
            if not await self.storage.reserve(chat_id, mid, model, private):
                return  # another event owns this job or permission has been exhausted
            reserved = True
            result = await self.stt.transcribe(path, model=self.config.models[model],
                                               audio_format=fmt, language=self.config.language)
            await self.storage.finish_job(chat_id, mid, model, result.text)
            await self.send("Расшифровка:\n\n" + result.text, chat_id=chat_id, reply_mid=mid)
        except Exception as exc:
            if reserved:
                await self.storage.finish_job(chat_id, mid, model, None, type(exc).__name__)
            LOG.error("MAX transcription failed in chat %s: %s", chat_id, type(exc).__name__)
            await self.send("Не удалось распознать аудио. Оператор может проверить журнал задачи.", chat_id=chat_id)
        finally:
            if path:
                path.unlink(missing_ok=True)

    async def handle(self, update: dict) -> None:
        kind = update.get("update_type")
        if kind == "bot_added" and not update.get("is_channel"):
            chat_id = int(update["chat_id"])
            await self.storage.ensure_group(chat_id)
            await self.storage.request(chat_id, (update.get("user") or {}).get("user_id"))
            for admin in self.config.admin_ids:
                try:
                    await self.send(f"Бот добавлен в чат {chat_id}. /approve_once {chat_id} или /approve_forever {chat_id}", user_id=admin)
                except httpx.HTTPError:
                    LOG.warning("Could not notify MAX admin")
            return
        if kind != "message_created":
            return
        message = update.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("body"), dict):
            LOG.warning("MAX message_created without message body; native voice may be unavailable")
            return
        recipient, sender, body = message.get("recipient") or {}, message.get("sender") or {}, message["body"]
        if sender.get("is_bot") or not sender.get("user_id"):
            return
        user_id = int(sender["user_id"])
        private = recipient.get("user_id") is not None or recipient.get("chat_type") == "dialog"
        if not private and (recipient.get("chat_type") != "chat" or not recipient.get("chat_id")):
            return
        chat_id = int(recipient.get("chat_id") or user_id)
        if private:
            self.dialogs[chat_id] = user_id
        admin = user_id in self.config.admin_ids
        text = str(body.get("text") or "").strip()
        parts = text.split()
        command, args = (parts[0], parts[1:]) if parts else ("", [])
        command = command.split("@", 1)[0].lower() if command.startswith("/") else ""
        if private and command in {"/start", "/help"}:
            await self.send("/tr [модель] — ответьте на аудио; /model — модели. Администратор: /requests, /approve_once, /approve_forever, /reject, /revoke.", chat_id=chat_id)
            return
        if private and admin and command == "/requests":
            pending = await self.storage.pending()
            await self.send("Заявки: " + (", ".join(str(row[0]) for row in pending) or "нет"), chat_id=chat_id)
            return
        if private and admin and command in {"/approve_once", "/approve_forever", "/reject", "/revoke"}:
            if len(args) != 1 or not args[0].lstrip("-").isdigit():
                await self.send("Укажите числовой ID чата.", chat_id=chat_id)
                return
            target_chat = int(args[0])
            if command == "/revoke":
                await self.storage.revoke(target_chat)
                success = True
            else:
                success = await self.storage.decide(target_chat, {
                    "/approve_once": "once", "/approve_forever": "always", "/reject": "reject"
                }[command])
            await self.send("Готово." if success else "Заявка не найдена или уже решена.", chat_id=chat_id)
            return
        if private and not admin:
            if command:
                await self.send("Расшифровка в личном чате доступна только администраторам бота.", chat_id=chat_id)
            return
        if not private:
            await self.storage.ensure_group(chat_id)
        group = await self.storage.group(chat_id) if not private else None
        if command == "/model":
            if len(args) == 2 and args[0] == "set" and args[1] in self.config.models and group and group["state"] == "approved":
                await self.storage.set_model(chat_id, args[1])
                await self.send("Модель сохранена.", chat_id=chat_id)
            else:
                await self.send("Модели: " + ", ".join(self.config.models), chat_id=chat_id)
            return
        if command in {"/auto_on", "/auto_off"} and not private:
            enabled = command == "/auto_on"
            success = await self.storage.set_auto(chat_id, enabled)
            await self.send("Автоматический режим изменён." if success else "Нужно постоянное разрешение чата.", chat_id=chat_id)
            return
        if command == "/tr":
            link = message.get("link") or {}
            linked_body = link.get("message") or {}
            if link.get("type") != "reply" or not isinstance(linked_body, dict) or not linked_body.get("mid"):
                await self.send("Ответьте командой /tr на аудиосообщение.", chat_id=chat_id)
                return
            model = args[0] if args else (group or {}).get("model") or self.config.default_model
            await self.transcribe(message, {"body": linked_body}, model, private)
            return
        if not command and audio_attachment(message) and (private or (group and group["state"] == "approved" and group["auto_enabled"])):
            await self.transcribe(message, message, (group or {}).get("model") or self.config.default_model, private)


def create_app() -> FastAPI:
    load_dotenv()
    token = os.environ["MAX_BOT_TOKEN"]
    secret = os.environ["MAX_WEBHOOK_SECRET"]
    routerai_key = os.environ["ROUTERAI_API_KEY"]
    if len(secret) < 5:
        raise ValueError("MAX_WEBHOOK_SECRET is too short")
    config = load_max_config(os.getenv("MAX_CONFIG_PATH", "max_config.yaml"))
    storage = MaxStorage(os.getenv("MAX_DB_PATH", "data/maxbot.sqlite3"))
    bot = MaxBot(config, storage, token, routerai_key)

    async def worker():
        while True:
            item = await storage.next_event()
            if not item:
                await asyncio.sleep(0.3)
                continue
            key, payload = item
            try:
                await bot.handle(payload)
            except Exception as exc:
                LOG.error("MAX update processing failed: %s", type(exc).__name__)
                await storage.finish_event(key, type(exc).__name__)
            else:
                await storage.finish_event(key)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await storage.open()
        task = asyncio.create_task(worker())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await bot.close()
            await storage.close()

    app = FastAPI(lifespan=lifespan)

    @app.post("/max/webhook")
    async def webhook(request: Request, x_max_bot_api_secret: str | None = Header(default=None)):
        if not x_max_bot_api_secret or not hmac.compare_digest(x_max_bot_api_secret, secret):
            raise HTTPException(status_code=403)
        if int(request.headers.get("content-length") or 0) > 1024 * 1024:
            raise HTTPException(status_code=413)
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            raise HTTPException(status_code=413)
        try:
            update = await request.json()
        except ValueError:
            raise HTTPException(status_code=400)
        if not isinstance(update, dict):
            raise HTTPException(status_code=400)
        message = update.get("message") or {}
        mid = (message.get("body") or {}).get("mid")
        key = f"{update.get('update_type')}:{mid or update.get('chat_id') or ''}:{update.get('timestamp')}"
        await storage.enqueue(key, update)
        return {"ok": True}

    return app


# Uvicorn factory: uvicorn maxbot.app:create_app --factory --host 127.0.0.1 --port 8080
