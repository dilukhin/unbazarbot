from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


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
    )
