"""Клиент OpenDota API (без ключа работает; ключ повышает лимиты).

Синхронный (requests) с троттлингом и ретраями. В async-коде вызывается через
asyncio.to_thread, чтобы не блокировать event loop. Сессию можно подставить (тесты).
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import requests

BASE_URL = "https://api.opendota.com/api"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


_MAX_RETRY_AFTER = 30.0


def _retry_after(resp, default: float) -> float:
    """Пауза из заголовка Retry-After (секунды), ограниченная сверху; иначе default."""
    try:
        value = float((getattr(resp, "headers", None) or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return default
    return min(max(value, 0.0), _MAX_RETRY_AFTER)


class OpenDota:
    HISTORY_PAGE = 1000  # размер страницы при полной загрузке истории

    def __init__(
        self,
        api_key: Optional[str] = None,
        min_interval: float = 1.1,
        timeout: int = 30,
        max_retries: int = 3,
        session=None,
    ):
        self.api_key = api_key
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session or requests.Session()
        self._last_call = 0.0
        # Один клиент шарится между to_thread-воркерами: сериализуем запросы,
        # чтобы не ломать троттлинг и не делить requests.Session между потоками гонкой.
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get(self, path: str, params: Optional[dict] = None):
        params = dict(params or {})
        if self.api_key:
            params["api_key"] = self.api_key
        url = f"{BASE_URL}{path}"

        with self._lock:  # сериализация запросов между потоками (троттлинг + общая Session)
            last_exc: Optional[Exception] = None
            for attempt in range(self.max_retries):
                self._throttle()
                delay = 1.5 * (attempt + 1)
                try:
                    resp = self._session.get(url, params=params, timeout=self.timeout)
                    if resp.status_code in _RETRY_STATUSES:
                        last_exc = RuntimeError(f"OpenDota HTTP {resp.status_code}")
                        delay = _retry_after(resp, delay)
                    else:
                        resp.raise_for_status()  # 4xx — не ретраим, сразу наверх
                        return resp.json()
                except requests.HTTPError:
                    raise
                except (requests.RequestException, ValueError) as exc:  # сеть / битый JSON
                    last_exc = exc
                if attempt < self.max_retries - 1:  # после последней попытки не спим зря
                    time.sleep(delay)
            raise last_exc or RuntimeError("OpenDota: не удалось получить ответ")

    def refresh(self, account_id: int) -> bool:
        """Попросить OpenDota перечитать историю матчей игрока (POST /refresh).

        Обновление у OpenDota асинхронное: свежие матчи появятся не мгновенно, а
        через некоторое время — зато следующий опрос будет актуальнее. Best-effort.
        """
        with self._lock:
            self._throttle()
            try:
                self._session.post(f"{BASE_URL}/players/{account_id}/refresh", timeout=self.timeout)
                return True
            except Exception:
                return False

    def get_profile(self, account_id: int) -> dict:
        data = self._get(f"/players/{account_id}") or {}
        profile = data.get("profile") or {}
        return {
            "rank_tier": data.get("rank_tier"),
            "leaderboard_rank": data.get("leaderboard_rank"),
            "personaname": profile.get("personaname"),
            "avatarfull": profile.get("avatarfull"),
            "profileurl": profile.get("profileurl"),
            "steamid": profile.get("steamid"),
            "loccountrycode": profile.get("loccountrycode"),
            "plus": bool(profile.get("plus")),
            "last_login": profile.get("last_login"),
        }

    def get_matches(self, account_id: int, limit: Optional[int] = 200, lobby_type: Optional[int] = 7) -> list[dict]:
        """Матчи игрока. По умолчанию только ранкед (lobby_type=7) — фильтр на стороне OpenDota,
        иначе лимит забивается обычными играми. limit=None — вся история."""
        base: dict = {"significant": 0}
        if lobby_type is not None:
            base["lobby_type"] = lobby_type
        path = f"/players/{account_id}/matches"
        if limit is not None:
            data = self._get(path, params=dict(base, limit=limit))
            return data if isinstance(data, list) else []
        # Вся история — постранично: один огромный ответ OpenDota иногда рвёт (HTTP 500).
        result: list[dict] = []
        offset = 0
        while True:
            page = self._get(path, params=dict(base, limit=self.HISTORY_PAGE, offset=offset))
            if not isinstance(page, list):
                break
            result.extend(page)
            if len(page) < self.HISTORY_PAGE:
                break
            offset += self.HISTORY_PAGE
        return result

    def get_lanes(self, account_id: int) -> dict:
        """Игры/победы по линиям из /players/{id}/counts → {lane_int: (games, wins)}.

        lane_role: 0 — линия неизвестна (нераспарсенные матчи), 1 safe, 2 mid, 3 off, 4 jungle.
        """
        data = self._get(f"/players/{account_id}/counts") or {}
        lane_role = data.get("lane_role") or {}
        result: dict[int, tuple[int, int]] = {}
        for key, stat in lane_role.items():
            try:
                lane = int(key)
            except (TypeError, ValueError):
                continue
            result[lane] = (stat.get("games", 0), stat.get("win", 0))
        return result

    def get_gpm_distribution(self, account_id: int) -> dict:
        """Медиана и пик GPM из гистограммы /players/{id}/histograms/gold_per_min."""
        data = self._get(f"/players/{account_id}/histograms/gold_per_min")
        buckets = [(b["x"], b.get("games", 0)) for b in data if b.get("games")] if isinstance(data, list) else []
        if not buckets:
            return {"median": None, "best": None}
        total = sum(games for _, games in buckets)
        half = total / 2
        cumulative = 0
        median = None
        for x, games in sorted(buckets):
            cumulative += games
            if cumulative >= half:
                median = x
                break
        best = max(x for x, _ in buckets)
        return {"median": median, "best": best}

    _MATCH_FIELDS = {
        "gold_per_min": "gpm", "xp_per_min": "xpm", "last_hits": "last_hits", "denies": "denies",
        "hero_damage": "hero_damage", "tower_damage": "tower_damage", "hero_healing": "hero_healing",
        "net_worth": "net_worth", "level": "level",
    }

    def get_match_player_stats(self, match_id: int, account_id: int) -> Optional[dict]:
        """Пер-матч статистика игрока из /matches/{id} + benchmarks (перцентиль vs тот же герой).

        GPM/урон/хил/нетворт и benchmarks приходят БЕЗ парса (из сводки Valve).
        """
        match = self._get(f"/matches/{match_id}") or {}
        player = next(
            (p for p in match.get("players", []) if p.get("account_id") == account_id), None
        )
        if player is None:
            return None
        result = {out: player.get(src) for src, out in self._MATCH_FIELDS.items()}
        benchmarks = {}
        for metric, value in (player.get("benchmarks") or {}).items():
            benchmarks[metric] = value.get("pct") if isinstance(value, dict) else value
        result["benchmarks"] = benchmarks
        return result

    def get_totals(self, account_id: int) -> dict:
        """Средние GPM/XPM/last hits из /players/{id}/totals (sum/n по полям)."""
        data = self._get(f"/players/{account_id}/totals")
        wanted = {"gold_per_min": "gpm", "xp_per_min": "xpm", "last_hits": "last_hits"}
        result: dict = {"gpm": None, "xpm": None, "last_hits": None}
        if isinstance(data, list):
            for row in data:
                key = wanted.get(row.get("field"))
                if key and row.get("n"):
                    result[key] = row["sum"] / row["n"]
        return result
