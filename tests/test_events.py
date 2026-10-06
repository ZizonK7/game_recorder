from lolrec import events as ev
from lolrec.analysis import participant_maps

from .fakes import PLAYERS, make_match, make_timeline


def who():
    return ev.PlayerIdentity("나#KR1", PLAYERS)


def test_identity_matches_full_and_short_names():
    w = who()
    assert w.is_me("나#KR1") and w.is_me("나") and w.is_me(" 나 ")
    assert not w.is_me("p1")
    assert w.my_team == "ORDER"
    assert w.is_ally("p1") is True
    assert w.is_ally("p7") is False
    assert w.is_ally("unknown") is None


def test_champion_kill_classification():
    w = who()
    death = ev.from_live_event({"EventName": "ChampionKill", "EventTime": 100.5, "KillerName": "p7",
                                "VictimName": "나", "Assisters": []}, w)
    assert death.type == "death" and "Darius" in death.label and death.game_time == 100.5
    kill = ev.from_live_event({"EventName": "ChampionKill", "EventTime": 1, "KillerName": "나#KR1",
                               "VictimName": "p8", "Assisters": []}, w)
    assert kill.type == "kill"
    assist = ev.from_live_event({"EventName": "ChampionKill", "EventTime": 1, "KillerName": "p1",
                                 "VictimName": "p8", "Assisters": ["나"]}, w)
    assert assist.type == "assist"
    other = ev.from_live_event({"EventName": "ChampionKill", "EventTime": 1, "KillerName": "p7",
                                "VictimName": "p1", "Assisters": []}, w)
    assert other.type == "enemy_kill"


def test_objectives_and_ignored_events():
    w = who()
    d = ev.from_live_event({"EventName": "DragonKill", "EventTime": 5, "KillerName": "p1",
                            "DragonType": "Fire", "Stolen": "False"}, w)
    assert d.type == "dragon" and d.label.startswith("아군") and "화염" in d.label
    t = ev.from_live_event({"EventName": "TurretKilled", "EventTime": 5, "KillerName": "p7",
                            "TurretKilled": "Turret_T1_L_03_A"}, w)
    assert t.type == "turret" and "아군 구조물" in t.label
    m = ev.from_live_event({"EventName": "Multikill", "EventTime": 5, "KillerName": "나", "KillStreak": 3}, w)
    assert m.type == "multikill" and "트리플킬" in m.label
    assert ev.from_live_event({"EventName": "MinionsSpawning", "EventTime": 65}, w) is None
    end = ev.from_live_event({"EventName": "GameEnd", "EventTime": 1800, "Result": "Win"}, w)
    assert end.type == "game_end" and "승리" in end.label


def test_timeline_and_merge():
    match, tl = make_match(), make_timeline()
    champs, teams = participant_maps(match)
    tl_events = ev.from_timeline(tl, 1, champs, teams[1])
    types = [e.type for e in tl_events]
    assert types.count("kill") == 1 and "ward" in types and "item" in types and "level" in types
    dragon = next(e for e in tl_events if e.type == "dragon")
    assert dragon.label.startswith("아군") and dragon.game_time == 600
    tower = next(e for e in tl_events if e.type == "turret")
    assert "적 구조물" in tower.label

    live = [ev.GameEvent("kill", 189.0, "킬: Caitlyn")]
    merged = ev.merge_timeline_events(live, tl_events)
    assert sum(1 for e in merged if e.type == "kill") == 1  # 중복 제거
    assert merged[0].source == "live" or merged[0].game_time <= merged[-1].game_time
    assert [e.game_time for e in merged] == sorted(e.game_time for e in merged)


def test_save_load_roundtrip(tmp_path):
    events = [ev.GameEvent("death", 100, "사망", data={"KillerName": "x"})]
    ev.apply_offset(events, 12.5)
    assert events[0].video_time == 112.5
    p = tmp_path / "events.json"
    ev.save_events(p, events, 12.5, {"me": "나"})
    loaded, offset, meta = ev.load_events(p)
    assert offset == 12.5 and meta["me"] == "나" and loaded[0].video_time == 112.5
    assert ev.load_events(tmp_path / "none.json") == ([], 0.0, {})


def test_colors_are_valid_qt_hex():
    for _, color in ev.EVENT_TYPES.values():
        assert color.startswith("#") and len(color) == 7
