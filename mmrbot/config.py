"""Конфигурация из окружения/.env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


@dataclass
class Config:
    bot_token: str
    opendota_api_key: Optional[str]
    stratz_api_key: Optional[str]
    db_path: str
    backup_keep: int = 7  # сколько ежедневных копий БД хранить (0 — бэкап выключен)


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, default)))
    except ValueError:
        return default


def load_config() -> Config:
    load_dotenv()  # подхватывает .env из текущей директории
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN не задан. Скопируй .env.example в .env и вставь токен от @BotFather."
        )
    return Config(
        bot_token=token,
        opendota_api_key=os.getenv("OPENDOTA_API_KEY") or None,
        stratz_api_key=os.getenv("STRATZ_API_KEY") or None,
        db_path=os.getenv("DB_PATH", "mmrbot.db"),
        backup_keep=_int_env("BACKUP_KEEP", 7),
    )
