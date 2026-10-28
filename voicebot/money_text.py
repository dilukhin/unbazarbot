from datetime import datetime
from decimal import Decimal, localcontext

from .money import MoneyError, MOSCOW, aware


def amount(atoms):
    if type(atoms) is not int or atoms<0:raise MoneyError('Invalid display amount')
    if 0<atoms<1000:return 'меньше 0,000001 Б'
    with localcontext() as ctx:
        ctx.prec=500
        value=format(Decimal(atoms).scaleb(-9),'f')
    return (value.rstrip('0').rstrip('.') if '.' in value else value).replace('.',',')+' Б'


def date(value):
    value=value if isinstance(value,datetime) else datetime.fromisoformat(value)
    return aware(value).astimezone(MOSCOW).strftime('%d.%m.%Y %H:%M МСК')


def balance_text(balance):
    return (f'Доступно: {amount(balance["free_atoms"]+balance["extra_atoms"])}\n'
            f'Бесплатные: {amount(balance["free_atoms"])}\nДополнительные: {amount(balance["extra_atoms"])}\n'
            f'В резерве: {amount(balance["held_atoms"])}\nСледующее месячное начисление: {amount(balance["monthly_atoms"])}\n'
            f'Период: {date(balance["period_start"])} — {date(balance["period_end"])}\n'
            f'Дата начисления: {date(balance["period_end"])}')


def tariff_status(value):
    return {'available':'проверен','unpriced':'цена недоступна','unavailable':'каталог недоступен',
            'stale':'тариф устарел','unsafe':'нет безопасной оценки','incompatible':'тариф несовместим'}.get(value,'стоимость неизвестна')
