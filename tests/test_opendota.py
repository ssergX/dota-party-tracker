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


def test_concurrent_calls_are_serialized_by_lock():
    """Один общий клиент из нескольких потоков не должен делать запросы одновременно."""
    import threading
    import time

    class ConcurrencyProbe:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self._lock = threading.Lock()

        def get(self, url, params=None, timeout=None):
            with self._lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            with self._lock:
                self.active -= 1
            return FakeResp({"rank_tier": 11})

    probe = ConcurrencyProbe()
    od = OpenDota(session=probe, min_interval=0)
    threads = [threading.Thread(target=lambda: od.get_profile(1)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert probe.max_active == 1  # запросы не пересекались


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
