from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from types import SimpleNamespace
import contextlib
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .access_ui import (admin_callback, decision_keyboard, entry_keyboard, home_keyboard,
                        keyboard, notify_access_result, notify_request, remove_decision_buttons,
                        request_keyboard, request_text, show_page)
from .config import AppConfig
from .db import Database, AccessRequest
from .media import MediaRef, download_media_to_temp, extract_media, guess_audio_format
from .stt_routerai import RouterAITranscriber
from .stt_routerai import ProviderCallError
from .provider_costs import GenerationCost, generation_id
from .money import to_atoms, MoneyError
from decimal import Decimal, localcontext
from .formatter import RouterAIFormatter
from .tariffs import PriceCatalog, Estimate
from .diagnostics import about_text, system_text
from .textfmt import chunks, user_label
from .audio_validation import prepare_audio, AudioError


@dataclass
class AppContext:
    config: AppConfig
    db: Database
    transcriber: RouterAITranscriber
    formatter: RouterAIFormatter | None = None
    prices: PriceCatalog | None = None
    audio_preparer: Any = prepare_audio
    audio_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


router = Router()


def admin_buttons(request_id: str) -> InlineKeyboardMarkup:
    # Совместимость со старыми сообщениями; новые уведомления используют единое меню.
    return keyboard([[('Разрешить 1 раз', f'ap1:{request_id}'),
                      ('Разрешить постоянно', f'apf:{request_id}')],
                     [('Отклонить', f'rej:{request_id}')]])


async def notify_admins(bot: Bot, ctx: AppContext, req: AccessRequest | None) -> None:
    await notify_request(bot, ctx, req)


async def create_request_for_message(
    message: Message,
    ctx: AppContext,
    reason: str,
    bot: Bot,
) -> AccessRequest | None:
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
        cooldown=ctx.config.admin_notify_cooldown_seconds,
    )
    await notify_admins(bot, ctx, req)
    return req


async def require_admin(message: Message, ctx: AppContext) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if is_private_chat(message) and await ctx.db.is_admin(user_id):
        return True
    await message.answer("Эта команда доступна администратору в личном чате с ботом.")
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
        if user_id and await ctx.db.private_user_allowed(user_id):
            return True
        await message.answer("Для личного распознавания нужно разрешение администратора.",
                             reply_markup=request_keyboard())
        return False

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

    await create_request_for_message(message, ctx, "Запрос распознавания в группе", bot)
    await message.answer("Доступ группы пока закрыт. Заявка может уже ожидать решения; после недавнего отказа действует пауза.")
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
        max_bytes = min(ctx.config.max_file_mb,20) * 1024 * 1024
        if media.file_size > max_bytes:
            return f"Файл слишком большой: {media.file_size // 1024 // 1024} МБ. Лимит: {max_bytes // 1024 // 1024} МБ."
    return None


async def transcribe_message_media(
    message: Message,
    target_message: Message,
    ctx: AppContext,
    bot: Bot,
    model_alias: str,
    require_auto: bool = False,
    raw: bool = False,
    extended: bool = False,
    media_ref: MediaRef | None = None,
) -> None:
    media = media_ref if extended and media_ref else extract_media(target_message,extended=extended)
    if extended and not await ctx.db.extended_audio_allowed(message.from_user.id if message.from_user else None):
        await message.answer('Для /trf требуется отдельное личное разрешение администратора.')
        return
    if not media:
        await message.answer("В сообщении нет голосового сообщения Telegram. Используйте /tr в ответ на голосовое.")
        return

    validation_error = validate_media(media, ctx)
    if validation_error:
        if not require_auto:
            await message.answer(validation_error)
        return

    model_cfg = ctx.config.model(model_alias)
    cached = await ctx.db.cached_transcript(media.file_unique_id, model_alias, model_cfg.provider_model)
    if cached:
        text = str(cached["transcript"])
        if not raw and ctx.config.formatter.enabled and cached.get('formatter_signature') == ctx.config.formatter.signature:
            text = cached.get('formatted_transcript') or text
        prefix = f"Расшифровка из кеша · модель: {model_alias}\n\n"
        first = True
        for part in chunks(text):
            await message.answer((prefix if first else "") + part, reply_to_message_id=target_message.message_id)
            first = False
        return

    def quote():
        return (ctx.prices.estimate_audio(model_cfg.provider_model, ctx.config.max_audio_seconds + 1)
                if ctx.prices else Estimate("unavailable", "catalog-service-missing"))

    if not require_auto and (message.from_user is None or getattr(message.from_user,'is_bot',False) or getattr(message,'sender_chat',None) is not None):
        await message.answer('Для личного списания требуется достоверный пользователь Telegram.')
        return

    estimate = quote()
    if not estimate.available:
        if not require_auto:
            await message.answer("Новый платный запрос недоступен: нет безопасной проверенной оценки стоимости (" + estimate.status + "). Кеш остаётся доступен.")
        return

    if require_auto:
        try:
            balance = await ctx.db.money_balance('group', message.chat.id)
            if balance['free_atoms'] + balance['extra_atoms'] < to_atoms(estimate.max_rub, rub=True):
                return
        except MoneyError:
            return
    else:
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
    prepared = None
    slot_owned = False
    paid_started = False
    completed = False
    try:
        reason = await ctx.db.reserve_financial_call(
            quote=quote, provider_model=model_cfg.provider_model,
            job_id=job_id, chat_id=message.chat.id,
            user_id=message.from_user.id if message.from_user else None,
            private=is_private_chat(message), file_unique_id=media.file_unique_id,
            model_alias=model_alias, seconds=media.duration or ctx.config.max_audio_seconds,
            limits=ctx.config.budget,
            allow_private_admins=ctx.config.allow_private_transcription_for_admins,
            require_auto=require_auto,
            require_extended=extended,
        )
        if reason:
            await ctx.db.fail_job(job_id, reason)
            if not require_auto:
                await message.answer(reason)
            return
        if ctx.audio_slots.locked():
            raise MoneyError('Бот занят подготовкой аудио. Повторите позже.')
        await ctx.audio_slots.acquire()
        slot_owned = True
        max_bytes=min(ctx.config.max_file_mb*1024*1024,20*1024*1024)
        tmp_path, telegram_file_path = await download_media_to_temp(bot, media, max_bytes=max_bytes)
        prepared = await ctx.audio_preparer(tmp_path,max_seconds=ctx.config.max_audio_seconds,
                                            max_bytes=max_bytes,audio_format=model_cfg.audio_format or 'ogg')
        await ctx.db.adjust_reserved_seconds(job_id,prepared.seconds,ctx.config.budget)
        ctx.audio_slots.release()
        slot_owned = False
        estimate = quote()
        if not estimate.available:
            await ctx.db.fail_job(job_id, "TariffUnavailable")
            await ctx.db.release_financial_not_sent(job_id)
            if not require_auto:
                await message.answer("Проверенная стоимость стала недоступна; платный запрос остановлен.")
            return
        async def before_send():
            await ctx.db.mark_financial_sending(job_id, quote, ctx.config.allow_private_transcription_for_admins)
        await before_send()
        paid_started = True
        result = await ctx.transcriber.transcribe(
            prepared.path,
            model=model_cfg.provider_model,
            audio_format=prepared.audio_format,
            language=ctx.config.language,
            temperature=ctx.config.temperature,
            before_send=before_send,
        )
        output_text = result.text
        # Additional paid formatting requires its own atomic monetary reserve (#20).
        # Until then persist and return STT directly, including when legacy config
        # still enables formatter. Existing formatted cache remains free to read.
        await ctx.db.record_financial_response(job_id, result)
        completed = True
        details = GenerationCost('unavailable')
        identity = generation_id(getattr(result,'generation_id',None))
        if identity:
            details = await ctx.transcriber.generation_cost(identity,model_cfg.provider_model)
        await ctx.db.finalize_financial_response(job_id,details)
        financial = await ctx.db.financial_call(job_id)
        if financial and financial['state']=='overrun':
            for destination in await ctx.db.admin_private_chats():
                with contextlib.suppress(Exception):
                    await bot.send_message(destination,'Расход превысил резерв. Модель приостановлена: '+model_cfg.provider_model+'. Задание: '+job_id)

        meta = f"Расшифровка · модель: {model_alias}"
        if result.duration_seconds is not None:
            meta += f" · {result.duration_seconds:.1f} сек"
        if result.cost is not None:
            meta += f" · расход RouterAI по ответу: {result.cost:g} ₽"
        if financial and financial['state']!='settled':
            meta += ' · стоимость ожидает сверки'
        meta += "\n\n"

        first = True
        for part in chunks(output_text):
            await message.answer((meta if first else "") + part, reply_to_message_id=target_message.message_id)
            first = False
    except asyncio.CancelledError:
        if paid_started and not completed:
            # Commit may have succeeded immediately before cancellation was delivered.
            completed = bool(await asyncio.shield(ctx.db.cached_transcript(media.file_unique_id, model_alias, model_cfg.provider_model)))
        released = await asyncio.shield(ctx.db.release_financial_not_sent(job_id))
        if not released:
            await asyncio.shield(ctx.db.finish_paid_call(job_id, 'interrupted'))
            await asyncio.shield(ctx.db.interrupt_financial_call(job_id))
        if not completed:
            await asyncio.shield(ctx.db.fail_job(job_id, 'Отменено до отправки' if released else 'Процесс прерван; исход оплаты неизвестен'))
        raise
    except Exception as exc:
        if not completed:
            await ctx.db.fail_job(job_id, type(exc).__name__)
        if paid_started:
            await ctx.db.finish_paid_call(job_id, 'error')
        released = await ctx.db.release_financial_not_sent(job_id, proven_not_sent=isinstance(exc,ProviderCallError) and not exc.sent)
        if not released:
            await ctx.db.interrupt_financial_call(job_id,getattr(exc,'generation_id',None))
        logging.error('Ошибка задания распознавания: %s', type(exc).__name__)
        if not require_auto or (not released and not isinstance(exc,(MoneyError,AudioError))):
            await message.answer(str(exc) if isinstance(exc,(MoneyError,AudioError)) else 'Не удалось завершить распознавание. Администратор может проверить состояние задания; платный вызов автоматически не повторяется.')
    finally:
        if slot_owned:
            ctx.audio_slots.release()
        if prepared is not None:
            prepared.close()
        if tmp_path:
            with contextlib.suppress(Exception):
                tmp_path.unlink(missing_ok=True)


@router.message(Command("start"))
async def cmd_start(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if is_private_chat(message) and user:
        if await ctx.db.is_admin(user.id):
            await ctx.db.register_admin_private_chat(user.id, message.chat.id, user.username, user.first_name)
            await message.answer("Здесь будут приходить заявки людей и групп. Управление: /access.",
                                 reply_markup=entry_keyboard())
        else:
            await ctx.db.ensure_private_user(user.id, user.full_name, user.username)
            allowed = await ctx.db.private_user_allowed(user.id)
            await message.answer("Личный доступ разрешён. Отправьте голосовое сообщение." if allowed else
                                 "Для распознавания в личном чате запросите разрешение администратора.",
                                 reply_markup=None if allowed else request_keyboard())
        return
    await message.answer("Для групп требуется разрешение администратора бота. "
                         "Используйте /tr в ответ на голосовое сообщение.")


@router.message(Command("help"))
async def cmd_help(message: Message, ctx: AppContext) -> None:
    await message.answer(
        "Команды:\n"
        "/tr [model] — распознать голосовое сообщение из ответа\n"
        "/tr_raw [model] — получить исходную расшифровку без оформления\n"
        "/model — список моделей\n"
        "/model set <alias> — выбрать модель для текущего чата\n"
        "/status — статус текущего чата\n"
        "/about — назначение и версия бота\n"
        "/auto_on, /auto_off — авто-распознавание новых voice в группе\n\n"
        "Админские в личке:\n"
        "/access — управление доступом людей и групп\n"
        "/stats — расход и лимиты за сутки UTC\n"
        "/system — сервер и среда запуска\n"
        "/requests — заявки людей и групп\n"
        "/groups — список групп\n"
        "/revoke <chat_id> — отозвать доступ"
    )


@router.message(Command('about'))
async def cmd_about(message: Message) -> None:
    await message.answer(about_text())


@router.message(Command('system'))
async def cmd_system(message: Message, ctx: AppContext) -> None:
    if await require_admin(message,ctx):
        for part in chunks(system_text(ctx)):
            await message.answer(part)


@router.message(Command("stats"))
async def cmd_stats(message: Message, ctx: AppContext) -> None:
    if not await require_admin(message, ctx):
        return
    rows = await ctx.db.usage_today()
    calls = sum(row['calls'] for row in rows)
    minutes = sum(row['seconds'] for row in rows) / 60
    known = sum(row['known_cost'] for row in rows)
    with localcontext() as decimal_context:
        decimal_context.prec=500
        exact_known=sum((row['known_cost_rub'] for row in rows),Decimal(0))
    unknown = sum(row['unknown_cost'] for row in rows)
    limits = ctx.config.budget
    # Legacy REAL figures remain visibly separate from exact new RUB accounting.
    lines = [f'За сутки UTC: {calls} новых попыток; зарезервировано {minutes:g} мин из {limits.daily_minutes_total}.',
             f'Точный подтверждённый расход: {exact_known:f} ₽; ожидают сверки: {unknown}.',
             f'Историческая оценка прежнего учёта: {known:g}.',
             f'Лимиты минут: чат {limits.daily_minutes_chat}, человек {limits.daily_minutes_user}.',
             f'Запросов в минуту: чат {limits.requests_per_minute_chat}, человек {limits.requests_per_minute_user}.']
    lines.append(ctx.prices.summary() if ctx.prices else "Тарифы: unavailable; денежный каталог не подключён.")
    for row in rows:
        lines.append(f"Чат {row['chat_id']}, модель {row['model_alias']}: {row['calls']} попыток, {row['seconds']/60:g} мин.")
    for part in chunks('\n'.join(lines)):
        await message.answer(part)


@router.message(Command("status"))
async def cmd_status(message: Message, ctx: AppContext) -> None:
    if is_private_chat(message):
        user_id = message.from_user.id if message.from_user else None
        is_admin = await ctx.db.is_admin(user_id)
        allowed = ctx.config.allow_private_transcription_for_admins if is_admin else await ctx.db.private_user_allowed(user_id)
        await message.answer(f"Личный чат. Администратор: {'да' if is_admin else 'нет'}. "
                             f"Распознавание: {'разрешено' if allowed else 'запрещено'}.")
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
        estimate = (ctx.prices.estimate_audio(model.provider_model, ctx.config.max_audio_seconds + 1)
                    if ctx.prices else Estimate("unavailable"))
        tariff = (f"предварительно до {estimate.max_rub} ₽ / {estimate.bazariki} Б за предел {ctx.config.max_audio_seconds + 1} с"
                  if estimate.available else f"цена/ограничение недоступны: {estimate.status}")
        details = ctx.prices.model_summary(model.provider_model) if ctx.prices else "каталог недоступен"
        lines.append(f"- {alias}: {model.provider_model} — {model.description}; {tariff}; {details}")
    await message.answer("\n".join(lines))


@router.message(Command("requests"))
async def cmd_requests(message: Message, ctx: AppContext, state: FSMContext) -> None:
    if await require_admin(message, ctx):
        await state.clear()
        await show_page(message, ctx, "requests", 0)


@router.message(Command("groups"))
async def cmd_groups(message: Message, ctx: AppContext, state: FSMContext) -> None:
    if await require_admin(message, ctx):
        await state.clear()
        await show_page(message, ctx, "group", 0)


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, ctx: AppContext) -> None:
    if not await require_admin(message, ctx):
        return
    try:
        chat_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Откройте /access → Группы → карточка → Отозвать доступ.")
        return
    row = await ctx.db.access_subject("group", chat_id)
    if not row:
        await message.answer("Группа не найдена.")
        return
    await message.answer(f"Отозвать доступ группы {(row['title'] or 'Без названия')[:200]}?",
        reply_markup=keyboard([[('Подтвердить', f"acc:apply:group:{chat_id}:{row['revision']}:revoke")],
                               [('Отмена', f'acc:card:group:{chat_id}')]]))


@router.message(Command("auto_on"))
async def cmd_auto_on(message: Message, ctx: AppContext, bot: Bot) -> None:
    if not is_group_chat(message):
        await message.answer("/auto_on имеет смысл только в группе.")
        return
    if not await check_access_for_transcription(message, ctx, bot):
        return
    if not await ctx.db.set_group_auto(message.chat.id, True):
        await message.answer("Автоматический режим требует постоянного разрешения группы.")
        return
    await message.answer("Авто-распознавание новых голосовых сообщений включено.")


@router.message(Command("auto_off"))
async def cmd_auto_off(message: Message, ctx: AppContext, bot: Bot) -> None:
    if not is_group_chat(message):
        await message.answer("/auto_off имеет смысл только в группе.")
        return
    if not await check_access_for_transcription(message, ctx, bot):
        return
    await ctx.db.set_group_auto(message.chat.id, False)
    await message.answer("Авто-распознавание выключено.")


@router.message(Command("tr", "tr_raw"))
async def cmd_transcribe(message: Message, command: CommandObject, ctx: AppContext, bot: Bot) -> None:
    target = message.reply_to_message
    if not target or not target.voice:
        await message.answer("Используйте /tr в ответ на голосовое сообщение Telegram. Для аудиофайлов предусмотрена отдельная команда /trf и отдельное разрешение.")
        return
    if not await check_access_for_transcription(message, ctx, bot):
        return
    model_alias = await resolve_model_alias(message, command, ctx)
    if command and command.command == 'tr_raw':
        await transcribe_message_media(message, target, ctx, bot, model_alias, raw=True)
    else:
        await transcribe_message_media(message, target, ctx, bot, model_alias)


@router.message(Command('trf'))
async def cmd_trf(message: Message, command: CommandObject, ctx: AppContext, bot: Bot) -> None:
    user=message.from_user
    if not user or user.is_bot or message.sender_chat is not None:
        await message.answer('Для /trf требуется достоверный пользователь Telegram.')
        return
    if not await ctx.db.extended_audio_allowed(user.id):
        await message.answer('Для /trf требуется отдельное личное разрешение администратора.')
        return
    if not await check_access_for_transcription(message,ctx,bot):return
    target=message.reply_to_message
    if target is None and is_private_chat(message) and extract_media(message,extended=True):target=message
    args=(command.args or '').strip()
    if args=='?':
        rows=[];buttons=[];tokens=[]
        media=extract_media(target,extended=True) if target else None
        for alias,model in ctx.config.models.items():
            estimate=ctx.prices.estimate_audio(model.provider_model,ctx.config.max_audio_seconds+1) if ctx.prices else Estimate('unavailable')
            label=f'{alias}: до {estimate.max_rub:f} ₽' if estimate.available else f'{alias}: стоимость недоступна ({estimate.status})'
            rows.append(label)
            if media is not None and estimate.available:
                token=await ctx.db.create_trf_choice(user.id,message.chat.id,target.message_id,alias,media)
                tokens.append(token);buttons.append([(label,'trfm:'+token)])
        sent=await message.answer('Модели /trf. Предварительный резерв рассчитан на предельную длительность:\n'+'\n'.join(rows)+
                                  ('\nОтветьте командой /trf ? на аудио для выбора.' if target is None else ''),
                                  reply_markup=keyboard(buttons) if buttons else None)
        if tokens:await ctx.db.bind_trf_choice(tokens,sent.message_id)
        return
    if target is None or extract_media(target,extended=True) is None:
        await message.answer('Используйте /trf [модель] в ответ на voice, audio или документ с аудио. В личке можно добавить /trf в подпись файла.')
        return
    if args and args not in ctx.config.models:
        await message.answer('Неизвестная модель. Доступные варианты: /trf ? в ответ на файл.')
        return
    alias=args or await resolve_model_alias(message,None,ctx)
    await transcribe_message_media(message,target,ctx,bot,alias,extended=True)


@router.callback_query(F.data.startswith('trfm:'))
async def trf_model_choice(callback: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    if not isinstance(callback.message,Message) or callback.from_user.is_bot:
        await callback.answer('Кнопка недоступна.');return
    row=await ctx.db.consume_trf_choice(callback.data[5:],callback.from_user.id,
                                       callback.message.chat.id,callback.message.message_id)
    if row is None:
        await callback.answer('Это чужая, использованная или просроченная кнопка.');return
    alias,target_id,media=row
    if alias not in ctx.config.models:
        await callback.answer('Модель недоступна. Откройте новое меню.');return
    caller=callback.message.model_copy(update={'from_user':callback.from_user,'sender_chat':None})
    if not await check_access_for_transcription(caller,ctx,bot):
        await callback.answer('Доступ недоступен.');return
    await callback.answer()
    await transcribe_message_media(caller,SimpleNamespace(message_id=target_id),ctx,bot,alias,extended=True,media_ref=media)


@router.message(F.voice)
async def media_auto_or_private(message: Message, ctx: AppContext, bot: Bot) -> None:
    if not message.voice:
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
        await transcribe_message_media(message, message, ctx, bot, model_alias, require_auto=True)


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
        reason="Бот добавлен в группу",
        cooldown=ctx.config.admin_notify_cooldown_seconds,
    )
    await notify_admins(bot, ctx, req)
    if req:
        with contextlib.suppress(Exception):
            await bot.send_message(chat.id, "Бот добавлен. Группа ожидает разрешения администратора бота.")


async def legacy_decision(callback: CallbackQuery, ctx: AppContext, bot: Bot, decision: str):
    if not await admin_callback(callback, ctx):
        return
    request_id = (callback.data or "").split(":", 1)[1]
    req = await ctx.db.decide_request(request_id, decision, callback.from_user.id, ctx.config.one_time_approval_jobs)
    if not req:
        await callback.answer("Заявка уже обработана или устарела.", show_alert=True)
        return
    await callback.answer("Решение сохранено.")
    await notify_access_result(bot, req.chat_id, "Доступ группы отклонён." if decision == "reject" else
                               "Доступ группы разрешён однократно." if decision == "approve_once" else
                               "Доступ группы разрешён постоянно. Автоматический режим включается отдельно.")
    await remove_decision_buttons(callback.message)
    await callback.message.answer("Решение сохранено. Управление доступом: /access.")


@router.callback_query(F.data.startswith("ap1:"))
async def cb_approve_once(callback: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    await legacy_decision(callback, ctx, bot, "approve_once")


@router.callback_query(F.data.startswith("apf:"))
async def cb_approve_forever(callback: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    await legacy_decision(callback, ctx, bot, "approve_forever")


@router.callback_query(F.data.startswith("rej:"))
async def cb_reject(callback: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    await legacy_decision(callback, ctx, bot, "reject")
