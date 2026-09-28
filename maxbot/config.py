from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class MaxConfig:
    admin_ids: frozenset[int]
    models: dict[str, str]
    default_model: str
    base_url: str
    language: str
    max_bytes: int
    max_seconds: int
    download_hosts: frozenset[str]
    private_admin_only: bool


def load_max_config(path: str | Path) -> MaxConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    max_section = raw.get("max") or {}
    stt = raw.get("stt") or {}
    limits = raw.get("limits") or {}
    models = {
        str(alias): str(spec["provider_model"])
        for alias, spec in (stt.get("models") or {}).items()
    }
    default = str(stt.get("default_model") or "")
    if not models or default not in models:
        raise ValueError("Configure stt.models and a valid stt.default_model")
    admins = frozenset(int(value) for value in (max_section.get("admin_user_ids") or []))
    if not admins:
        raise ValueError("max.admin_user_ids must contain a MAX user_id")
    max_mb = int(limits.get("max_file_mb", 20))
    max_seconds = int(limits.get("max_audio_seconds", 600))
    if max_mb < 1 or max_seconds < 1:
        raise ValueError("Media limits must be positive")
    hosts = frozenset(str(host).lower() for host in (max_section.get("download_hosts") or []))
    if not hosts or any("/" in host or ":" in host or host.startswith(".") for host in hosts):
        raise ValueError("Configure exact MAX media download hosts")
    return MaxConfig(
        admin_ids=admins,
        models=models,
        default_model=default,
        base_url=str(stt.get("routerai_base_url") or "https://routerai.ru/api/v1"),
        language=str(stt.get("language") or "ru"),
        max_bytes=max_mb * 1024 * 1024,
        max_seconds=max_seconds,
        download_hosts=hosts,
        private_admin_only=True,
    )
