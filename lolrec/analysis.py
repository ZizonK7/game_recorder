"""Match-v5 데이터에서 대시보드용 지표를 계산."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

QUEUE_NAMES = {
    420: "솔로랭크", 440: "자유랭크", 400: "일반(드래프트)", 430: "일반(블라인드)", 490: "빠른 대전",
    450: "칼바람", 1700: "아레나", 1900: "URF", 900: "URF", 0: "사용자 설정",
}
POSITION_KO = {"TOP": "탑", "JUNGLE": "정글", "MIDDLE": "미드", "BOTTOM": "원딜", "UTILITY": "서폿", "": "-"}


def queue_name(queue_id: int | None) -> str:
    if queue_id is None:
        return "-"
    return QUEUE_NAMES.get(queue_id, f"큐 {queue_id}")


def find_me(match: dict, puuid: str = "", riot_id: str = "") -> dict | None:
    parts = (match.get("info") or {}).get("participants") or []
    if puuid:
        for p in parts:
            if p.get("puuid") == puuid:
                return p
    if riot_id:
        name = riot_id.split("#")[0].casefold()
        for p in parts:
            if (p.get("riotIdGameName") or p.get("summonerName") or "").casefold() == name:
                return p
    return None


def game_duration_sec(match: dict) -> float:
    info = match.get("info") or {}
    d = float(info.get("gameDuration") or 0)
    # 2021년 10월 이전 경기는 gameDuration 이 밀리초 단위
    if "gameEndTimestamp" not in info:
        d /= 1000.0
    return d


@dataclass
class GameSummary:
    match_id: str
    queue_id: int | None
    champion: str
    position: str
    win: bool
    kills: int
    deaths: int
    assists: int
    cs: int
    gold: int
    damage: int
    vision: int
    duration_min: float
    kill_participation: float
    game_start: int  # epoch ms
    remake: bool = False

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(1, self.deaths)

    @property
    def cs_per_min(self) -> float:
        return self.cs / max(self.duration_min, 1e-6)

    @property
    def damage_per_min(self) -> float:
        return self.damage / max(self.duration_min, 1e-6)

    @property
    def gold_per_min(self) -> float:
        return self.gold / max(self.duration_min, 1e-6)


def summarize(match: dict, puuid: str = "", riot_id: str = "") -> GameSummary | None:
    me = find_me(match, puuid, riot_id)
    if me is None:
        return None
    info = match["info"]
    team_kills = sum(p.get("kills", 0) for p in info["participants"] if p.get("teamId") == me.get("teamId"))
    k, d, a = me.get("kills", 0), me.get("deaths", 0), me.get("assists", 0)
    return GameSummary(
        match_id=(match.get("metadata") or {}).get("matchId", ""),
        queue_id=info.get("queueId"),
        champion=me.get("championName", ""),
        position=me.get("teamPosition") or me.get("individualPosition") or "",
        win=bool(me.get("win")),
        kills=k, deaths=d, assists=a,
        cs=me.get("totalMinionsKilled", 0) + me.get("neutralMinionsKilled", 0),
        gold=me.get("goldEarned", 0),
        damage=me.get("totalDamageDealtToChampions", 0),
        vision=me.get("visionScore", 0),
        duration_min=game_duration_sec(match) / 60.0,
        kill_participation=(k + a) / team_kills if team_kills else 0.0,
        game_start=info.get("gameStartTimestamp") or info.get("gameCreation") or 0,
        remake=bool(me.get("gameEndedInEarlySurrender")),
    )


@dataclass
class ChampionStat:
    champion: str
    games: int = 0
    wins: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    cs_per_min: list[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    @property
    def kda(self) -> float:
        return (self.kills + self.assists) / max(1, self.deaths)


@dataclass
class Overview:
    games: int
    wins: int
    avg_kda: float
    avg_kills: float
    avg_deaths: float
    avg_assists: float
    avg_cs_per_min: float
    avg_damage_per_min: float
    avg_gold_per_min: float
    avg_vision: float
    avg_kp: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0


def overview(summaries: list[GameSummary]) -> Overview:
    games = [g for g in summaries if not g.remake]
    n = len(games) or 1

    def avg(f):
        return sum(f(g) for g in games) / n

    deaths = sum(g.deaths for g in games)
    return Overview(
        games=len(games),
        wins=sum(g.win for g in games),
        avg_kda=(sum(g.kills + g.assists for g in games)) / max(1, deaths),
        avg_kills=avg(lambda g: g.kills),
        avg_deaths=avg(lambda g: g.deaths),
        avg_assists=avg(lambda g: g.assists),
        avg_cs_per_min=avg(lambda g: g.cs_per_min),
        avg_damage_per_min=avg(lambda g: g.damage_per_min),
        avg_gold_per_min=avg(lambda g: g.gold_per_min),
        avg_vision=avg(lambda g: g.vision),
        avg_kp=avg(lambda g: g.kill_participation),
    )


def champion_stats(summaries: list[GameSummary]) -> list[ChampionStat]:
    stats: dict[str, ChampionStat] = defaultdict(lambda: ChampionStat(""))
    for g in summaries:
        if g.remake:
            continue
        s = stats[g.champion]
        s.champion = g.champion
        s.games += 1
        s.wins += int(g.win)
        s.kills += g.kills
        s.deaths += g.deaths
        s.assists += g.assists
        s.cs_per_min.append(g.cs_per_min)
    return sorted(stats.values(), key=lambda s: s.games, reverse=True)


def lane_gold_diff(match: dict, timeline: dict, puuid: str = "", riot_id: str = "") -> list[tuple[int, int]]:
    """분 단위 (분, 내 골드 - 상대 라이너 골드)."""
    me = find_me(match, puuid, riot_id)
    if me is None:
        return []
    pos = me.get("teamPosition")
    opp = next(
        (p for p in match["info"]["participants"]
         if pos and p.get("teamPosition") == pos and p.get("teamId") != me.get("teamId")),
        None,
    )
    if opp is None:
        return []
    my_id, opp_id = str(me["participantId"]), str(opp["participantId"])
    out = []
    for i, frame in enumerate((timeline.get("info") or {}).get("frames") or []):
        pf = frame.get("participantFrames") or {}
        if my_id in pf and opp_id in pf:
            out.append((i, pf[my_id].get("totalGold", 0) - pf[opp_id].get("totalGold", 0)))
    return out


def participant_maps(match: dict) -> tuple[dict[int, str], dict[int, int]]:
    """participantId -> 챔피언, participantId -> teamId."""
    champs, teams = {}, {}
    for p in (match.get("info") or {}).get("participants") or []:
        champs[p["participantId"]] = p.get("championName", "")
        teams[p["participantId"]] = p.get("teamId")
    return champs, teams
