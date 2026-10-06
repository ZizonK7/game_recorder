import csv

from lolrec import analysis
from lolrec.config import Settings, load_settings, save_settings
from lolrec.exporter import export_games, flatten
from lolrec.storage import Storage

from .fakes import ME, make_match, make_timeline


def test_summarize_and_overview():
    m1, m2 = make_match("KR_1", win=True, seed=1), make_match("KR_2", "Zed", win=False, seed=2)
    s1, s2 = analysis.summarize(m1, ME), analysis.summarize(m2, ME)
    assert s1.champion == "Ahri" and s1.win and s1.duration_min == 30
    assert s1.cs == m1["info"]["participants"][0]["totalMinionsKilled"] + m1["info"]["participants"][0]["neutralMinionsKilled"]
    assert 0 <= s1.kill_participation <= 1
    ov = analysis.overview([s1, s2])
    assert ov.games == 2 and ov.wins == 1 and ov.win_rate == 0.5
    champs = analysis.champion_stats([s1, s2])
    assert {c.champion for c in champs} == {"Ahri", "Zed"}
    # riot id 로도 찾기
    assert analysis.find_me(m1, "", "나#KR1")["participantId"] == 1
    assert analysis.summarize(m1, "nobody") is None


def test_lane_gold_diff():
    data = analysis.lane_gold_diff(make_match(), make_timeline(minutes=10), ME)
    assert len(data) == 11
    assert data[0] == (0, 0) and data[10][1] == 10 * 20  # 400 vs 380 골드/분


def test_old_duration_in_ms():
    m = make_match()
    del m["info"]["gameEndTimestamp"]
    m["info"]["gameDuration"] = 1_500_000
    assert analysis.game_duration_sec(m) == 1500


def test_flatten():
    assert flatten({"a": {"b": 1}, "c": [1, 2]}) == {"a.b": 1, "c": "[1, 2]"}


def test_storage_and_export(tmp_path):
    st = Storage(tmp_path / "db.sqlite", tmp_path / "data")
    gid = st.create_game(match_id="KR_1", platform="KR", puuid=ME, started_at="2026-10-06T21:00:00",
                         status="none", api_status="pending")
    st.save_raw("KR_1", make_match(), make_timeline(minutes=5))
    st.update_game(gid, api_status="done", kills=3, deaths=1, assists=4, win=1)
    g = st.get_game(gid)
    assert g.kda_text == "3/1/4" and not g.has_video
    assert st.find_by_match_id("KR_1").id == gid
    assert st.pending_api_games() == []

    counts = export_games(st, [g], tmp_path / "out", ME)
    assert counts["participants.csv"] == 10
    assert counts["frames.csv"] == 6 * 10
    assert counts["events.csv"] == 3
    with (tmp_path / "out" / "participants.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert sum(r["isMe"] == "True" for r in rows) == 1
    assert "challenges.kda" in rows[0]
    assert (tmp_path / "out" / "raw" / "matches" / "KR_1.json").exists()


def test_settings_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    s = Settings(resolution="1080p", ranked_only=True)
    save_settings(s, p)
    loaded = load_settings(p)
    assert loaded.resolution == "1080p" and loaded.ranked_only
    assert loaded.should_record_queue(420) and not loaded.should_record_queue(450)
    assert loaded.should_record_queue(None)
    assert Settings().should_record_queue(450)
    p.write_text('{"unknown_key": 1, "fps": 60}')
    assert load_settings(p).fps == 60
