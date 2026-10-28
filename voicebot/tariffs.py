"""Consume published agent-toolchain prices; never fetch RouterAI's catalog."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

import httpx

from .runtime import InstanceLock

URL = "https://raw.githubusercontent.com/dilukhin/agent-toolchain/refs/heads/automation/routerai-prices/routerai-prices.json"
OWNER = "agent-toolchain:routerai-prices:v1"
PUBLISHER = "dilukhin/agent-toolchain"
NOTICE = "Managed RouterAI prices; do not edit. See docs/routerai_prices_contract_ru.md."
SOURCE = "https://routerai.ru/api/v1/models"
MAX_BYTES = 8 * 1024 * 1024
UNITS = {"token", "second", "request", "image", "megapixel", "search_unit"}
MODEL_ID = re.compile(r"~?[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.:+-]*\Z")
DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


class TariffError(ValueError):
    pass


@dataclass(frozen=True)
class TariffSettings:
    refresh_seconds: int = 21600
    max_age_seconds: int = 172800
    timeout_seconds: int = 15

    def __post_init__(self):
        if any(type(v) is not int for v in (self.refresh_seconds, self.max_age_seconds, self.timeout_seconds)):
            raise ValueError("tariffs settings must be integers")
        if not 60 <= self.refresh_seconds <= 86400 or not self.refresh_seconds <= self.max_age_seconds <= 604800 or not 1 <= self.timeout_seconds <= 60:
            raise ValueError("invalid tariffs refresh, age or timeout")


@dataclass(frozen=True)
class Quantity:
    amount: Decimal
    unit: str

    def __post_init__(self):
        if not isinstance(self.amount, Decimal) or not self.amount.is_finite() or not 0 <= self.amount <= Decimal("1e12") or self.unit not in UNITS:
            raise TariffError("invalid usage bound")
        if len(self.amount.as_tuple().digits) > 80 or self.amount.adjusted() < -78 or len(format(self.amount, "f")) > 80:
            raise TariffError("usage bound precision unsupported")


@dataclass(frozen=True)
class Rate:
    component: str
    unit: str
    rub: Decimal


@dataclass(frozen=True)
class ModelTariff:
    model_id: str
    status: str
    reason: str | None
    api: str | None
    ceilings: tuple[Rate, ...] = ()
    endpoints: tuple[tuple[Rate, ...], ...] = ()


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    source_checked_at: datetime
    published_at: datetime
    models: Mapping[str, ModelTariff]


@dataclass(frozen=True)
class Estimate:
    status: str
    reason: str | None = None
    max_rub: Decimal | None = None
    bazariki: Decimal | None = None
    observed_rub: Decimal | None = None
    snapshot_id: str | None = None
    checked_at: datetime | None = None

    @property
    def available(self) -> bool:
        return self.status == "available"


def _time(value) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
        raise TariffError("invalid UTC timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise TariffError("invalid UTC timestamp") from None


def _price(value) -> Decimal:
    if not isinstance(value, str) or len(value) > 80 or not DECIMAL.fullmatch(value):
        raise TariffError("price must be a decimal string")
    return Decimal(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TariffError("duplicate JSON key")
        result[key] = value
    return result


def _reject_float(value):
    raise TariffError("numeric decimal forbidden in published contract")


def content_hash(content) -> str:
    data = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def _model(model_id, value) -> ModelTariff:
    if not isinstance(value, dict) or value.get("id") != model_id:
        raise TariffError("invalid model identity")
    status = value.get("status")
    if status == "unpriced":
        return ModelTariff(model_id, "unpriced", "publisher-unpriced", value.get("api"))
    if status != "available" or value.get("reason") is not None:
        return ModelTariff(model_id, "incompatible", "unknown-model-status", None)
    architecture = value.get("architecture")
    if not isinstance(architecture, dict) or any(not isinstance(architecture.get(k), list) or not architecture[k] or not all(isinstance(x, str) for x in architecture[k]) for k in ("input_modalities", "output_modalities")):
        return ModelTariff(model_id, "unsafe", "modalities-not-confirmed", None)
    api = value.get("api")
    bounds = value.get("price_ceilings")
    eps = value.get("endpoints")
    if not isinstance(api, str) or not api or not isinstance(bounds, dict) or not isinstance(eps, list) or not 1 <= len(eps) <= 64:
        raise TariffError("incomplete available model")
    ceilings = {}
    for component, bound in bounds.items():
        if not isinstance(component, str) or not isinstance(bound, dict) or bound.get("unit") not in UNITS:
            raise TariffError("invalid ceiling")
        ceilings[component] = Rate(component, bound["unit"], _price(bound.get("rub")))
    endpoints, providers = [], set()
    for ep in eps:
        if not isinstance(ep, dict) or not isinstance(ep.get("provider"), str) or ep["provider"] in providers:
            raise TariffError("invalid provider identity")
        providers.add(ep["provider"])
        if ep.get("source_url") != f"{SOURCE}/{model_id}/endpoints":
            raise TariffError("untrusted endpoint source")
        apis = ep.get("supported_apis")
        if ep.get("variable_pricing") is not False or not isinstance(apis, list) or not all(isinstance(x, str) for x in apis) or api not in apis:
            return ModelTariff(model_id, "unsafe", "variable-or-unknown-route", api)
        pricing, units = ep.get("pricing"), ep.get("pricing_units")
        if not isinstance(pricing, dict) or not pricing or not isinstance(units, dict):
            raise TariffError("incomplete endpoint pricing")
        rates = []
        for component, raw in pricing.items():
            rate = _price(raw)
            bound = ceilings.get(component)
            if not bound or units.get(component) != bound.unit or rate > bound.rub:
                return ModelTariff(model_id, "unsafe", "unapproved-unit-or-price", api)
            rates.append(Rate(component, bound.unit, rate))
        endpoints.append(tuple(rates))
    return ModelTariff(model_id, "available", None, api, tuple(ceilings.values()), tuple(endpoints))


def parse_document(raw: bytes, *, now: datetime) -> Snapshot:
    if len(raw) > MAX_BYTES:
        raise TariffError("published document oversized")
    try:
        value = json.loads(raw, parse_float=_reject_float, parse_constant=_reject_float, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
            raise TariffError("incompatible schema")
        for key, expected in (("owner", OWNER), ("publisher", PUBLISHER), ("_managed_notice", NOTICE), ("source_url", SOURCE), ("state", "published")):
            if value.get(key) != expected:
                raise TariffError("untrusted identity or unpublished candidate")
        checked, published, changed = (_time(value.get(k)) for k in ("source_checked_at", "published_at", "content_changed_at"))
        if not checked <= published <= now or not changed <= published:
            raise TariffError("invalid or future publication time")
        validation = value.get("validation")
        if not isinstance(validation, dict) or validation.get("workflow") != "validate.yml" or validation.get("status") != "success":
            raise TariffError("unvalidated document")
        if not isinstance(validation.get("candidate_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", validation["candidate_sha"]) or type(validation.get("run_id")) is not int or validation["run_id"] <= 0:
            raise TariffError("invalid validation identity")
        content = value.get("content")
        if not isinstance(content, dict) or content.get("currency") != "RUB" or content_hash(content) != value.get("snapshot_id"):
            raise TariffError("currency or content hash mismatch")
        models = content.get("models")
        if not isinstance(models, dict) or not 1 <= len(models) <= 10000 or not all(isinstance(k, str) and MODEL_ID.fullmatch(k) for k in models):
            raise TariffError("invalid models")
        result = {key: _model(key, record) for key, record in models.items()}
        return Snapshot(value["snapshot_id"], checked, published, MappingProxyType(result))
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise TariffError("published document validation failed") from None


class PriceCatalog:
    def __init__(self, cache_path: str | Path, settings: TariffSettings | None = None, *, clock: Callable[[], datetime] | None = None, transport=None):
        self.path = Path(cache_path)
        self.pause_path = self.path.with_suffix(self.path.suffix + ".paused")
        self.settings = settings or TariffSettings()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.transport = transport
        self.snapshot: Snapshot | None = None
        self.last_error: str | None = None
        self._etag: str | None = None
        self._refresh_lock = asyncio.Lock()

    def load(self) -> bool:
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            self.snapshot = parse_document(raw, now=self.clock())
            return True
        except (OSError, TariffError):
            self.last_error = "cache-unavailable"
            return False

    def _accept(self, raw: bytes) -> None:
        snapshot = parse_document(raw, now=self.clock())
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The application owns the instance lock. This additional file lease also
        # protects independent simultaneous refreshers against stale overwrites.
        with InstanceLock(str(self.path) + ".lock"):
            previous = self.snapshot
            if self.path.exists():
                with self.path.open("rb") as stream:
                    disk_raw = stream.read(MAX_BYTES + 1)
                try:
                    disk = parse_document(disk_raw, now=self.clock())
                except TariffError:
                    disk = None  # A fully validated remote document can repair a corrupt local cache.
                if disk is not None and (previous is None or disk.source_checked_at > previous.source_checked_at):
                    previous = disk
            if previous:
                if snapshot.source_checked_at < previous.source_checked_at or snapshot.published_at < previous.published_at:
                    raise TariffError("older published observation")
                if snapshot.source_checked_at == previous.source_checked_at and snapshot.snapshot_id != previous.snapshot_id:
                    raise TariffError("different content with replayed observation")
            fd, temporary = tempfile.mkstemp(prefix=".routerai-prices-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.snapshot = snapshot

    async def refresh(self) -> bool:
        if self._refresh_lock.locked():
            return False
        async with self._refresh_lock:
            try:
                headers = {"Accept": "application/json", "Accept-Encoding": "identity", "Cache-Control": "no-cache"}
                if self._etag:
                    headers["If-None-Match"] = self._etag
                async with httpx.AsyncClient(timeout=self.settings.timeout_seconds, follow_redirects=False, transport=self.transport) as client:
                    async with client.stream("GET", URL, headers=headers) as response:
                        if response.status_code == 304:
                            # 304 says nothing about source freshness. Keep the
                            # original source timestamp, including after expiry.
                            self.last_error = None if self.snapshot is not None else "refresh-failed"
                            return self.snapshot is not None
                        response.raise_for_status()
                        if 300 <= response.status_code < 400:
                            raise TariffError("redirect forbidden")
                        raw = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            raw.extend(chunk)
                            if len(raw) > MAX_BYTES:
                                raise TariffError("published document oversized")
                        await asyncio.to_thread(self._accept, bytes(raw))
                        self._etag = response.headers.get("ETag")
                self.last_error = None
                return True
            except (httpx.HTTPError, OSError, TariffError, RuntimeError, ImportError, ValueError):
                self.last_error = "refresh-failed"
                return False

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.settings.refresh_seconds)

    def status(self) -> str:
        if self.pause_path.exists():
            return "unsafe"
        if self.snapshot is None:
            return "unavailable"
        age = (self.clock() - self.snapshot.source_checked_at).total_seconds()
        return "stale" if age < 0 or age > self.settings.max_age_seconds else "available"

    def estimate(self, model_id: str, quantities: Mapping[str, Quantity], *, api: str) -> Estimate:
        snapshot = self.snapshot
        if self.pause_path.exists():
            return Estimate("unsafe", "paid-requests-paused")
        age = (self.clock() - snapshot.source_checked_at).total_seconds() if snapshot else None
        status = "unavailable" if age is None else ("stale" if age < 0 or age > self.settings.max_age_seconds else "available")
        if snapshot is None or status != "available":
            return Estimate(status, "no-fresh-published-prices")
        info = {"snapshot_id": snapshot.snapshot_id, "checked_at": snapshot.source_checked_at}
        model = snapshot.models.get(model_id)
        if not model:
            return Estimate("unpriced", "model-missing", **info)
        if model.status != "available":
            return Estimate(model.status, model.reason, **info)
        if model.api != api:
            return Estimate("unsafe", "api-mismatch", **info)
        # Ceilings include every approved positive component, even if a provider
        # omits that optional component today. Missing quantity is never zero.
        def cost(rates):
            total = Decimal(0)
            for rate in rates:
                if not rate.rub:
                    continue
                quantity = quantities.get(rate.component)
                if not isinstance(quantity, Quantity) or quantity.unit != rate.unit:
                    raise TariffError("missing or incompatible upper usage bound")
                total += rate.rub * quantity.amount
            return total
        try:
            with localcontext() as context:
                context.prec = 400
                upper = cost(model.ceilings)
                observed = max(cost(endpoint) for endpoint in model.endpoints)
                return Estimate("available", max_rub=upper, bazariki=upper * Decimal(100), observed_rub=observed, **info)
        except TariffError:
            return Estimate("unsafe", "usage-bound-unknown", **info)

    def estimate_audio(self, model_id: str, seconds_upper: int | Decimal) -> Estimate:
        if isinstance(seconds_upper, bool) or not isinstance(seconds_upper, (int, Decimal)):
            return Estimate("unsafe", "audio-duration-unknown")
        try:
            duration = Quantity(Decimal(seconds_upper), "second")
        except TariffError:
            return Estimate("unsafe", "audio-duration-unknown")
        model = self.snapshot.models.get(model_id) if self.snapshot else None
        quantities = {}
        if model:
            for rate in model.ceilings:
                if rate.unit == "second":
                    quantities[rate.component] = duration
                elif rate.unit == "request":
                    quantities[rate.component] = Quantity(Decimal(1), "request")
        return self.estimate(model_id, quantities, api="audio/transcriptions")

    def model_summary(self, model_id: str) -> str:
        snapshot = self.snapshot
        model = snapshot.models.get(model_id) if snapshot else None
        if not model or model.status != "available":
            return "единицы/тариф не подтверждены"
        units = ", ".join(sorted({r.unit for r in model.ceilings if r.rub})) or "нулевой тариф"
        return "единицы: " + units + "; проверка " + snapshot.source_checked_at.strftime("%Y-%m-%d %H:%M UTC")

    def summary(self) -> str:
        state = self.status()
        if not self.snapshot:
            return f"Тарифы: {state}; опубликованный каталог не получен."
        stamp = self.snapshot.source_checked_at.strftime("%Y-%m-%d %H:%M UTC")
        unusable = sum(m.status != "available" for m in self.snapshot.models.values())
        return f"Тарифы: {state}; проверка {stamp}; {self.snapshot.snapshot_id}; непригодных моделей {unusable}; обновление {self.last_error or 'ok'}."
