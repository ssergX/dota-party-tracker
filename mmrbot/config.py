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
    opendota_min_interval: float = 1.1  # пауза между запросами к OpenDota, сек
    backup_keep: int = 7  # сколько ежедневных копий БД хранить (0 — бэкап выключен)
    steam_api_key: Optional[str] = None  # Steam Web API: оповещения «зашёл в Dota 2» (без ключа выключены)
    opendota_burst: int = 5  # сколько запросов к OpenDota можно отправить подряд без паузы


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
            "BOT_TOKEN не задан. Скопируйте .env.example в .env и впишите токен от @BotFather."
        )
    api_key = os.getenv("OPENDOTA_API_KEY") or None
    # С ключом лимиты OpenDota выше — можно опрашивать чаще (быстрее графики и /stats).
    default_interval = 0.25 if api_key else 1.1
    try:
        interval = max(0.0, float(os.getenv("OPENDOTA_MIN_INTERVAL", default_interval)))
    except ValueError:
        interval = default_interval
    return Config(
        bot_token=token,
        opendota_api_key=api_key,
        opendota_min_interval=interval,
        stratz_api_key=os.getenv("STRATZ_API_KEY") or None,
        steam_api_key=os.getenv("STEAM_API_KEY") or None,
        db_path=os.getenv("DB_PATH", "mmrbot.db"),
        backup_keep=_int_env("BACKUP_KEEP", 7),
        # 5 подряд + по одному в 1.1 с — это меньше 60 запросов в любую минуту.
        opendota_burst=max(1, _int_env("OPENDOTA_BURST", 5)),
    )
