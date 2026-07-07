from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import contextlib

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .config import AppConfig
from .db import Database, AccessRequest
from .media import MediaRef, download_media_to_temp, extract_media, guess_audio_format
from .stt_routerai import RouterAITranscriber
from .textfmt import chunks, user_label


@dataclass
class AppContext:
    config: AppConfig
    db: Database
    transcriber: RouterAITranscriber


router = Router()


def admin_buttons(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Разрешить 1 раз", callback_data=f"ap1:{request_id}"),
                InlineKeyboardButton(text="Разрешить навсегда", callback_data=f"apf:{request_id}"),
            ],
            [InlineKeyboardButton(text="Отклонить", callback_data=f"rej:{request_id}")],
        ]
    )


def request_text(req: AccessRequest) -> str:
    return (
        "Новая заявка на использование бота\n\n"
        f"Группа: {req.chat_title or '(без названия)'}\n"
        f"Chat ID: {req.chat_id}\n"
        f"Запросил: {user_label(req.requester_user_id, req.requester_username)}\n"
        f"Причина: {req.reason}\n"
        f"Request ID: {req.id}"
    )


async def notify_admins(bot: Bot, ctx: AppContext, req: AccessRequest) -> None:
    if not ctx.config.notify_admins_on_new_request:
        return
    admin_chats = await ctx.db.admin_private_chats()
    if not admin_chats:
        return
    for chat_id in admin_chats:
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, request_text(req), reply_markup=admin_buttons(req.id))


async def create_request_for_message(
    message: Message,
    ctx: AppContext,
    reason: str,
    bot: Bot,
) -> AccessRequest:
    await ctx.db.ensure_group(
        chat_id=message.chat.id,
        title=message.chat.title or message.chat.full_name,
        username=message.chat.username,
        default_model=ctx.config.default_model,
    )
    from_user = message.from_user
    req = await ctx.db.create_or_get_pending_request(
        chat_id=message.chat.id,
        chat_title=message.chat.title or message.chat.full_name,
        requester_user_id=from_user.id if from_user else None,
        requester_username=from_user.username if from_user else None,
        reason=reason,
    )
    await notify_admins(bot, ctx, req)
    return req


async def require_admin(message: Message, ctx: AppContext) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if await ctx.db.is_admin(user_id):
        return True
    await message.answer("Эта команда доступна только администраторам бота.")
    return False


def is_group_chat(message: Message) -> bool:
    return message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


def is_private_chat(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


async def check_access_for_transcription(message: Message, ctx: AppContext, bot: Bot) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if is_private_chat(message):
        if await ctx.db.is_admin(user_id):
            return ctx.config.allow_private_transcription_for_admins
        return ctx.config.allow_private_transcription_for_non_admins

    if not is_group_chat(message):
        return False

    await ctx.db.ensure_group(
        chat_id=message.chat.id,
        title=message.chat.title,
        username=message.chat.username,
        default_model=ctx.config.default_model,
    )
    group = await ctx.db.get_group(message.chat.id)
    if group and group.status == "approved":
        return True
    if group and group.status == "approved_once" and group.remaining_jobs > 0:
        return True

    await create_request_for_message(message, ctx, "attempted transcription", bot)
    await message.answer("Группа ещё не разрешена. Заявка отправлена администраторам бота.")
    return False


def pick_model_alias(message: Message, command: CommandObject | None, ctx: AppContext) -> str:
    if command and command.args:
        maybe_alias = command.args.strip().split()[0]
        if maybe_alias in ctx.config.models:
            return maybe_alias
    # Group model is handled by caller when available. This fallback is for private chats.
    return ctx.config.default_model


async def resolve_model_alias(message: Message, command: CommandObject | None, ctx: AppContext) -> str:
    if command and command.args:
        maybe_alias = command.args.strip().split()[0]
        if maybe_alias in ctx.config.models:
            return maybe_alias
    if is_group_chat(message):
        group = await ctx.db.get_group(message.chat.id)
        if group and group.default_model in ctx.config.models:
            return group.default_model  # type: ignore[return-value]
    return ctx.config.default_model


def validate_media(media: MediaRef, ctx: AppContext) -> str | None:
    if media.duration is not None and media.duration > ctx.config.max_audio_seconds:
        return f"Аудио слишком длинное: {media.duration} сек. Лимит: {ctx.config.max_audio_seconds} сек."
    if media.file_size is not None:
        max_bytes = ctx.config.max_file_mb * 1024 * 1024
        if media.file_size > max_bytes:
            return f"Файл слишком большой: {media.file_size // 1024 // 1024} МБ. Лимит: {ctx.config.max_file_mb} МБ."
    return None


async def transcribe_message_media(
    message: Message,
    target_message: Message,
    ctx: AppContext,
    bot: Bot,
    model_alias: str,
) -> None:
    media = extract_media(target_message)
    if not media:
        await message.answer("В сообщении не найдено voice/audio. Используйте /tr как reply на голосовое или аудио.")
        return

    validation_error = validate_media(media, ctx)
    if validation_error:
        await message.answer(validation_error)
        return

    model_cfg = ctx.config.model(model_alias)
    cached = await ctx.db.cached_transcript(media.file_unique_id, model_alias)
    if cached:
        text = str(cached["transcript"])
        prefix = f"Расшифровка из кеша · модель: {model_alias}\n\n"
        first = True
        for part in chunks(text):
            await message.answer((prefix if first else "") + part, reply_to_message_id=target_message.message_id)
            first = False
        return

    await message.answer(f"Распознаю аудио моделью {model_alias}…", reply_to_message_id=target_message.message_id)

    job_id = await ctx.db.create_job(
        chat_id=message.chat.id,
        user_id=message.from_user.id if message.from_user else None,
        message_id=target_message.message_id,
        file_id=media.file_id,
        file_unique_id=media.file_unique_id,
        model_alias=model_alias,
        provider_model=model_cfg.provider_model,
    )

    tmp_path: Path | None = None
    try:
        tmp_path, telegram_file_path = await download_media_to_temp(bot, media)
        audio_format = guess_audio_format(media, telegram_file_path, model_cfg.audio_format)
        result = await ctx.transcriber.transcribe(
            tmp_path,
            model=model_cfg.provider_model,
            audio_format=audio_format,
            language=ctx.config.language,
            temperature=ctx.config.temperature,
        )
        await ctx.db.finish_job(job_id, result.text, result.cost, result.duration_seconds)
        if is_group_chat(message):
            await ctx.db.consume_one_time_job_if_needed(message.chat.id)

        meta = f"Расшифровка · модель: {model_alias}"
        if result.duration_seconds is not None:
            meta += f" · {result.duration_seconds:.1f} сек"
        if result.cost is not None:
            meta += f" · cost: {result.cost:g}"
        meta += "\n\n"

        first = True
        for part in chunks(result.text):
            await message.answer((meta if first else "") + part, reply_to_message_id=target_message.message_id)
            first = False
    except Exception as exc:
        await ctx.db.fail_job(job_id, str(exc))
        await message.answer(f"Не удалось распознать аудио: {exc}")
    finally:
        if tmp_path:
            with contextlib.suppress(Exception):
                tmp_path.unlink(missing_ok=True)


@router.message(Command("start"))
async def cmd_start(message: Message, ctx: AppContext) -> None:
    user = message.from_user
    if user and await ctx.db.is_admin(user.id) and is_private_chat(message):
        await ctx.db.register_admin_private_chat(user.id, message.chat.id, user.username, user.first_name)
        await message.answer(
            "Админский чат зарегистрирован. Теперь сюда будут приходить заявки групп.\n"
            "Команды: /requests, /groups, /help"
        )
        return
    await message.answer(
        "Бот расшифровывает voice/audio по команде /tr reply. "
        "Для групп требуется подтверждение администратора бота."
    )


@router.message(Command("help"))
async def cmd_help(message: Message, ctx: AppContext) -> None:
    await message.answer(
        "Команды:\n"
        "/tr [model] — распознать voice/audio из reply\n"
        "/model — список моделей\n"
        "/model set <alias> — выбрать модель для текущего чата\n"
        "/status — статус текущего чата\n"
        "/auto_on, /auto_off — авто-распознавание новых voice в группе\n\n"
        "Админские в личке:\n"
        "/requests — заявки групп\n"
        "/groups — список групп\n"
        "/revoke <chat_id> — отозвать доступ"
    )


@router.message(Command("status"))
async def cmd_status(message: Message, ctx: AppContext) -> None:
    if is_private_chat(message):
        is_admin = await ctx.db.is_admin(message.from_user.id if message.from_user else None)
        await message.answer(f"Личный чат. Admin: {'yes' if is_admin else 'no'}")
        return
    await ctx.db.ensure_group(message.chat.id, message.chat.title, message.chat.username, ctx.config.default_model)
    group = await ctx.db.get_group(message.chat.id)
    await message.answer(
        f"Статус группы: {group.status if group else 'unknown'}\n"
        f"Авто-режим: {'on' if group and group.auto_enabled else 'off'}\n"
        f"Модель: {(group.default_model if group and group.default_model else ctx.config.default_model)}\n"
        f"Осталось one-time jobs: {group.remaining_jobs if group else 0}"
    )


@router.message(Command("model"))
async def cmd_model(message: Message, command: CommandObject, ctx: AppContext, bot: Bot) -> None:
    args = (command.args or "").strip().split()
    if args and args[0] == "set":
        if len(args) < 2:
            await message.answer("Использование: /model set <alias>")
            return
        alias = args[1]
        if alias not in ctx.config.models:
            await message.answer(f"Неизвестная модель: {alias}. Напишите /model для списка.")
            return
        if is_private_chat(message):
            if not await require_admin(message, ctx):
                return
            # For private chat this only validates; there is no per-user setting in MVP.
            await message.answer(f"Модель {alias} существует. В личке используйте /tr {alias} как разовый выбор.")
            return
        if not await check_access_for_transcription(message, ctx, bot):
            return
        await ctx.db.set_group_model(message.chat.id, alias)
        await message.answer(f"Модель для группы: {alias}")
        return

    if is_group_chat(message):
        group = await ctx.db.get_group(message.chat.id)
        current = group.default_model if group and group.default_model else ctx.config.default_model
    else:
        current = ctx.config.default_model
    lines = [f"Текущая модель: {current}", "", "Доступные модели:"]
    for alias, model in ctx.config.models.items():
        lines.append(f"- {alias}: {model.provider_model} — {model.description}")
    await message.answer("\n".join(lines))


@router.message(Command("requests"))
async def cmd_requests(message: Message, ctx: AppContext) -> None:
    if not await require_admin(message, ctx):
        return
    requests = await ctx.db.list_pending_requests()
    if not requests:
        await message.answer("Pending-заявок нет.")
        return
    for req in requests:
        await message.answer(request_text(req), reply_markup=admin_buttons(req.id))


@router.message(Command("groups"))
async def cmd_groups(message: Message, ctx: AppContext) -> None:
    if not await require_admin(message, ctx):
        return
    groups = await ctx.db.list_groups()
    if not groups:
        await message.answer("Групп пока нет.")
        return
    lines = ["Группы:"]
    for group in groups:
        lines.append(
            f"\n{group.title or '(без названия)'}\n"
            f"chat_id: {group.chat_id}\n"
            f"status: {group.status}\n"
            f"auto: {'on' if group.auto_enabled else 'off'}\n"
            f"model: {group.default_model or ctx.config.default_model}\n"
            f"remaining_jobs: {group.remaining_jobs}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, ctx: AppContext) -> None:
    if not await require_admin(message, ctx):
        return
    if not command.args:
        await message.answer("Использование: /revoke <chat_id>")
        return
    try:
        chat_id = int(command.args.strip().split()[0])
    except ValueError:
        await message.answer("chat_id должен быть числом, например -1001234567890")
        return
    ok = await ctx.db.revoke_group(chat_id, message.from_user.id if message.from_user else 0)
    await message.answer("Доступ отозван." if ok else "Группа не найдена.")


@router.message(Command("auto_on"))
async def cmd_auto_on(message: Message, ctx: AppContext, bot: Bot) -> None:
    if not is_group_chat(message):
        await message.answer("/auto_on имеет смысл только в группе.")
        return
    if not await check_access_for_transcription(message, ctx, bot):
        return
    await ctx.db.set_group_auto(message.chat.id, True)
    await message.answer("Авто-распознавание новых voice/audio включено.")


@router.message(Command("auto_off"))
async def cmd_auto_off(message: Message, ctx: AppContext, bot: Bot) -> None:
    if not is_group_chat(message):
        await message.answer("/auto_off имеет смысл только в группе.")
        return
    if not await check_access_for_transcription(message, ctx, bot):
        return
    await ctx.db.set_group_auto(message.chat.id, False)
    await message.answer("Авто-распознавание выключено.")


@router.message(Command("tr"))
async def cmd_transcribe(message: Message, command: CommandObject, ctx: AppContext, bot: Bot) -> None:
    if not await check_access_for_transcription(message, ctx, bot):
        return
    target = message.reply_to_message
    if not target:
        await message.answer("Используйте /tr как reply на voice/audio сообщение.")
        return
    model_alias = await resolve_model_alias(message, command, ctx)
    await transcribe_message_media(message, target, ctx, bot, model_alias)


@router.message(F.voice | F.audio | F.document)
async def media_auto_or_private(message: Message, ctx: AppContext, bot: Bot) -> None:
    media = extract_media(message)
    if not media:
        return
    if is_private_chat(message):
        if not await check_access_for_transcription(message, ctx, bot):
            return
        model_alias = ctx.config.default_model
        await transcribe_message_media(message, message, ctx, bot, model_alias)
        return

    if is_group_chat(message):
        group = await ctx.db.get_group(message.chat.id)
        if not group or not group.auto_enabled or group.status != "approved":
            return
        model_alias = group.default_model or ctx.config.default_model
        await transcribe_message_media(message, message, ctx, bot, model_alias)


@router.my_chat_member()
async def my_chat_member_update(event: ChatMemberUpdated, ctx: AppContext, bot: Bot) -> None:
    chat = event.chat
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    new_status = event.new_chat_member.status
    if new_status not in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}:
        return

    await ctx.db.ensure_group(chat.id, chat.title, chat.username, ctx.config.default_model)
    req = await ctx.db.create_or_get_pending_request(
        chat_id=chat.id,
        chat_title=chat.title,
        requester_user_id=event.from_user.id if event.from_user else None,
        requester_username=event.from_user.username if event.from_user else None,
        reason="bot added to group",
    )
    await notify_admins(bot, ctx, req)
    with contextlib.suppress(Exception):
        await bot.send_message(chat.id, "Бот добавлен. Группа ожидает подтверждения администратором бота.")


@router.callback_query(F.data.startswith("ap1:"))
async def cb_approve_once(callback: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    if not await ctx.db.is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Только для админов бота", show_alert=True)
        return
    request_id = callback.data.split(":", 1)[1]
    req = await ctx.db.decide_request(
        request_id, "approve_once", callback.from_user.id, ctx.config.one_time_approval_jobs
    )
    await callback.answer("Разрешено 1 раз")
    if callback.message:
        await callback.message.edit_text(f"Заявка {request_id}: разрешено 1 раз")
    if req:
        with contextlib.suppress(Exception):
            await bot.send_message(req.chat_id, "Группа разрешена на одно распознавание. Используйте /tr reply на voice/audio.")


@router.callback_query(F.data.startswith("apf:"))
async def cb_approve_forever(callback: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    if not await ctx.db.is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Только для админов бота", show_alert=True)
        return
    request_id = callback.data.split(":", 1)[1]
    req = await ctx.db.decide_request(request_id, "approve_forever", callback.from_user.id, 0)
    await callback.answer("Разрешено навсегда")
    if callback.message:
        await callback.message.edit_text(f"Заявка {request_id}: разрешено навсегда")
    if req:
        with contextlib.suppress(Exception):
            await bot.send_message(req.chat_id, "Группа разрешена. Теперь работает /tr reply на voice/audio.")


@router.callback_query(F.data.startswith("rej:"))
async def cb_reject(callback: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    if not await ctx.db.is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Только для админов бота", show_alert=True)
        return
    request_id = callback.data.split(":", 1)[1]
    req = await ctx.db.decide_request(request_id, "reject", callback.from_user.id, 0)
    await callback.answer("Отклонено")
    if callback.message:
        await callback.message.edit_text(f"Заявка {request_id}: отклонена")
    if req:
        with contextlib.suppress(Exception):
            await bot.send_message(req.chat_id, "Заявка на использование бота отклонена.")
