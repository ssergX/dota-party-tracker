"""Клиент Steam Web API: «играет ли сейчас в Dota 2» (GetPlayerSummaries).

Синхронный (requests), вызывается через asyncio.to_thread. Один запрос — до 100 аккаунтов.
Steam отдаёт только «в Dota 2 / нет» (без героя и без различия лобби/матч) и только для открытых профилей.
"""
from __future__ import annotations

from typing import Optional

import requests

from mmrbot.ids import STEAMID64_BASE

URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
DOTA_APP_ID = "570"
BATCH = 100


def parse_in_dota(data: dict, account_ids: list[int]) -> dict[int, Optional[bool]]:
    """{account_id: True (в Dota) | False | None (профиль закрыт / Steam не вернул игрока)}."""
    result: dict[int, Optional[bool]] = {a: None for a in account_ids}
    for player in (data.get("response") or {}).get("players") or []:
        try:
            account_id = int(player["steamid"]) - STEAMID64_BASE
        except (KeyError, TypeError, ValueError):
            continue
        if account_id not in result or player.get("communityvisibilitystate") != 3:
            continue
        result[account_id] = str(player.get("gameid", "")) == DOTA_APP_ID
    return result


class Steam:
    def __init__(self, api_key: str, session=None, timeout: float = 15):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout

    def get_in_dota(self, account_ids: list[int]) -> dict[int, Optional[bool]]:
        result: dict[int, Optional[bool]] = {}
        for i in range(0, len(account_ids), BATCH):
            chunk = account_ids[i:i + BATCH]
            try:
                resp = self.session.get(
                    URL,
                    params={"key": self.api_key, "steamids": ",".join(str(STEAMID64_BASE + a) for a in chunk)},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                result.update(parse_in_dota(resp.json(), chunk))
            except Exception as exc:
                # В тексте requests-ошибок бывает полный URL с ключом — наружу отдаём только тип сбоя.
                raise RuntimeError(f"Steam API недоступен ({type(exc).__name__})") from None
        return result
