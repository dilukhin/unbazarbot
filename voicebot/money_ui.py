"""Private balances and confirmed administrator budget operations."""
import json
import contextlib
from decimal import Decimal, localcontext

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

from .access_ui import keyboard, admin_callback, admin_message
from .money import MoneyError, decimal_amount, to_atoms
from .textfmt import chunks
from .money_text import amount, date, balance_text, tariff_status

router=Router(name='money')

LABELS={'monthly':'Месячная квота со следующего периода','current':'Начислить до конца текущего периода',
        'extra':'Начислить без срока сгорания','debit-current':'Корректировка бесплатного остатка вниз',
        'debit-extra':'Корректировка дополнительного остатка вниз','trf-on':'Разрешить /trf','trf-off':'Запретить /trf',
        'request-close':'Закрыть запрос без начисления'}

HELP={
 'money':('Базарики','1 базарик = 0,01 ₽. Базарики дробные. Обычно личная бесплатная квота — 1000 Б за свой месяц; точное значение и остаток показывает /balance. Бесплатные средства расходуются прежде дополнительных. Дополнительные начисления не сгорают. Это внутренняя единица, вывода и переводов между людьми нет.'),
 'dates':('Даты начисления','Личный месяц начинается с первого явного обращения. Групповой — с первого постоянного разрешения группы. Начисление происходит в 00:00 по Москве в своё число месяца. Для 29–31 в коротком месяце берётся последний день, затем возвращается исходное число: 31 января → последний день февраля → 31 марта. Бесплатный остаток прошлого месяца не переносится.'),
 'budgets':('Личный и групповой бюджет','Авто-voice в группе расходует только групповой бюджет. При его исчерпании бот молчит, настройка авто остаётся включённой; новый период возобновляет средства. Ручные /tr и /trf в разрешённой группе расходуют личные базарики вызывающего, даже когда группа исчерпала бюджет. Деньги не заменяют разрешение на доступ.'),
 'prices':('Стоимость и модели','/model и /trf ? показывают модели, свежесть тарифа и предварительный резерв. Окончательный расход сообщает RouterAI, он может быть дробным: 0,00738 ₽ = 0,738 Б. Неизвестный или устаревший тариф останавливает новую оплату. Каталог обычно проверяется клиентом раз в 6 часов; обычный допустимый возраст — 48 часов, он настраивается. Каталожная оценка и фактическое списание — разные суммы.'),
 'files':('Файлы и /trf','/tr — только ответ на голосовое. /trf — отдельное разрешение для voice, audio и документа с аудио; в личке команда может быть подписью файла. /trf ? в ответ на файл открывает выбор модели. Без команды обычные файлы не обрабатываются. Проверяются содержимое, размер и фактическая длительность. Видео, PDF и неизвестный формат не допускаются; пределы зависят от настроек и Telegram.'),
 'cache':('Кеш','Готовая расшифровка той же реальной моделью возвращается бесплатно при действующем доступе. Кеш работает и при недоступном новом тарифе. Смена названия модели не разрешает вторую оплату. При отзыве прав на текущий контекст кеш тоже недоступен. Исходный вариант доступен через /tr_raw.'),
 'errors':('Ошибки и сверка','После возможной отправки с неизвестным исходом резерв сохраняется: отсутствие цены не означает нулевой расход. Аудио автоматически повторно не отправляется. История /balance показывает ожидающие сверки запросы. Если расход превысил резерв, модель приостанавливается для проверки администратора; личный баланс не становится отрицательным.'),
 'topup':('Пополнение','В личном /balance есть запрос пополнения администратору. Он может отдельно изменить месячную квоту, начислить средства до конца текущего периода или добавить несгораемый остаток. Автоматического приёма платежей нет. Запрос не списывает реальные деньги и не выдаёт доступ.'),
 'privacy':('Права и конфиденциальность','Личный баланс и история показываются только в личке владельцу и администратору бота. В группе виден её общий бюджет. /trf требует отдельного личного разрешения. Аудио обрабатывает внешний RouterAI, исходный файл хранится временно; в истории финансов нет текста расшифровки.'),
}


class MoneyInput(StatesGroup):
    amount=State()


async def send_balance(message,ctx,owner):
    balance=await ctx.db.money_balance('user',owner)
    buttons=[[('История',f'bal:{owner}:history:0'),('Модели',f'bal:{owner}:models')],
             [('Запросить пополнение',f'bal:{owner}:request')]]
    if await ctx.db.is_admin(owner):buttons.append([('Управление бюджетами','mu:home')])
    await message.answer(balance_text(balance),reply_markup=keyboard(buttons))


@router.message(Command('balance'))
async def balance_command(message: Message,ctx):
    if message.chat.type!=ChatType.PRIVATE or not message.from_user or message.chat.id!=message.from_user.id:
        await message.answer('Личный баланс доступен в личном чате с ботом: /balance.');return
    # Also works in isolated component tests without dispatcher middleware.
    try:
        await ctx.db.touch_money_user(message.from_user.id)
        await send_balance(message,ctx,message.from_user.id)
    except MoneyError:
        await message.answer('Баланс временно недоступен. Обратитесь к администратору.')


async def history(message,ctx,kind,subject,page=0,owner=None):
    rows,more=await ctx.db.money_history(kind,subject,page)
    names={'monthly':'Месячное начисление','expire':'Истечение бесплатного остатка','reserve':'Резерв',
           'settled':'Расход подтверждён','released':'Резерв снят','monthly-policy':'Изменение будущей квоты',
           'extra-grant':'Дополнительное начисление','current-grant':'Начисление на текущий период',
           'current-correction':'Корректировка бесплатных','extra-correction':'Корректировка дополнительных'}
    lines=['История операций:']
    for row in rows:
        data=json.loads(row['payload_json'])
        value=data.get('atoms',data.get('charged_atoms',0))
        if row['event']=='released':value=data.get('released_free_atoms',0)+data.get('released_extra_atoms',0)
        lines.append(f'{date(row["created_at"])} · {names.get(row["event"],"Операция")}: {amount(value)}')
    if not rows:lines.append('Операций пока нет.')
    if page==0:
        calls=await ctx.db.financial_history(kind,subject)
        if calls:lines.append('\nПоследние запросы:')
        for call in calls:
            if call['state']=='not-sent':
                lines.append(f'{date(call["created_at"])} · {call["provider_model"]}: не отправлен, резерв освобождён.')
                continue
            state={'settled':'подтверждён','reserved':'подготовка','sending':'обрабатывается',
                   'reported':'сверка','overrun':'проверка перерасхода'}.get(call['state'],'ожидает сверки')
            value=call['final_rub'] if call['state']=='settled' else call['bound_rub']
            lines.append(f'{date(call["created_at"])} · {call["provider_model"]} · №{call["job_id"][:12]}: {state}, '+('расход ' if call['state']=='settled' else 'резерв ')+str(value)+' ₽')
    prefix=f'bal:{owner}:history' if owner is not None else f'mu:history:{kind}:{subject}'
    buttons=[]
    nav=[]
    if page:nav.append(('Назад',prefix+':'+str(page-1)))
    if more:nav.append(('Далее',prefix+':'+str(page+1)))
    if nav:buttons.append(nav)
    buttons.append([('Баланс',f'bal:{owner}:show' if owner is not None else f'mu:view:{kind}:{subject}')])
    parts=list(chunks('\n'.join(lines)))
    for index,part in enumerate(parts):await message.answer(part,reply_markup=keyboard(buttons) if index==len(parts)-1 else None)


@router.callback_query(F.data.startswith('bal:'))
async def balance_callback(callback: CallbackQuery,ctx,bot):
    parts=(callback.data or '').split(':')
    if not isinstance(callback.message,Message) or callback.message.chat.type!=ChatType.PRIVATE or callback.message.chat.id!=callback.from_user.id or len(parts)<3 or parts[1]!=str(callback.from_user.id):
        await callback.answer('Личный баланс доступен только владельцу в личке.',show_alert=True);return
    owner=callback.from_user.id
    await callback.answer()
    try:
        if parts[2]=='show':await send_balance(callback.message,ctx,owner)
        elif parts[2]=='history':await history(callback.message,ctx,'user',owner,int(parts[3]),owner=owner)
        elif parts[2]=='models':
            lines=[]
            for alias,model in ctx.config.models.items():
                estimate=ctx.prices.estimate_audio(model.provider_model,ctx.config.max_audio_seconds+1) if ctx.prices else None
                label=alias+': '+(f'резерв до {estimate.max_rub:f} ₽' if estimate and estimate.available else tariff_status(estimate.status) if estimate else 'каталог недоступен')
                if estimate and estimate.checked_at:label+='; проверено '+date(estimate.checked_at)
                lines.append(label)
            await callback.message.answer('\n'.join(lines))
        elif parts[2]=='request':
            created=await ctx.db.request_money_topup(owner)
            await callback.message.answer('Запрос пополнения принят.' if created else 'Запрос уже ожидает решения или отправлен недавно.')
            if created:
                for chat in await ctx.db.admin_private_chats():
                    with contextlib.suppress(Exception):
                        await bot.send_message(chat,f'Запрос пополнения от пользователя {owner}.',reply_markup=keyboard([[('Бюджет',f'mu:view:user:{owner}')]]))
    except (MoneyError,ValueError,IndexError):await callback.message.answer('Кнопка устарела. Откройте /balance заново.')


@router.message(Command('helphelp'))
async def helphelp(message: Message):
    await message.answer('Подробная справка: выберите раздел.',reply_markup=keyboard([[(label,'hh:'+key)] for key,(label,_) in HELP.items()]))


@router.callback_query(F.data.startswith('hh:'))
async def help_callback(callback: CallbackQuery):
    key=(callback.data or '')[3:]
    if key=='home' and isinstance(callback.message,Message):
        await callback.answer();await helphelp(callback.message);return
    item=HELP.get(key)
    if item is None or not isinstance(callback.message,Message):
        await callback.answer('Раздел недоступен.');return
    await callback.answer()
    await callback.message.answer(item[0]+'\n\n'+item[1],reply_markup=keyboard([[('Все разделы','hh:home')]]))


async def admin_card(message,ctx,kind,subject):
    ctx.db._money_subject(kind,subject)
    rows=[]
    try:
        balance=await ctx.db.money_balance(kind,subject)
        text=('Пользователь' if kind=='user' else 'Группа')+f' {subject}\n'+balance_text(balance)
        rows=[[('История',f'mu:history:{kind}:{subject}:0')]]
        for action in ('monthly','current','extra','debit-current','debit-extra'):
            rows.append([(LABELS[action],f'mu:edit:{kind}:{subject}:{action}')])
    except MoneyError as exc:
        text=(f'Счёт {subject} ещё не открыт: требуется явное обращение человека или постоянное разрешение группы.'
              if str(exc)=='Money anchor not established' else 'Бюджет временно недоступен. Требуется проверка администратора.')
    if kind=='user':
        request=await ctx.db.money_request(subject)
        if request:
            text+='\nОжидает запрос пополнения от '+date(request['created_at'])
            rows.append([(LABELS['request-close'],f'mu:edit:user:{subject}:request-close')])
        allowed=await ctx.db.extended_audio_allowed(subject)
        text+='\nПраво /trf: '+('разрешено' if allowed else 'запрещено')
        action='trf-off' if allowed else 'trf-on'
        rows.append([(LABELS[action],f'mu:edit:user:{subject}:{action}')])
    rows.append([('Бюджеты','mu:home')])
    await message.answer(text,reply_markup=keyboard(rows))


async def confirmation(message,ctx,owner,kind,subject,action,amount_text=None,edit=None):
    edit=edit or {}
    token=await ctx.db.create_money_intent(owner,message.chat.id,kind,subject,action,amount_text,
               revision=edit.get('revision'),period_start=edit.get('period'),permission_revision=edit.get('permission_revision'),request_stamp=edit.get('request_stamp'))
    text=f'Подтвердите: {LABELS[action]}.\nАдресат: {"пользователь" if kind=="user" else "группа"} {subject}.'
    if amount_text is not None:
        with localcontext() as decimal_context:
            decimal_context.prec=500
            text+='\nСумма: '+format(Decimal(to_atoms(amount_text)).scaleb(-9),'f').replace('.',',')+' Б'
    sent=await message.answer(text,reply_markup=keyboard([[('Подтвердить','mu:confirm:'+token),('Отмена','mu:cancel:'+token)]]))
    await ctx.db.bind_money_intent(token,sent.message_id)


@router.callback_query(F.data.startswith('mu:'))
async def money_admin_callback(callback: CallbackQuery,ctx,state: FSMContext,bot):
    if not await admin_callback(callback,ctx):return
    if callback.message.chat.id!=callback.from_user.id:
        await callback.answer('Откройте личный чат с ботом.',show_alert=True);return
    parts=(callback.data or '').split(':')
    await callback.answer()
    try:
        if parts[1]=='home':
            await state.clear()
            await callback.message.answer('Бюджеты: выберите раздел.',reply_markup=keyboard([[('Люди','mu:list:user:0'),('Группы','mu:list:group:0')],[('Запросы пополнения','mu:requests:0')]]))
        elif parts[1]=='requests':
            page=int(parts[2]);rows,more=await ctx.db.pending_money_requests(page)
            buttons=[[(f'{row["user_id"]} · {date(row["created_at"])}',f'mu:view:user:{row["user_id"]}')] for row in rows]
            nav=[]
            if page:nav.append(('Назад',f'mu:requests:{page-1}'))
            if more:nav.append(('Далее',f'mu:requests:{page+1}'))
            if nav:buttons.append(nav)
            buttons.append([('Бюджеты','mu:home')])
            await callback.message.answer('Запросы пополнения:' if rows else 'Запросов пополнения нет.',reply_markup=keyboard(buttons))
        elif parts[1]=='list':
            kind,page=parts[2],int(parts[3]);rows,more=await ctx.db.money_subjects(kind,page)
            buttons=[[((str(row['subject_id'])+' · '+str(row['title'] or 'Без названия').replace('\n',' '))[:60],f'mu:view:{kind}:{row["subject_id"]}')] for row in rows]
            nav=[]
            if page:nav.append(('Назад',f'mu:list:{kind}:{page-1}'))
            if more:nav.append(('Далее',f'mu:list:{kind}:{page+1}'))
            if nav:buttons.append(nav)
            buttons.append([('Разделы','mu:home')])
            await callback.message.answer('Люди' if kind=='user' else 'Группы',reply_markup=keyboard(buttons))
        elif parts[1]=='view':
            await state.clear();await admin_card(callback.message,ctx,parts[2],int(parts[3]))
        elif parts[1]=='history':await history(callback.message,ctx,parts[2],int(parts[3]),int(parts[4]))
        elif parts[1]=='edit':
            kind,subject,action=parts[2],int(parts[3]),parts[4]
            if action not in LABELS:raise MoneyError('Unknown action')
            ctx.db._money_subject(kind,subject)
            if action.startswith('trf-'):
                if kind!='user':raise MoneyError('Unknown permission')
                profile=await ctx.db.access_subject('user',subject)
                await confirmation(callback.message,ctx,callback.from_user.id,kind,subject,action,
                                   edit={'permission_revision':profile['revision'] if profile else -1})
            elif action=='request-close':
                request=await ctx.db.money_request(subject)
                if not request or kind!='user':raise MoneyError('Unknown request')
                await confirmation(callback.message,ctx,callback.from_user.id,kind,subject,action,edit={'request_stamp':request['created_at']})
            else:
                balance=await ctx.db.money_balance(kind,subject)
                await state.set_state(MoneyInput.amount)
                await state.update_data(money_edit={'kind':kind,'subject':subject,'action':action,'actor':callback.from_user.id,
                                                   'revision':balance['quota_revision'],'period':balance['period_start']})
                await callback.message.answer(LABELS[action]+'. Введите сумму базариков числом. Отмена: /cancel.')
        elif parts[1]=='confirm':
            result=await ctx.db.apply_money_intent(parts[2],callback.from_user.id,callback.message.chat.id,callback.message.message_id)
            if result['status']=='applied':
                await callback.message.answer('Изменение применено и записано в историю.')
                await admin_card(callback.message,ctx,result['kind'],result['subject_id'])
                if result['kind']=='user':
                    with contextlib.suppress(Exception):
                        note=('Администратор закрыл запрос пополнения без начисления.' if result['action']=='request-close' else
                              'Администратор обновил разрешение /trf.' if result['action'].startswith('trf-') else 'Администратор обновил ваш бюджет.')
                        await bot.send_message(result['subject_id'],note+' Баланс и история: /balance; подробности: /helphelp.')
            else:await callback.message.answer('Подтверждение устарело, уже обработано или не соответствует текущему состоянию. Откройте карточку заново.')
        elif parts[1]=='cancel':
            target=await ctx.db.cancel_money_intent(parts[2],callback.from_user.id,callback.message.chat.id,callback.message.message_id)
            await state.clear()
            await callback.message.answer('Подтверждение отменено.' if target else 'Подтверждение недоступно.')
            if target:await admin_card(callback.message,ctx,*target)
    except (MoneyError,ValueError,IndexError,KeyError):
        await callback.message.answer('Операция недоступна. Обновите карточку бюджета.')


@router.message(MoneyInput.amount,F.chat.type==ChatType.PRIVATE,F.text,~F.text.startswith('/'))
async def money_amount(message: Message,ctx,state: FSMContext):
    if not await admin_message(message,ctx):return
    if message.chat.id!=message.from_user.id:return
    data=(await state.get_data()).get('money_edit')
    if not data or data['actor']!=message.from_user.id:
        await state.clear();await message.answer('Откройте карточку бюджета заново.');return
    try:
        value=format(decimal_amount(message.text.strip().replace(',','.')),'f')
        to_atoms(value)
        await confirmation(message,ctx,message.from_user.id,data['kind'],data['subject'],data['action'],value,edit=data)
        await state.clear()
    except (MoneyError,ValueError):
        await message.answer('Введите допустимую неотрицательную сумму. Ноль разрешён для будущей месячной квоты. Отмена: /cancel.')


@router.message(Command('cancel'))
async def cancel_money_input(message: Message,ctx,state: FSMContext):
    if message.chat.type==ChatType.PRIVATE and message.from_user and message.chat.id==message.from_user.id:
        await state.clear()
        await ctx.db.cancel_private_choices(message.from_user.id,message.chat.id)
        await message.answer('Ввод и ожидающие подтверждения отменены.')
