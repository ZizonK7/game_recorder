"""테스트용 가짜 Match-v5 / timeline 데이터."""

from __future__ import annotations

import random

CHAMPS = ["Ahri", "LeeSin", "Garen", "Jinx", "Thresh", "Zed", "Vi", "Darius", "Caitlyn", "Lulu"]
POS = ["MIDDLE", "JUNGLE", "TOP", "BOTTOM", "UTILITY"]
ME = "puuid-me"


def make_match(match_id: str = "KR_1", my_champ: str = "Ahri", win: bool = True, seed: int = 0,
               queue: int = 420, duration: int = 1800, start_ms: int = 1_790_000_000_000) -> dict:
    rnd = random.Random(seed)
    parts = []
    for i in range(10):
        team = 100 if i < 5 else 200
        champ = my_champ if i == 0 else CHAMPS[i]
        parts.append({
            "participantId": i + 1, "puuid": ME if i == 0 else f"puuid-{i}", "teamId": team,
            "championName": champ, "riotIdGameName": "나" if i == 0 else f"p{i}", "riotIdTagline": "KR1",
            "teamPosition": POS[i % 5], "win": (team == 100) == win,
            "kills": rnd.randint(0, 12), "deaths": rnd.randint(0, 10), "assists": rnd.randint(0, 15),
            "totalMinionsKilled": rnd.randint(100, 250), "neutralMinionsKilled": rnd.randint(0, 30),
            "goldEarned": rnd.randint(8000, 16000), "totalDamageDealtToChampions": rnd.randint(8000, 40000),
            "visionScore": rnd.randint(10, 60), "challenges": {"kda": 3.2, "killParticipation": 0.5},
            "perks": {"styles": [{"style": 8100}]},
        })
    return {
        "metadata": {"matchId": match_id, "participants": [p["puuid"] for p in parts]},
        "info": {
            "gameId": int(match_id.split("_")[1]), "queueId": queue, "gameDuration": duration,
            "gameStartTimestamp": start_ms, "gameEndTimestamp": start_ms + duration * 1000,
            "gameVersion": "16.19.1", "participants": parts,
            "teams": [{"teamId": 100, "win": win, "objectives": {"baron": {"kills": 1}}},
                      {"teamId": 200, "win": not win, "objectives": {"baron": {"kills": 0}}}],
        },
    }


def make_timeline(match_id: str = "KR_1", minutes: int = 30) -> dict:
    frames = []
    for m in range(minutes + 1):
        pf = {str(i): {"participantId": i, "totalGold": 500 + m * (400 if i == 1 else 380), "level": min(18, 1 + m // 2),
                       "minionsKilled": m * 7, "position": {"x": 100 * i, "y": 200}} for i in range(1, 11)}
        events = []
        if m == 3:
            events = [
                {"type": "WARD_PLACED", "timestamp": 185_000, "creatorId": 1, "wardType": "YELLOW_TRINKET"},
                {"type": "CHAMPION_KILL", "timestamp": 190_000, "killerId": 1, "victimId": 8,
                 "assistingParticipantIds": [2]},
                {"type": "ITEM_PURCHASED", "timestamp": 191_000, "participantId": 1, "itemId": 1056},
            ]
        if m == 10:
            events = [
                {"type": "ELITE_MONSTER_KILL", "timestamp": 600_000, "killerId": 2, "killerTeamId": 100,
                 "monsterType": "DRAGON", "monsterSubType": "FIRE_DRAGON"},
                {"type": "LEVEL_UP", "timestamp": 601_000, "participantId": 1, "level": 6},
                {"type": "BUILDING_KILL", "timestamp": 640_000, "killerId": 3, "teamId": 200,
                 "buildingType": "TOWER_BUILDING"},
            ]
        frames.append({"timestamp": m * 60_000, "participantFrames": pf, "events": events})
    return {"metadata": {"matchId": match_id}, "info": {"frameInterval": 60000, "frames": frames}}


PLAYERS = [
    {"riotId": "나#KR1", "riotIdGameName": "나", "summonerName": "나", "team": "ORDER", "championName": "Ahri"},
    {"riotId": "p1#KR1", "riotIdGameName": "p1", "summonerName": "p1", "team": "ORDER", "championName": "LeeSin"},
    {"riotId": "p7#KR1", "riotIdGameName": "p7", "summonerName": "p7", "team": "CHAOS", "championName": "Darius"},
    {"riotId": "p8#KR1", "riotIdGameName": "p8", "summonerName": "p8", "team": "CHAOS", "championName": "Caitlyn"},
]
