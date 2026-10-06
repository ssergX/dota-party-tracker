from mmrbot.opendota import OpenDota


class FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.post_calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        return FakeResp(self.payload)

    def post(self, url, timeout=None):
        self.post_calls.append({"url": url})
        return FakeResp({})


def test_get_profile_extracts_fields():
    session = FakeSession({"profile": {"personaname": "Вася"}, "rank_tier": 75, "leaderboard_rank": None})
    od = OpenDota(session=session, min_interval=0)
    profile = od.get_profile(42)
    assert profile["rank_tier"] == 75
    assert profile["leaderboard_rank"] is None
    assert profile["personaname"] == "Вася"
    assert session.calls[0]["url"].endswith("/players/42")


def test_get_matches_returns_list():
    session = FakeSession([{"match_id": 1, "lobby_type": 7}, {"match_id": 2, "lobby_type": 0}])
    od = OpenDota(session=session, min_interval=0)
    matches = od.get_matches(42)
    assert [m["match_id"] for m in matches] == [1, 2]
    assert session.calls[0]["url"].endswith("/players/42/matches")


def test_api_key_added_to_params():
    session = FakeSession({"rank_tier": 11})
    od = OpenDota(session=session, min_interval=0, api_key="SECRET")
    od.get_profile(42)
    assert session.calls[0]["params"].get("api_key") == "SECRET"


def test_no_api_key_means_no_param():
    session = FakeSession({"rank_tier": 11})
    od = OpenDota(session=session, min_interval=0)
    od.get_profile(42)
    assert "api_key" not in session.calls[0]["params"]


def test_get_match_player_stats_extracts_fields_and_benchmarks():
    match = {"players": [
        {"account_id": 42, "gold_per_min": 500, "xp_per_min": 600, "last_hits": 180, "denies": 10,
         "hero_damage": 25000, "tower_damage": 3000, "hero_healing": 0, "net_worth": 18000, "level": 25,
         "benchmarks": {"gold_per_min": {"raw": 500, "pct": 0.8}, "hero_healing_per_min": {"raw": 0, "pct": 0.1}}},
        {"account_id": 99, "gold_per_min": 300},
    ]}
    od = OpenDota(session=FakeSession(match), min_interval=0)
    s = od.get_match_player_stats(123, 42)
    assert s["gpm"] == 500 and s["net_worth"] == 18000 and s["hero_damage"] == 25000
    assert s["benchmarks"]["gold_per_min"] == 0.8
    assert s["benchmarks"]["hero_healing_per_min"] == 0.1


def test_get_match_player_stats_missing_player_returns_none():
    od = OpenDota(session=FakeSession({"players": []}), min_interval=0)
    assert od.get_match_player_stats(123, 42) is None


def test_refresh_posts_to_endpoint():
    session = FakeSession({})
    od = OpenDota(session=session, min_interval=0)
    assert od.refresh(42) is True
    assert session.post_calls[0]["url"].endswith("/players/42/refresh")


def test_refresh_swallows_errors():
    class BrokenSession(FakeSession):
        def post(self, url, timeout=None):
            raise RuntimeError("network down")

    od = OpenDota(session=BrokenSession({}), min_interval=0)
    assert od.refresh(42) is False  # не бросает, возвращает False


def test_get_lanes_normalizes_lane_role():
    session = FakeSession({"lane_role": {"1": {"games": 10, "win": 6}, "2": {"games": 4, "win": 1}}})
    od = OpenDota(session=session, min_interval=0)
    lanes = od.get_lanes(42)
    assert lanes[1] == (10, 6)
    assert lanes[2] == (4, 1)
    assert session.calls[0]["url"].endswith("/players/42/counts")


def test_get_lanes_empty_when_absent():
    od = OpenDota(session=FakeSession({}), min_interval=0)
    assert od.get_lanes(42) == {}


def test_get_gpm_distribution_median_and_best():
    session = FakeSession([
        {"x": 0, "games": 1, "win": 0},
        {"x": 100, "games": 2, "win": 1},
        {"x": 200, "games": 1, "win": 1},
    ])
    od = OpenDota(session=session, min_interval=0)
    dist = od.get_gpm_distribution(42)
    assert dist["median"] == 100  # 4 игры, медиана падает в бакет 100
    assert dist["best"] == 200
    assert session.calls[0]["url"].endswith("/players/42/histograms/gold_per_min")


def test_get_gpm_distribution_empty():
    od = OpenDota(session=FakeSession([]), min_interval=0)
    assert od.get_gpm_distribution(42) == {"median": None, "best": None}


def test_get_totals_computes_averages():
    session = FakeSession([
        {"field": "gold_per_min", "n": 10, "sum": 5000},   # avg 500
        {"field": "xp_per_min", "n": 10, "sum": 6000},      # avg 600
        {"field": "last_hits", "n": 10, "sum": 1800},       # avg 180
        {"field": "kills", "n": 10, "sum": 100},            # игнор
    ])
    od = OpenDota(session=session, min_interval=0)
    totals = od.get_totals(42)
    assert totals["gpm"] == 500
    assert totals["xpm"] == 600
    assert totals["last_hits"] == 180
    assert session.calls[0]["url"].endswith("/players/42/totals")


def test_get_totals_handles_missing_fields():
    session = FakeSession([])
    od = OpenDota(session=session, min_interval=0)
    totals = od.get_totals(42)
    assert totals == {"gpm": None, "xpm": None, "last_hits": None}


def test_concurrent_calls_respect_throttle_but_overlap():
    """Потоки не стоят в очереди целиком (запросы перекрываются), но старты разнесены троттлингом."""
    import threading
    import time

    starts = []

    class Probe:
        def get(self, url, params=None, timeout=None):
            starts.append(time.monotonic())
            time.sleep(0.05)
            return FakeResp({"rank_tier": 11})

    od = OpenDota(session=Probe(), min_interval=0.03)
    threads = [threading.Thread(target=lambda: od.get_profile(1)) for _ in range(5)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    starts.sort()
    assert all(b - a >= 0.025 for a, b in zip(starts, starts[1:]))  # частота запросов в лимите
    assert elapsed < 5 * 0.05  # быстрее полностью последовательного выполнения


class PagedSession(FakeSession):
    """Отдаёт матчи страницами по offset/limit, как OpenDota."""

    def __init__(self, total):
        super().__init__([])
        self.total = total

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        offset = (params or {}).get("offset", 0)
        limit = (params or {}).get("limit", self.total)
        return FakeResp([{"match_id": i} for i in range(offset, min(offset + limit, self.total))])


def test_get_matches_ranked_filter_and_limit():
    session = FakeSession([])
    od = OpenDota(session=session, min_interval=0)
    od.get_matches(42)
    assert session.calls[0]["params"] == {"significant": 0, "limit": 200, "lobby_type": 7}


def test_get_matches_full_history_is_paged():
    session = PagedSession(2500)
    od = OpenDota(session=session, min_interval=0)
    od.HISTORY_PAGE = 1000
    result = od.get_matches(42, limit=None)
    assert len(result) == 2500 and result[0]["match_id"] == 0 and result[-1]["match_id"] == 2499
    assert [c["params"]["offset"] for c in session.calls] == [0, 1000, 2000]
    assert all(c["params"]["lobby_type"] == 7 for c in session.calls)


class SeqSession(FakeSession):
    """Отдаёт ответы по очереди (для проверки ретраев)."""

    def __init__(self, responses):
        super().__init__(None)
        self.responses = list(responses)

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        return self.responses.pop(0)


class RespWithHeaders(FakeResp):
    def __init__(self, payload, status_code=200, headers=None):
        super().__init__(payload, status_code)
        self.headers = headers or {}


class BadJsonResp(FakeResp):
    def json(self):
        raise ValueError("not json")


def _no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr("mmrbot.opendota.time.sleep", lambda s: sleeps.append(s))
    return sleeps


def test_retry_honors_retry_after_header(monkeypatch):
    sleeps = _no_sleep(monkeypatch)
    session = SeqSession([RespWithHeaders({}, 429, {"Retry-After": "7"}), FakeResp({"rank_tier": 5})])
    od = OpenDota(session=session, min_interval=0)
    assert od.get_profile(1)["rank_tier"] == 5
    assert sleeps == [7.0]


def test_no_sleep_after_last_failed_attempt(monkeypatch):
    sleeps = _no_sleep(monkeypatch)
    session = SeqSession([FakeResp({}, 503)] * 3)
    od = OpenDota(session=session, min_interval=0, max_retries=3)
    try:
        od.get_profile(1)
        assert False, "ожидали исключение"
    except RuntimeError:
        pass
    assert len(sleeps) == 2  # между попытками, но не после последней


def test_invalid_json_is_retried(monkeypatch):
    _no_sleep(monkeypatch)
    session = SeqSession([BadJsonResp({}), FakeResp({"rank_tier": 9})])
    od = OpenDota(session=session, min_interval=0)
    assert od.get_profile(1)["rank_tier"] == 9


# --- достоверность и скорость сбора ------------------------------------------

def test_aggregates_are_ranked_only():
    """totals/counts/histograms без фильтра считают все режимы — бот показывает только ранкед."""
    session = FakeSession([])
    od = OpenDota(session=session, min_interval=0)
    od.get_totals(42)
    od.get_lanes(42)
    od.get_gpm_distribution(42)
    assert [c["params"] for c in session.calls] == [{"lobby_type": 7}] * 3


def test_get_recent_matches_endpoint():
    session = FakeSession([{"match_id": 1, "gold_per_min": 500}])
    od = OpenDota(session=session, min_interval=0)
    assert od.get_recent_matches(42)[0]["gold_per_min"] == 500
    assert session.calls[0]["url"].endswith("/players/42/recentMatches")


def test_shared_match_is_fetched_once_for_all_party_members():
    match = {"players": [{"account_id": 1, "gold_per_min": 500}, {"account_id": 2, "gold_per_min": 300}]}
    session = FakeSession(match)
    od = OpenDota(session=session, min_interval=0)
    assert od.get_match_player_stats(900, 1)["gpm"] == 500
    assert od.get_match_player_stats(900, 2)["gpm"] == 300
    assert len(session.calls) == 1  # второй участник той же игры — из кэша


def test_empty_match_is_not_cached():
    session = FakeSession({})
    od = OpenDota(session=session, min_interval=0)
    od.get_match_player_stats(900, 1)
    od.get_match_player_stats(900, 1)
    assert len(session.calls) == 2  # матч мог ещё не доехать до OpenDota — спросим снова


def test_party_size_from_party_id_on_same_side():
    match = {"players": [
        {"account_id": 1, "player_slot": 0, "party_id": 7},
        {"account_id": 2, "player_slot": 1, "party_id": 7},
        {"account_id": 3, "player_slot": 2, "party_id": 9},
        {"account_id": 4, "player_slot": 128, "party_id": 7},   # другая сторона — не в счёт
        {"account_id": 5, "player_slot": 129, "party_size": 3, "party_id": 1},
    ]}
    od = OpenDota(session=FakeSession(match), min_interval=0)
    assert od.get_match_player_stats(1, 1)["party_size"] == 2
    assert od.get_match_player_stats(1, 3)["party_size"] == 1
    assert od.get_match_player_stats(1, 5)["party_size"] == 3   # готовое значение OpenDota важнее


def test_long_rate_limit_fails_fast_and_blocks_next_calls(monkeypatch):
    from mmrbot.opendota import RateLimited
    sleeps = _no_sleep(monkeypatch)
    session = SeqSession([RespWithHeaders({}, 429, {"Retry-After": "3600"}), FakeResp({"rank_tier": 5})])
    od = OpenDota(session=session, min_interval=0)
    for _ in range(2):
        try:
            od.get_profile(1)
            assert False, "ожидали RateLimited"
        except RateLimited:
            pass
    assert sleeps == []                # не висим час в команде пользователя
    assert len(session.calls) == 1     # второй вызов в сеть не ходил — пауза ещё идёт
    assert od.refresh(1) is False and session.post_calls == []


def test_rate_limit_block_expires(monkeypatch):
    from mmrbot.opendota import RateLimited
    _no_sleep(monkeypatch)
    session = SeqSession([RespWithHeaders({}, 429, {"Retry-After": "60"}), FakeResp({"rank_tier": 5})])
    od = OpenDota(session=session, min_interval=0)
    try:
        od.get_profile(1)
    except RateLimited:
        pass
    od._blocked_until = 0.0  # пауза истекла
    assert od.get_profile(1)["rank_tier"] == 5


def test_burst_lets_first_requests_go_without_waiting(monkeypatch):
    """Пачка в пределах burst уходит сразу, дальше — по одному в min_interval (в сумме лимит не превышен)."""
    sleeps = _no_sleep(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr("mmrbot.opendota.time.monotonic", lambda: clock[0])
    od = OpenDota(session=FakeSession({"rank_tier": 1}), min_interval=1.0, burst=3)
    for _ in range(5):
        od.get_profile(1)
    assert [round(s, 3) for s in sleeps] == [1.0, 2.0]    # 3 сразу, 4-й и 5-й — по расписанию
    clock[0] += 60                                         # простой — ведро снова полное
    sleeps.clear()
    od.get_profile(1)
    assert sleeps == []


def test_default_burst_keeps_strict_spacing(monkeypatch):
    sleeps = _no_sleep(monkeypatch)
    monkeypatch.setattr("mmrbot.opendota.time.monotonic", lambda: 1000.0)
    od = OpenDota(session=FakeSession({"rank_tier": 1}), min_interval=1.0)
    od.get_profile(1)
    od.get_profile(1)
    assert sleeps == [1.0]


def test_refresh_post_is_rate_limited_per_account():
    session = FakeSession({})
    od = OpenDota(session=session, min_interval=0)
    assert od.refresh(42) is True
    assert od.refresh(42) is False       # повторный пинок того же игрока — не шлём
    assert od.refresh(43) is True        # другой игрок — шлём
    assert len(session.post_calls) == 2


def test_failed_refresh_post_can_be_retried():
    class Flaky(FakeSession):
        def post(self, url, timeout=None):
            self.post_calls.append({"url": url})
            if len(self.post_calls) == 1:
                raise RuntimeError("network down")
            return FakeResp({})

    od = OpenDota(session=Flaky({}), min_interval=0)
    assert od.refresh(42) is False
    assert od.refresh(42) is True


def test_party_id_zero_means_unknown_not_a_group():
    """party_id = 0 у одиночек: пять одиночек на стороне не должны стать «пати из 5»."""
    match = {"players": [{"account_id": i, "player_slot": i - 1, "party_id": 0} for i in range(1, 6)]}
    od = OpenDota(session=FakeSession(match), min_interval=0)
    assert od.get_match_player_stats(2, 1)["party_size"] is None


def test_hidden_profile_found_by_player_slot():
    """У скрытого профиля account_id в матче обнулён — игрока находим по сохранённому слоту."""
    match = {"players": [
        {"account_id": None, "player_slot": 3, "gold_per_min": 555},
        {"account_id": 9, "player_slot": 130, "gold_per_min": 100},
    ]}
    od = OpenDota(session=FakeSession(match), min_interval=0)
    assert od.get_match_player_stats(3, 42) is None
    assert od.get_match_player_stats(3, 42, player_slot=3)["gpm"] == 555


def test_get_heroes_returns_list_or_empty():
    heroes = [{"id": 1, "localized_name": "Anti-Mage"}]
    assert OpenDota(session=FakeSession(heroes), min_interval=0).get_heroes() == heroes
    assert OpenDota(session=FakeSession({"oops": 1}), min_interval=0).get_heroes() == []
