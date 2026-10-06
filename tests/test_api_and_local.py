import pytest

from lolrec.riot import api
from lolrec.riot.local import parse_cmdline, parse_lockfile


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_rate_limiter_respects_short_and_long_windows():
    clock = FakeClock()
    rl = api.RateLimiter([(20, 1.0), (100, 120.0)], clock=clock, sleep=clock.sleep)
    for _ in range(20):
        rl.acquire()
    assert clock.t == 0.0
    rl.acquire()  # 21번째는 1초 윈도우를 기다려야 함
    assert clock.t >= 1.0
    for _ in range(79):
        rl.acquire()
    t_at_100 = clock.t
    rl.acquire()  # 101번째는 120초 윈도우
    assert clock.t >= 120.0 > t_at_100


def test_rate_limiter_block():
    clock = FakeClock()
    rl = api.RateLimiter([(20, 1.0)], clock=clock, sleep=clock.sleep)
    rl.block_for(7)
    rl.acquire()
    assert clock.t >= 7


@pytest.mark.parametrize("value,expected", [("KR", "KR"), ("kr", "KR"), ("EUW", "EUW1"), ("NA", "NA1"),
                                            (None, "KR"), ("JP1", "JP1"), ("VN", "VN2")])
def test_normalize_platform(value, expected):
    assert api.normalize_platform(value) == expected


def test_routing_and_match_id():
    assert api.routing_for("KR") == "asia"
    assert api.routing_for("EUW1") == "europe"
    assert api.routing_for("VN2") == "sea"
    assert api.ACCOUNT_ROUTING[api.routing_for("VN2")] == "asia"
    assert api.match_id_for("kr", 7123456789) == "KR_7123456789"


def test_parse_lockfile_and_cmdline():
    c = parse_lockfile("LeagueClient:1234:56789:secretPW_-x:https")
    assert c.port == 56789 and c.password == "secretPW_-x"
    assert c.auth_header.startswith("Basic ")
    assert parse_lockfile("garbage") is None
    c2 = parse_cmdline(["LeagueClientUx.exe", "--app-port=50000", "--remoting-auth-token=abc-DEF_1"])
    assert c2.port == 50000 and c2.password == "abc-DEF_1"
    assert parse_cmdline(["x"]) is None
