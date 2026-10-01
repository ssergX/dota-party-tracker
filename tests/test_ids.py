import pytest

from mmrbot.ids import parse_account_id

STEAMID64_BASE = 76561197960265728


def test_dotabuff_url():
    assert parse_account_id("https://www.dotabuff.com/players/123456789") == 123456789


def test_opendota_url():
    assert parse_account_id("https://www.opendota.com/players/123456789") == 123456789


def test_stratz_url():
    assert parse_account_id("https://stratz.com/players/123456789") == 123456789


def test_url_with_trailing_path_and_query():
    assert parse_account_id("https://www.dotabuff.com/players/123456789/matches?enhance=overview") == 123456789


def test_raw_account_id():
    assert parse_account_id("123456789") == 123456789


def test_raw_account_id_with_whitespace():
    assert parse_account_id("  123456789  ") == 123456789


def test_steamid64_converted_to_account_id():
    steamid64 = STEAMID64_BASE + 555
    assert parse_account_id(str(steamid64)) == 555


def test_steam_profile_url_converted():
    steamid64 = STEAMID64_BASE + 987654
    assert parse_account_id(f"https://steamcommunity.com/profiles/{steamid64}") == 987654


def test_vanity_steam_url_raises_helpful_error():
    with pytest.raises(ValueError) as exc:
        parse_account_id("https://steamcommunity.com/id/some_custom_name")
    assert "id/" in str(exc.value).lower() or "кастом" in str(exc.value).lower()


def test_garbage_raises():
    with pytest.raises(ValueError):
        parse_account_id("не ссылка и не число")


def test_empty_raises():
    with pytest.raises(ValueError):
        parse_account_id("   ")


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status


class _Session:
    def __init__(self, text, status=200):
        self.resp = _Resp(text, status)
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return self.resp


def test_vanity_extracts_name():
    from mmrbot.ids import extract_vanity
    assert extract_vanity("https://steamcommunity.com/id/some_name") == "some_name"
    assert extract_vanity("steamcommunity.com/id/some_name/") == "some_name"
    assert extract_vanity("https://steamcommunity.com/id/some_name/games?tab=all") == "some_name"
    assert extract_vanity("https://dotabuff.com/players/1") is None


def test_resolve_account_id_vanity_via_steam_xml():
    from mmrbot.ids import resolve_account_id
    session = _Session(f"<profile><steamID64>{STEAMID64_BASE + 4242}</steamID64></profile>")
    assert resolve_account_id("https://steamcommunity.com/id/some_name/", session=session) == 4242
    assert "/id/some_name/?xml=1" in session.urls[0]


def test_resolve_account_id_vanity_not_found():
    from mmrbot.ids import resolve_account_id
    session = _Session("<response><error>The specified profile could not be found.</error></response>")
    with pytest.raises(ValueError) as exc:
        resolve_account_id("https://steamcommunity.com/id/nobody", session=session)
    assert "не нашёл" in str(exc.value).lower()


def test_resolve_account_id_passthrough_without_network():
    from mmrbot.ids import resolve_account_id
    session = _Session("")
    assert resolve_account_id("https://www.dotabuff.com/players/123", session=session) == 123
    assert session.urls == []


def test_vanity_retries_transient_steam_failures():
    from mmrbot.ids import resolve_account_id

    class Flaky:
        def __init__(self):
            self.calls = 0

        def get(self, url, timeout=None):
            self.calls += 1
            if self.calls < 3:
                raise ConnectionError("reset")
            return _Resp(f"<profile><steamID64>{STEAMID64_BASE + 7}</steamID64></profile>")

    flaky = Flaky()
    assert resolve_account_id("https://steamcommunity.com/id/x", session=flaky) == 7
    assert flaky.calls == 3
