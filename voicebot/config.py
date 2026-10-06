from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paid_store import BudgetLimits
from .formatter import FormatterConfig


@dataclass(frozen=True)
class ModelConfig:
    alias: str
    provider_model: str
    description: str = ""
    audio_format: str | None = None


@dataclass(frozen=True)
class AppConfig:
    bot_username: str
    admin_user_ids: set[int]
    allow_private_transcription_for_admins: bool
    allow_private_transcription_for_non_admins: bool
    one_time_approval_jobs: int
    notify_admins_on_new_request: bool
    max_audio_seconds: int
    max_file_mb: int
    admin_notify_cooldown_seconds: int
    default_model: str
    language: str
    temperature: float | None
    routerai_base_url: str
    models: dict[str, ModelConfig]
    budget: BudgetLimits = field(default_factory=BudgetLimits)
    formatter: FormatterConfig = field(default_factory=FormatterConfig)

    def model(self, alias: str | None = None) -> ModelConfig:
        selected = alias or self.default_model
        if selected not in self.models:
            raise KeyError(f"Unknown model alias: {selected}")
        return self.models[selected]


def _read_bool(root: dict[str, Any], path: tuple[str, ...], default: bool) -> bool:
    value: Any = root
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return bool(value)


def _read_int(root: dict[str, Any], path: tuple[str, ...], default: int) -> int:
    value: Any = root
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_str(root: dict[str, Any], path: tuple[str, ...], default: str) -> str:
    value: Any = root
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return str(value)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    telegram = raw.get("telegram") or {}
    access = raw.get("access") or {}
    limits = raw.get("limits") or {}
    stt = raw.get("stt") or {}

    if access.get("allow_private_transcription_for_non_admins", False):
        raise ValueError("Управление личным доступом требует access.allow_private_transcription_for_non_admins: false. "
                         "Разрешения обычных пользователей теперь выдаются в меню /access.")

    models_raw = stt.get("models") or {}
    models: dict[str, ModelConfig] = {}
    for alias, model_raw in models_raw.items():
        model_raw = model_raw or {}
        provider_model = model_raw.get("provider_model")
        if not provider_model:
            raise ValueError(f"stt.models.{alias}.provider_model is required")
        models[str(alias)] = ModelConfig(
            alias=str(alias),
            provider_model=str(provider_model),
            description=str(model_raw.get("description") or ""),
            audio_format=(str(model_raw.get("format")) if model_raw.get("format") else None),
        )

    default_model = str(stt.get("default_model") or "")
    if not default_model:
        raise ValueError("stt.default_model is required")
    if default_model not in models:
        raise ValueError(f"stt.default_model={default_model!r} is not present in stt.models")

    admin_ids = set()
    for user_id in telegram.get("admin_user_ids") or []:
        admin_ids.add(int(user_id))

    budget_raw = raw.get('budget') or {}
    budget = BudgetLimits(**{name: int(budget_raw.get(name, field.default))
                            for name, field in BudgetLimits.__dataclass_fields__.items()})
    if any(value < 1 for value in budget.__dict__.values()):
        raise ValueError('Все лимиты budget должны быть положительными')
    formatter_raw = raw.get('formatter') or {}
    formatter_models = {str(alias): str(spec['provider_model'])
                        for alias, spec in (formatter_raw.get('models') or {}).items()}
    formatter = FormatterConfig(
        enabled=bool(formatter_raw.get('enabled',False)),
        default_model=str(formatter_raw.get('default_model') or ''),
        models=formatter_models,
        max_input_chars=int(formatter_raw.get('max_input_chars',12000)),
        max_output_tokens=int(formatter_raw.get('max_output_tokens',4096)),
        timeout_seconds=int(formatter_raw.get('timeout_seconds',30)),
    )
    if min(formatter.max_input_chars,formatter.max_output_tokens,formatter.timeout_seconds)<1:
        raise ValueError('Лимиты formatter должны быть положительными')
    if formatter.enabled and (formatter.default_model not in formatter.models or not formatter.models[formatter.default_model]):
        raise ValueError('Для formatter выберите default_model и provider_model')

    return AppConfig(
        bot_username=str(telegram.get("bot_username") or ""),
        admin_user_ids=admin_ids,
        allow_private_transcription_for_admins=bool(
            access.get("allow_private_transcription_for_admins", True)
        ),
        allow_private_transcription_for_non_admins=bool(
            access.get("allow_private_transcription_for_non_admins", False)
        ),
        one_time_approval_jobs=int(access.get("one_time_approval_jobs", 1)),
        notify_admins_on_new_request=bool(access.get("notify_admins_on_new_request", True)),
        max_audio_seconds=int(limits.get("max_audio_seconds", 600)),
        max_file_mb=int(limits.get("max_file_mb", 20)),
        admin_notify_cooldown_seconds=int(limits.get("admin_notify_cooldown_seconds", 300)),
        default_model=default_model,
        language=str(stt.get("language") or "ru"),
        temperature=(float(stt["temperature"]) if "temperature" in stt and stt["temperature"] is not None else None),
        routerai_base_url=str(stt.get("routerai_base_url") or "https://routerai.ru/api/v1"),
        models=models,
        budget=budget,
        formatter=formatter,
    )
