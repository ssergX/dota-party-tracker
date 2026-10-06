"""Клиент Stratz GraphQL API: позиция/роль/лейн/IMP и пер-матч статистика.

Синхронный (requests), как opendota.py: троттлинг (слоты), ретраи; запросы потоков перекрываются. Лимиты free-ключа
(8/сек, 150/мин, 1500/час) велики для бота пати, но запросы всё равно батчим —
один запрос на игрока забирает пачку матчей сразу.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import requests

URL = "https://api.stratz.com/graphql"
_RETRY_STATUSES = {429, 500, 502, 503, 504}

_PLAYER_FIELDS = """
    lobbyType
    players {
      steamAccountId isRadiant partyId heroId
      position role lane imp
      goldPerMinute experiencePerMinute networth heroDamage towerDamage heroHealing
      numLastHits numDenies level
    }
"""

_FULL_MATCH_QUERY = """
query {
  match(id: %d) {
    id durationSeconds didRadiantWin startDateTime
    players {
      steamAccountId isRadiant heroId position role lane kills deaths assists imp
      goldPerMinute experiencePerMinute networth heroDamage towerDamage heroHealing
      numLastHits numDenies level
      steamAccount { name }
    }
  }
}
"""

_FIELD_MAP = {
    "goldPerMinute": "gpm", "experiencePerMinute": "xpm", "networth": "net_worth",
    "heroDamage": "hero_damage", "towerDamage": "tower_damage", "heroHealing": "hero_healing",
    "numLastHits": "last_hits", "numDenies": "denies", "level": "level",
}


def _position(value) -> Optional[int]:
    """'POSITION_3' → 3; неизвестно → None."""
    try:
        return int(str(value).rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        return None


def _party_size(rows: list, me: dict) -> Optional[int]:
    """Размер пати игрока: союзники с тем же partyId (нет partyId → играл один)."""
    if "isRadiant" not in me:
        return None
    party = me.get("partyId")
    if party is None:
        return 1
    return sum(1 for r in rows if r.get("isRadiant") == me["isRadiant"] and r.get("partyId") == party)


class Stratz:
    def __init__(
        self,
        api_key: str,
        min_interval: float = 0.2,
        timeout=(5, 25),
        max_retries: int = 3,
        retry_sleep: float = 1.5,
        chunk: int = 10,
        session=None,
    ):
        self.api_key = api_key
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_sleep = retry_sleep
        self.chunk = chunk
        self._session = session or requests.Session()
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        """Резервируем слот под локом, спим вне лока (как в opendota.py): запросы идут параллельно."""
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._last_call + self.min_interval)
            self._last_call = slot
        wait = slot - now
        if wait > 0:
            time.sleep(wait)

    def _query(self, query: str, variables: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "STRATZ_API",  # без него Stratz отвечает 403
            "Content-Type": "application/json",
        }
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self._session.post(
                    URL, json={"query": query, "variables": variables},
                    headers=headers, timeout=self.timeout,
                )
                if resp.status_code not in _RETRY_STATUSES:
                    resp.raise_for_status()
                    payload = resp.json()
                    if payload.get("errors"):
                        raise RuntimeError(f"Stratz: {payload['errors'][0].get('message')}")
                    return payload.get("data") or {}
                last_exc = RuntimeError(f"Stratz HTTP {resp.status_code}")
            except requests.HTTPError:
                raise  # 4xx — не ретраим
            except (requests.RequestException, ValueError) as exc:  # сеть / битый JSON
                last_exc = exc
            if attempt < self.max_retries - 1:  # после последней попытки не спим зря
                time.sleep(self.retry_sleep * (attempt + 1))
        raise last_exc or RuntimeError("Stratz: не удалось получить ответ")

    def get_matches(
        self, account_id: int, match_ids: list[int], hints: Optional[dict] = None
    ) -> dict[int, dict]:
        """Данные игрока по конкретным матчам → {match_id: поля}.

        История игрока у Stratz отстаёт (и lobbyType там ненадёжен), а запрос по match(id)
        отдаёт свежие матчи — поэтому берём по id, пачками через алиасы GraphQL (один запрос
        на chunk матчей). Матчей, которых у Stratz ещё нет, в результате не будет.

        hints — {match_id: (is_radiant, hero_id)} из сохранённых матчей: у скрытого профиля steamAccountId
        в ответе обнулён, и тогда игрока находим по стороне и герою (если такая строка ровно одна).
        """
        result: dict[int, dict] = {}
        for i in range(0, len(match_ids), self.chunk):
            ids = match_ids[i:i + self.chunk]
            body = "\n".join(f"  m{n}: match(id: {int(mid)}) {{{_PLAYER_FIELDS}}}" for n, mid in enumerate(ids))
            data = self._query(f"query {{\n{body}\n}}", {})
            for n, mid in enumerate(ids):
                rows = (data.get(f"m{n}") or {}).get("players") or []
                if not rows:
                    continue
                row = next((r for r in rows if r.get("steamAccountId") == account_id), None)
                if row is None and hints and mid in hints:
                    is_radiant, hero_id = hints[mid]
                    same = [r for r in rows if r.get("isRadiant") == is_radiant and r.get("heroId") == hero_id]
                    row = same[0] if len(same) == 1 else None
                if row is None and not any("steamAccountId" in r for r in rows):
                    row = rows[0]  # ответ без id игроков — единственная строка
                if row is None:
                    continue
                info = {
                    "party_size": _party_size(rows, row),
                    "position": _position(row.get("position")),
                    "role": row.get("role"),
                    "lane": row.get("lane"),
                    "imp": row.get("imp"),
                }
                for src, out in _FIELD_MAP.items():
                    info[out] = row.get(src)
                result[mid] = info
        return result

    def get_match(self, match_id: int):
        """Полный матч (все 10 игроков) по id → dict | None, если Stratz матча не знает."""
        data = self._query(_FULL_MATCH_QUERY % int(match_id), {})
        raw = data.get("match")
        if not raw:
            return None
        players = []
        for row in raw.get("players") or []:
            player = {
                "account_id": row.get("steamAccountId"),
                "name": (row.get("steamAccount") or {}).get("name"),
                "is_radiant": bool(row.get("isRadiant")),
                "hero_id": row.get("heroId"),
                "position": _position(row.get("position")),
                "role": row.get("role"),
                "lane": row.get("lane"),
                "kills": row.get("kills"),
                "deaths": row.get("deaths"),
                "assists": row.get("assists"),
                "imp": row.get("imp"),
            }
            for src, out in _FIELD_MAP.items():
                player[out] = row.get(src)
            players.append(player)
        return {
            "match_id": raw.get("id", match_id),
            "start_time": raw.get("startDateTime"),
            "duration": raw.get("durationSeconds"),
            "radiant_win": raw.get("didRadiantWin"),
            "players": players,
        }
