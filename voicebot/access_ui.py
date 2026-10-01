from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


router = Router(name="access")
PAGE_SIZE = 8
STATUS = {"pending": "Ожидает разрешения", "approved": "Доступ разрешён",
          "approved_once": "Разрешено однократно", "revoked": "Доступ отозван",
          "rejected": "Заявка отклонена", "superseded": "Заявка устарела"}
LABELS = {"requests": "Заявки", "user": "Люди", "group": "Группы"}


class AccessInput(StatesGroup):
    note = State()
    search = State()


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
    ])


def home_keyboard():
    return keyboard([[(label, f"acc:list:{kind}:0")] for kind, label in LABELS.items()])


def request_keyboard():
    return keyboard([[('Запросить доступ', 'acc:request')]])


def entry_keyboard():
    return keyboard([[('Доступ', 'acc:home')]])


def decision_keyboard(req):
    choices = [('Разрешить', f'acc:decide:{req.id}:approve_forever')]
    if req.subject_type == 'group':
        choices = [('Разрешить 1 раз', f'acc:decide:{req.id}:approve_once'),
                   ('Разрешить постоянно', f'acc:decide:{req.id}:approve_forever')]
    return keyboard([choices, [('Отклонить', f'acc:decide:{req.id}:reject')],
                     [('Все заявки', 'acc:list:requests:0')]])


def request_text(req):
    label = 'Человек' if req.subject_type == 'user' else 'Группа'
    username = f'@{req.requester_username}' if req.requester_username else 'не задано'
    return (f'Заявка на доступ\n\n{label}: {(req.chat_title or "Без названия")[:200]}\n'
            f'Имя пользователя заявителя: {username}\nИдентификатор: {req.chat_id}\n'
            f'Кто запросил: {req.requester_user_id}\nПояснение: {req.reason[:500]}\nДата: {req.created_at}')


async def notify_request(bot, ctx, req):
    if not req or not req.is_new or not ctx.config.notify_admins_on_new_request:
        return
    for chat_id in await ctx.db.admin_private_chats():
        with contextlib.suppress(Exception):
            await bot.send_message(chat_id, request_text(req), reply_markup=decision_keyboard(req))


async def admin_callback(callback, ctx):
    if (not isinstance(callback.message, Message) or callback.message.chat.type != ChatType.PRIVATE
            or not await ctx.db.is_admin(callback.from_user.id)):
        await callback.answer('Только для администратора в личном чате с ботом.', show_alert=True)
        return False
    return True


async def admin_message(message, ctx):
    if (message.chat.type != ChatType.PRIVATE or not message.from_user
            or not await ctx.db.is_admin(message.from_user.id)):
        await message.answer('Управление доступом доступно администратору в личном чате с ботом.')
        return False
    return True


async def show_page(message, ctx, kind, page, search=''):
    rows, total, page = await ctx.db.access_page(kind, page, search, PAGE_SIZE)
    buttons = []
    for row in rows:
        if kind == 'requests':
            prefix = 'Человек' if row['subject_type'] == 'user' else 'Группа'
            name = row['chat_title'] or 'Без названия'
            data = f'acc:req:{row["id"]}'
            label = f'{prefix}: {name}'
        else:
            name = row['title'] or (f'@{row["username"]}' if row['username'] else 'Без названия')
            data = f'acc:card:{kind}:{row["user_id"] if kind == "user" else row["chat_id"]}'
            label = f'{name} — {STATUS.get(row["status"], row["status"])}'
        buttons.append([(label[:64], data)])
    nav = []
    if page:
        nav.append(('Назад', f'acc:list:{kind}:{page - 1}'))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(('Далее', f'acc:list:{kind}:{page + 1}'))
    if nav:
        buttons.append(nav)
    buttons.extend([[('Поиск', f'acc:search:{kind}'), ('Показать все', f'acc:all:{kind}')],
                    [('Меню доступа', 'acc:home')]])
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    text = f'{LABELS[kind]} · страница {page + 1} из {pages}\nВсего записей: {total}'
    if search:
        text += f'\nПоиск: {search}'
    if not rows:
        text += '\nЗаписей нет.'
    await message.answer(text, reply_markup=keyboard(buttons))


async def show_card(message, ctx, kind, target_id):
    row = await ctx.db.access_subject(kind, target_id)
    if not row:
        await message.answer('Запись не найдена.')
        return
    is_admin = kind == 'user' and await ctx.db.is_admin(target_id)
    text = (f'{"Человек" if kind == "user" else "Группа"}: {(row["title"] or "Без названия")[:200]}\n'
            f'Имя пользователя: {"@" + row["username"] if row["username"] else "не задано"}\n'
            f'Идентификатор: {target_id}\nСтатус: {STATUS.get(row["status"], row["status"])}\n'
            f'Кто разрешил: {row["approved_by_user_id"] or "—"}\nКогда: {row["approved_at"] or "—"}')
    if row['revoked_at']:
        text += f'\nКто отозвал или отклонил: {row["revoked_by_user_id"]}\nКогда: {row["revoked_at"]}'
    if kind == 'group':
        text += f'\nАвтоматическое распознавание: {"включено" if row["auto_enabled"] else "выключено"}'
        if row['status'] == 'approved_once':
            text += f'\nОсталось распознаваний: {row["remaining_jobs"]}'
    buttons = []
    if is_admin:
        text += '\nАдминистратор: его полномочия задаются отдельно в конфигурации.'
    else:
        allowed = row['status'] in {'approved', 'approved_once'}
        action, label = ('revoke', 'Отозвать доступ') if allowed else (
            'approve_forever', 'Восстановить доступ' if row['status'] == 'revoked' else 'Разрешить доступ')
        buttons.append([(label, f'acc:confirm:{kind}:{target_id}:{row["revision"]}:{action}')])
        if kind == 'group' and row['status'] == 'approved_once':
            buttons.append([('Разрешить постоянно',
                             f'acc:confirm:{kind}:{target_id}:{row["revision"]}:approve_forever')])
    buttons.append([(f'Список: {LABELS[kind]}', f'acc:list:{kind}:0'), ('Меню доступа', 'acc:home')])
    await message.answer(text, reply_markup=keyboard(buttons))


@router.message(Command('access'))
async def access_command(message: Message, ctx, state: FSMContext):
    if await admin_message(message, ctx):
        await state.clear()
        await message.answer('Доступ: выберите раздел.', reply_markup=home_keyboard())


@router.message(Command('cancel'))
async def cancel_command(message: Message, state: FSMContext):
    if message.chat.type == ChatType.PRIVATE:
        await state.clear()
        await message.answer('Ввод отменён. Для начала используйте /start.')


async def submit_user_request(message, user, ctx, bot, note):
    await ctx.db.ensure_private_user(user.id, user.full_name, user.username)
    if await ctx.db.is_admin(user.id) or await ctx.db.private_user_allowed(user.id):
        await message.answer('Доступ уже есть. Можно отправлять голосовые сообщения.')
        return
    req = await ctx.db.create_access_request('user', user.id, user.full_name, user.id, user.username,
                                              note, ctx.config.admin_notify_cooldown_seconds)
    await notify_request(bot, ctx, req)
    await message.answer('Заявка отправлена. После решения администратора придёт уведомление.' if req and req.is_new
                         else 'Заявка уже ожидает решения.' if req else
                         'Новый запрос пока недоступен. Попробуйте немного позже.')


@router.callback_query(F.data.startswith('acc:'))
async def access_callback(callback: CallbackQuery, ctx, bot, state: FSMContext):
    parts = (callback.data or '').split(':')
    action = parts[1] if len(parts) > 1 else ''
    if action in {'request', 'skip'}:
        if not isinstance(callback.message, Message) or callback.message.chat.type != ChatType.PRIVATE:
            await callback.answer('Запросите личный доступ в личном чате с ботом.', show_alert=True)
            return
        await callback.answer()
        if action == 'request':
            if await ctx.db.private_user_allowed(callback.from_user.id) or await ctx.db.is_admin(callback.from_user.id):
                await callback.message.answer('Доступ уже есть.')
                return
            if await ctx.db.pending_access_request('user', callback.from_user.id):
                await callback.message.answer('Ваша заявка уже ожидает решения администратора.')
                return
            await state.set_state(AccessInput.note)
            await callback.message.answer('Напишите коротко, кто вы, чтобы администратор узнал вас. '
                                          'Можно пропустить; для отмены — /cancel.',
                                          reply_markup=keyboard([[('Без пояснения', 'acc:skip')]]))
        elif await state.get_state() == AccessInput.note.state:
            await state.clear()
            await submit_user_request(callback.message, callback.from_user, ctx, bot, 'Без пояснения')
        else:
            await callback.message.answer('Начните запрос заново через /start.')
        return
    if not await admin_callback(callback, ctx):
        return
    await callback.answer()
    try:
        if action == 'home' and len(parts) == 2:
            await state.clear()
            await callback.message.answer('Доступ: выберите раздел.', reply_markup=home_keyboard())
        elif action in {'list', 'all'}:
            kind = parts[2]
            if kind not in LABELS:
                raise ValueError()
            data = await state.get_data()
            search = data.get('query', '') if data.get('kind') == kind and action == 'list' else ''
            await state.set_state(None)
            await state.set_data({'kind': kind, 'query': search})
            await show_page(callback.message, ctx, kind, int(parts[3]) if action == 'list' else 0, search)
        elif action == 'search':
            if parts[2] not in LABELS:
                raise ValueError()
            await state.set_state(AccessInput.search)
            await state.set_data({'kind': parts[2]})
            await callback.message.answer('Введите имя, название группы или имя пользователя. Для отмены — /cancel.')
        elif action == 'card':
            await show_card(callback.message, ctx, parts[2], int(parts[3]))
        elif action == 'req':
            req = await ctx.db.get_request(parts[2])
            if not req or req.status != 'pending':
                await callback.message.answer('Заявка уже обработана или устарела.')
            else:
                await callback.message.answer(request_text(req), reply_markup=decision_keyboard(req))
        elif action == 'decide':
            req = await ctx.db.decide_access_request(parts[2], parts[3], callback.from_user.id,
                                                     ctx.config.one_time_approval_jobs)
            if not req:
                await callback.message.answer('Заявка уже обработана или устарела.')
                return
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer('Решение сохранено.')
            text = ('Доступ отклонён.' if parts[3] == 'reject' else
                    'Доступ разрешён на одно распознавание.' if parts[3] == 'approve_once' else
                    'Личный доступ разрешён. Можно отправлять голосовые сообщения.' if req.subject_type == 'user' else
                    'Доступ группы разрешён. Используйте /tr в ответ на аудио. Автоматический режим включается отдельно.')
            with contextlib.suppress(Exception):
                await bot.send_message(req.chat_id, text)
        elif action in {'confirm', 'apply'} and len(parts) == 6:
            kind, target_id, revision, decision = parts[2], int(parts[3]), int(parts[4]), parts[5]
            if kind not in {'user', 'group'} or decision not in {'revoke', 'approve_forever'}:
                raise ValueError()
            row = await ctx.db.access_subject(kind, target_id)
            if not row or row['revision'] != revision:
                await callback.message.answer('Состояние изменилось. Откройте карточку заново.')
                return
            if action == 'confirm':
                verb = 'Отозвать' if decision == 'revoke' else 'Разрешить'
                await callback.message.answer(f'{verb} доступ: {(row["title"] or "Без названия")[:200]}?',
                    reply_markup=keyboard([[('Подтвердить', f'acc:apply:{kind}:{target_id}:{revision}:{decision}')],
                                           [('Отмена', f'acc:card:{kind}:{target_id}')]]))
            else:
                changed = await ctx.db.change_access(kind, target_id, decision, callback.from_user.id, revision)
                if not changed:
                    await callback.message.answer('Состояние изменилось или действие недоступно. Откройте карточку заново.')
                    return
                await callback.message.edit_reply_markup(reply_markup=None)
                await show_card(callback.message, ctx, kind, target_id)
                with contextlib.suppress(Exception):
                    await bot.send_message(target_id, 'Доступ отозван.' if decision == 'revoke' else 'Доступ разрешён.')
        else:
            raise ValueError()
    except (ValueError, IndexError):
        await callback.message.answer('Кнопка недействительна. Откройте /access заново.')


@router.message(AccessInput.note, F.chat.type == ChatType.PRIVATE, F.text, ~F.text.startswith('/'))
async def user_note(message: Message, ctx, bot, state: FSMContext):
    if len(message.text) > 500:
        await message.answer('Пояснение должно быть не длиннее 500 символов. Напишите короче или /cancel.')
        return
    await state.clear()
    await submit_user_request(message, message.from_user, ctx, bot, message.text)


@router.message(AccessInput.search, F.text, ~F.text.startswith('/'))
async def admin_search(message: Message, ctx, state: FSMContext):
    if not await admin_message(message, ctx):
        await state.clear()
        return
    if len(message.text) > 100:
        await message.answer('Введите не более 100 символов.')
        return
    data = await state.get_data()
    kind = data.get('kind', 'user')
    query = message.text.strip()
    await state.set_state(None)
    await state.set_data({'kind': kind, 'query': query})
    await show_page(message, ctx, kind, 0, query)
