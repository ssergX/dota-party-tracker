from mmrbot.stratz import Stratz


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
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self.payloads.pop(0)
        return item if isinstance(item, FakeResp) else FakeResp(item)


def _match(position="POSITION_1", imp=12, lobby="RANKED"):
    return {
        "lobbyType": lobby,
        "players": [{
            "position": position, "role": "CORE", "lane": "SAFE_LANE", "imp": imp,
            "goldPerMinute": 600, "experiencePerMinute": 700, "networth": 30000,
            "heroDamage": 25000, "towerDamage": 3000, "heroHealing": 0,
            "numLastHits": 400, "numDenies": 20, "level": 25,
        }],
    }


def test_get_matches_maps_fields_and_skips_missing():
    session = FakeSession([{"data": {"m0": _match(), "m1": None, "m2": _match("POSITION_5", -4)}}])
    st = Stratz("key", session=session, min_interval=0)
    result = st.get_matches(42, [100, 101, 102])
    assert set(result) == {100, 102}
    assert result[100]["position"] == 1 and result[100]["role"] == "CORE"
    assert result[100]["lane"] == "SAFE_LANE" and result[100]["imp"] == 12
    assert result[100]["gpm"] == 600 and result[100]["net_worth"] == 30000 and result[100]["last_hits"] == 400
    assert result[102]["position"] == 5 and result[102]["imp"] == -4


def test_query_uses_aliases_ids_and_headers():
    session = FakeSession([{"data": {"m0": None, "m1": None}}])
    Stratz("secret", session=session, min_interval=0).get_matches(42, [100, 101])
    call = session.calls[0]
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["headers"]["User-Agent"] == "STRATZ_API"
    assert call["json"]["variables"] == {}
    assert "$acc" not in call["json"]["query"]  # неиспользуемая переменная — 400 у Stratz
    assert "m0: match(id: 100)" in call["json"]["query"] and "m1: match(id: 101)" in call["json"]["query"]


def test_chunks_large_batches():
    payloads = [{"data": {f"m{i}": None for i in range(10)}}, {"data": {f"m{i}": None for i in range(3)}}]
    session = FakeSession(payloads)
    Stratz("k", session=session, min_interval=0, chunk=10).get_matches(1, list(range(13)))
    assert len(session.calls) == 2


def test_unknown_position_is_none_and_empty_ids_make_no_request():
    session = FakeSession([{"data": {"m0": _match(position=None)}}])
    st = Stratz("k", session=session, min_interval=0)
    assert st.get_matches(1, [5])[5]["position"] is None
    assert st.get_matches(1, []) == {}
    assert len(session.calls) == 1


def test_retries_on_429_then_succeeds():
    session = FakeSession([FakeResp({}, 429), {"data": {"m0": _match()}}])
    st = Stratz("k", session=session, min_interval=0, retry_sleep=0)
    assert 5 in st.get_matches(1, [5])
    assert len(session.calls) == 2


def test_graphql_errors_raise():
    session = FakeSession([{"errors": [{"message": "boom"}]}])
    st = Stratz("k", session=session, min_interval=0)
    try:
        st.get_matches(1, [5])
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("ожидали RuntimeError")


def _full_player(acc, radiant, hero, pos="POSITION_1", name="Ник"):
    return {"steamAccountId": acc, "isRadiant": radiant, "heroId": hero, "position": pos, "role": "CORE",
            "lane": "SAFE_LANE", "kills": 5, "deaths": 2, "assists": 7, "imp": 10,
            "goldPerMinute": 600, "experiencePerMinute": 700, "networth": 30000, "heroDamage": 25000,
            "towerDamage": 3000, "heroHealing": 0, "numLastHits": 400, "numDenies": 20, "level": 25,
            "steamAccount": {"name": name}}


def test_get_match_returns_full_match():
    payload = {"data": {"match": {
        "id": 77, "lobbyType": "RANKED", "durationSeconds": 2000, "didRadiantWin": True, "startDateTime": 1700000000,
        "players": [_full_player(1, True, 2, name="Вася"), _full_player(2, False, 5, "POSITION_5", None)],
    }}}
    session = FakeSession([payload])
    match = Stratz("k", session=session, min_interval=0).get_match(77)
    assert match["match_id"] == 77 and match["radiant_win"] is True
    assert match["duration"] == 2000 and match["start_time"] == 1700000000
    first, second = match["players"]
    assert first["account_id"] == 1 and first["name"] == "Вася" and first["is_radiant"] is True
    assert first["position"] == 1 and first["hero_id"] == 2 and first["imp"] == 10
    assert first["kills"] == 5 and first["net_worth"] == 30000
    assert second["position"] == 5 and second["name"] is None
    assert "match(id: 77)" in session.calls[0]["json"]["query"]


def test_get_match_missing_returns_none():
    session = FakeSession([{"data": {"match": None}}])
    assert Stratz("k", session=session, min_interval=0).get_match(1) is None


def _party_match(me_party, others):
    """Матч с 10 игроками: наш (acc 42, команда radiant) + остальные (acc, isRadiant, partyId)."""
    row = lambda acc, rad, party: {"steamAccountId": acc, "isRadiant": rad, "partyId": party,
                                   "position": "POSITION_1", "imp": 1}
    return {"players": [row(42, True, me_party)] + [row(a, r, p) for a, r, p in others]}


def test_party_size_counts_teammates_with_same_party_id():
    match = _party_match(7, [(1, True, 7), (2, True, 7), (3, True, 9), (4, False, 7)])  # враг с тем же id — не в счёт
    session = FakeSession([{"data": {"m0": match}}])
    info = Stratz("k", session=session, min_interval=0).get_matches(42, [5])[5]
    assert info["party_size"] == 3


def test_party_size_is_one_when_no_party_id():
    session = FakeSession([{"data": {"m0": _party_match(None, [(1, True, None), (2, False, None)])}}])
    assert Stratz("k", session=session, min_interval=0).get_matches(42, [5])[5]["party_size"] == 1
