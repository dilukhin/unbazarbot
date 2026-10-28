"""Exact money boundaries. One atom is 10^-9 bazarika (10^-11 RUB)."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, localcontext
from zoneinfo import ZoneInfo

ATOMS_PER_BAZARIK = 10**9
ATOMS_PER_RUB = 10**11
MAX_ATOMS = 10**18
MOSCOW = ZoneInfo('Europe/Moscow')


class MoneyError(ValueError):
    pass


def decimal_amount(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise MoneyError('Exact decimal required')
    if isinstance(value, str) and len(value) > 480:
        raise MoneyError('Amount text too long')
    try:
        result = Decimal(value)
    except ArithmeticError:
        raise MoneyError('Invalid amount') from None
    if not result.is_finite() or result < 0 or len(result.as_tuple().digits) > 400 or not -400 <= result.adjusted() <= 18:
        raise MoneyError('Amount outside supported precision')
    return result


def to_atoms(value, *, rub=False) -> int:
    value = decimal_amount(value)
    with localcontext() as ctx:
        ctx.prec = 500
        atoms = int((value * (ATOMS_PER_RUB if rub else ATOMS_PER_BAZARIK)).to_integral_value(rounding=ROUND_CEILING))
    if atoms > MAX_ATOMS:
        raise MoneyError('Amount outside ledger capacity')
    return atoms


def bazariki(atoms: int) -> Decimal:
    if type(atoms) is not int or not 0 <= atoms <= MAX_ATOMS:
        raise MoneyError('Invalid atomic amount')
    with localcontext() as ctx:
        ctx.prec = 200
        return Decimal(atoms).scaleb(-9)


def aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MoneyError('Timezone required')
    return value.astimezone(timezone.utc)


def period(anchor: datetime, at: datetime) -> tuple[str, str]:
    anchor, at = aware(anchor), aware(at)
    if at < anchor:
        raise MoneyError('Time precedes account anchor')
    local, day = at.astimezone(MOSCOW), anchor.astimezone(MOSCOW).day

    def boundary(year, month):
        return datetime(year, month, min(day, calendar.monthrange(year, month)[1]), tzinfo=MOSCOW)

    year, month = local.year, local.month
    start = boundary(year, month)
    if local < start:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        start = boundary(year, month)
    end = boundary(year + 1, 1) if month == 12 else boundary(year, month + 1)
    return tuple(x.astimezone(timezone.utc).isoformat(timespec='seconds') for x in (start, end))


@dataclass(frozen=True)
class MoneySettings:
    user_monthly_bazariki: str = '1000'
    group_monthly_bazariki: str = '10000'

    def __post_init__(self):
        to_atoms(self.user_monthly_bazariki)
        to_atoms(self.group_monthly_bazariki)

    def monthly(self, kind):
        if kind not in {'user', 'group'}:
            raise MoneyError('Unknown money subject')
        return to_atoms(self.user_monthly_bazariki if kind == 'user' else self.group_monthly_bazariki)
