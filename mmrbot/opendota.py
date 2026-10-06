"""Клиент OpenDota API (без ключа работает; ключ повышает лимиты).

Синхронный (requests) с троттлингом и ретраями. В async-коде вызывается через
asyncio.to_thread, чтобы не блокировать event loop. Сессию можно подставить (тесты).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Optional

import requests

BASE_URL = "https://api.opendota.com/api"
_RETRY_STATUSES = {429, 500, 502, 503, 504}
RANKED_LOBBY = 7


_MAX_RETRY_AFTER = 30.0
_INLINE_WAIT = 10.0  # дольше этого лимит не пережидаем в запросе — сразу отдаём кэш из БД
_MAX_BLOCK = 900.0  # потолок паузы после 429 (сек): дальше пробуем снова
_BLOCK_NO_HEADER = 30.0  # 429 без Retry-After и после всех попыток — короткая пауза


class RateLimited(RuntimeError):
    """OpenDota ответил 429: до конца паузы запросы не отправляются (вызывающий код берёт кэш)."""


def _retry_after_raw(resp) -> Optional[float]:
    """Значение заголовка Retry-After в секундах; None — заголовка нет или он нечисловой."""
    try:
        return max(float((getattr(resp, "headers", None) or {}).get("Retry-After")), 0.0)
    except (TypeError, ValueError):
        return None


def _retry_after(resp, default: float) -> float:
    """Пауза из заголовка Retry-After (секунды), ограниченная сверху; иначе default."""
    value = _retry_after_raw(resp)
    return default if value is None else min(value, _MAX_RETRY_AFTER)


def _party_size(players: list, me: dict) -> Optional[int]:
    """Размер пати игрока из состава матча: party_size от OpenDota, иначе союзники с тем же party_id."""
    size = me.get("party_size")
    if size:
        return size
    party_id = me.get("party_id")
    if not party_id or me.get("player_slot") is None:  # 0 — «нет пати» у одиночек: по нему группу не собрать
        return None
    side = me["player_slot"] < 128
    same = sum(
        1 for p in players
        if p.get("party_id") == party_id and p.get("player_slot") is not None and (p["player_slot"] < 128) == side
    )
    return same or None


class OpenDota:
    HISTORY_PAGE = 1000  # размер страницы при полной загрузке истории
    MATCH_CACHE_SIZE = 64  # сколько последних матчей держим в памяти
    MATCH_CACHE_TTL = 600  # сек: свежий матч может дополниться — долго не держим
    REFRESH_GAP = 600  # сек: POST /refresh одного игрока не чаще (обновление у OpenDota всё равно асинхронное)

    def __init__(
        self,
        api_key: Optional[str] = None,
        min_interval: float = 1.1,
        timeout=(5, 25),  # (соединение, чтение): недоступный сервер отваливается за 5 с, а не за 30
        max_retries: int = 3,
        session=None,
        burst: int = 1,
    ):
        self.api_key = api_key
        self.min_interval = min_interval
        # Сколько запросов можно отправить подряд без паузы (дальше — по одному в min_interval).
        # Лимит OpenDota считается за минуту, поэтому короткая пачка в него укладывается, а
        # /stats на несколько игроков не ждёт по секунде на каждого.
        self.burst = max(1, int(burst))
        self.timeout = timeout
        self.max_retries = max_retries
        if session is None:
            session = requests.Session()
            # Игроки обновляются параллельно: пул по умолчанию (10) рвал бы лишние соединения.
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16)
            session.mount("https://", adapter)
        self._session = session
        self._last_call = 0.0
        self._blocked_until = 0.0  # monotonic-время, до которого OpenDota просил не ходить (429)
        self._match_cache: "OrderedDict[int, tuple[float, dict]]" = OrderedDict()
        self._match_locks: dict[int, threading.Lock] = {}
        self._refresh_at: dict[int, float] = {}
        # Лок защищает только резервирование «слота» запроса (троттлинг): сами HTTP-запросы идут
        # параллельно — ожидание сети перекрывается между потоками, частота остаётся в лимите.
        self._lock = threading.Lock()

    def _throttle(self) -> None:
        """Резервируем слот под локом, спим вне лока — потоки не стоят в очереди целиком.

        Ведро с жетонами (GCRA): средняя частота — один запрос в min_interval, но первые burst
        запросов после простоя уходят сразу. _last_call — «теоретическое время» следующего слота.
        """
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._last_call)
            wait = slot - (self.burst - 1) * self.min_interval - now
            self._last_call = slot + self.min_interval
        if wait > 0:
            time.sleep(wait)

    def _get(self, path: str, params: Optional[dict] = None):
        params = dict(params or {})
        if self.api_key:
            params["api_key"] = self.api_key
        url = f"{BASE_URL}{path}"

        last_exc: Optional[Exception] = None
        limited = False
        for attempt in range(self.max_retries):
            if time.monotonic() < self._blocked_until:
                raise RateLimited("OpenDota: лимит запросов, пауза ещё не истекла")
            self._throttle()
            delay = 1.5 * (attempt + 1)
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
                if resp.status_code in _RETRY_STATUSES:
                    last_exc = RuntimeError(f"OpenDota HTTP {resp.status_code}")
                    limited = resp.status_code == 429
                    asked = _retry_after_raw(resp) if limited else None
                    if asked is not None and asked > _INLINE_WAIT:
                        # Долгая пауза (дневной лимит и т.п.): не висим и не шлём запросы впустую.
                        self._blocked_until = time.monotonic() + min(asked, _MAX_BLOCK)
                        raise RateLimited(f"OpenDota HTTP 429, повтор через {asked:.0f} с")
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
        if limited:  # лимит не отпустил за все попытки — остальные запросы этой волны не тратим
            self._blocked_until = time.monotonic() + _BLOCK_NO_HEADER
        raise last_exc or RuntimeError("OpenDota: не удалось получить ответ")

    def refresh(self, account_id: int) -> bool:
        """Попросить OpenDota перечитать историю матчей игрока (POST /refresh).

        Обновление у OpenDota асинхронное: свежие матчи появятся не мгновенно, а
        через некоторое время — зато следующий опрос будет актуальнее. Best-effort.
        Повторный вызов раньше REFRESH_GAP ничего не шлёт (False): пинок уже в очереди,
        а слот троттлинга и лимит запросов нужнее самим данным.
        """
        now = time.monotonic()
        with self._lock:
            last = self._refresh_at.get(account_id)
            if last is not None and now - last < self.REFRESH_GAP:
                return False
            self._refresh_at[account_id] = now
        if now < self._blocked_until:
            return False
        self._throttle()
        try:
            self._session.post(f"{BASE_URL}/players/{account_id}/refresh", timeout=self.timeout)
            return True
        except Exception:
            with self._lock:
                self._refresh_at.pop(account_id, None)  # не дошло — в следующий раз попробуем снова
            return False

    def get_heroes(self) -> list[dict]:
        """Справочник героев /heroes: [{id, localized_name, ...}] (пусто при сбое формата)."""
        data = self._get("/heroes")
        return data if isinstance(data, list) else []

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
            "fh_unavailable": profile.get("fh_unavailable"),  # None — признака нет в ответе
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

    def get_recent_matches(self, account_id: int) -> list[dict]:
        """Последние ~20 матчей игрока (все лобби) сразу с GPM/XPM/уроном/лечением/добиваниями.

        Один лёгкий запрос вместо списка на 200 матчей + отдельного запроса на каждый матч.
        Ранкед отбирает вызывающий код; порядок — от новых к старым.
        """
        data = self._get(f"/players/{account_id}/recentMatches")
        return data if isinstance(data, list) else []

    def get_lanes(self, account_id: int, lobby_type: Optional[int] = RANKED_LOBBY) -> dict:
        """Игры/победы по линиям из /players/{id}/counts → {lane_int: (games, wins)}.

        lane_role: 0 — линия неизвестна (нераспарсенные матчи), 1 safe, 2 mid, 3 off, 4 jungle.
        По умолчанию только ранкед — как и вся остальная статистика бота.
        """
        data = self._get(f"/players/{account_id}/counts", params=self._lobby(lobby_type)) or {}
        lane_role = data.get("lane_role") or {}
        result: dict[int, tuple[int, int]] = {}
        for key, stat in lane_role.items():
            try:
                lane = int(key)
            except (TypeError, ValueError):
                continue
            result[lane] = (stat.get("games", 0), stat.get("win", 0))
        return result

    @staticmethod
    def _lobby(lobby_type: Optional[int]) -> dict:
        return {} if lobby_type is None else {"lobby_type": lobby_type}

    def get_gpm_distribution(self, account_id: int, lobby_type: Optional[int] = RANKED_LOBBY) -> dict:
        """Медиана и пик GPM из гистограммы /players/{id}/histograms/gold_per_min (по умолчанию ранкед)."""
        data = self._get(f"/players/{account_id}/histograms/gold_per_min", params=self._lobby(lobby_type))
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
        "net_worth": "net_worth", "level": "level", "leaver_status": "leaver_status",
    }

    def get_match(self, match_id: int) -> dict:
        """Полный матч /matches/{id} с короткоживущим кэшем в памяти.

        Одновременные запросы одного матча (участники пати обновляются параллельно) ждут
        первый ответ, а не идут в сеть каждый сам.
        """
        with self._lock:
            hit = self._match_cache.get(match_id)
            if hit and time.monotonic() - hit[0] < self.MATCH_CACHE_TTL:
                return hit[1]
            gate = self._match_locks.setdefault(match_id, threading.Lock())
        with gate:
            with self._lock:
                hit = self._match_cache.get(match_id)
                if hit and time.monotonic() - hit[0] < self.MATCH_CACHE_TTL:
                    return hit[1]
            try:
                match = self._get(f"/matches/{match_id}") or {}
            finally:
                with self._lock:
                    self._match_locks.pop(match_id, None)
            if match.get("players"):  # пустой ответ не кэшируем — матч мог ещё не доехать
                with self._lock:
                    self._match_cache[match_id] = (time.monotonic(), match)
                    self._match_cache.move_to_end(match_id)
                    while len(self._match_cache) > self.MATCH_CACHE_SIZE:
                        self._match_cache.popitem(last=False)
            return match

    def get_match_player_stats(
        self, match_id: int, account_id: int, player_slot: Optional[int] = None
    ) -> Optional[dict]:
        """Пер-матч статистика игрока из /matches/{id} + benchmarks (перцентиль vs тот же герой).

        GPM/урон/хил/нетворт и benchmarks приходят БЕЗ парса (из сводки Valve).
        Матч берётся из кэша: совместная игра пати — один запрос на всех её участников.
        """
        match = self.get_match(match_id)
        players = match.get("players") or []
        player = next((p for p in players if p.get("account_id") == account_id), None)
        if player is None and player_slot is not None:  # скрытый профиль: account_id в матче обнулён — ищем по слоту
            player = next((p for p in players if p.get("player_slot") == player_slot), None)
        if player is None:
            return None
        result = {out: player.get(src) for src, out in self._MATCH_FIELDS.items()}
        result["party_size"] = _party_size(players, player)
        benchmarks = {}
        for metric, value in (player.get("benchmarks") or {}).items():
            benchmarks[metric] = value.get("pct") if isinstance(value, dict) else value
        result["benchmarks"] = benchmarks
        return result

    def get_totals(self, account_id: int, lobby_type: Optional[int] = RANKED_LOBBY) -> dict:
        """Средние GPM/XPM/last hits из /players/{id}/totals (sum/n по полям), по умолчанию ранкед.

        Без фильтра OpenDota усредняет по всем режимам (обычные игры, турбо) — цифры не сходились
        с ранкед-статистикой бота.
        """
        data = self._get(f"/players/{account_id}/totals", params=self._lobby(lobby_type))
        wanted = {"gold_per_min": "gpm", "xp_per_min": "xpm", "last_hits": "last_hits"}
        result: dict = {"gpm": None, "xpm": None, "last_hits": None}
        if isinstance(data, list):
            for row in data:
                key = wanted.get(row.get("field"))
                if key and row.get("n"):
                    result[key] = row["sum"] / row["n"]
        return result
