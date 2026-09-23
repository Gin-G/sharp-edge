"""Who is actually playing this week, and who inherited a role because someone is not.

**This is the data the projection has never had.** Every other module here
argues about *how much* a player produces; none of them knows *whether he
plays*, or whether the man he splits carries with is on injured reserve. The
projection is a season-long rate for a nominal starter — it reads last year's
usage, it has no snap share and no depth chart, and ``card.MIN_LINE`` exists
entirely to refuse bets where that blindness is doing the talking. Refusing was
the only option available. This module is the alternative: three nflverse feeds
that say, per week, who is out and who moved up.

**Three feeds, because no one of them is enough.**

``roster_weekly``  carries the NFL's own roster designation. ``RES`` is
    reserve/injured — this is where Jordan Mason sits, and it is why Aaron
    Jones is the Minnesota RB1 rather than half of a committee. ``I01`` on an
    otherwise-active player is the inactive designation for the upcoming game,
    and it is the single most decisive field in here: on settled weeks not one
    player carrying it recorded a stat. It is also the *earliest* hard signal —
    44 skill players carried it for week 3 on the Tuesday, before a single
    injury report had been filed.

``injuries``       is the official practice report: Out / Doubtful /
    Questionable plus practice participation. It is the feed everyone means by
    "the injury report", and it arrives **team by team through the week** — on
    a Tuesday it holds two clubs, by Friday all thirty-two. That trickle is the
    reason this is checked daily rather than once a week.

``depth_charts``   is timestamped and republished about twice a day. It gives a
    positional rank, which is the only direct read on role anywhere in the
    system, and it is what lets a ``role_conflict`` be adjudicated instead of
    merely flagged.

**The derived number that matters is not a flag, it is a share.** Knowing
Jordan Mason is out tells you nothing on its own. What moves a line is that his
carries are now somebody else's, so for every team and position this computes
``vacated_share`` — the fraction of that group's recent production belonging to
players who will not play. Minnesota's backfield has lost 44% of its rushing to
Mason's reserve/injured designation; Aaron Jones absorbs it, the market's line
says so, and our projection of Jones still describes the committee he was half
of. The gap between the two is a stale number, not an edge. That is the exact
shape of every role failure in ``card.py``'s docstring, and it now has a
magnitude attached.

**Within position, deliberately.** A receiver's vacated targets genuinely leak
to tight ends and backs, so the within-group share understates the effect. It
is the conservative direction and it keeps the number interpretable; a
cross-position redistribution would need a target-share model to be worth more
than a guess.

Nothing here is ever allowed to take the board down. Every fetch is guarded and
an empty result means the board behaves exactly as it did before this module
existed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

NFLVERSE_ROSTER = (
    "https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/"
    "roster_weekly_{season}.parquet"
)
NFLVERSE_INJURIES = (
    "https://github.com/nflverse/nflverse-data/releases/download/injuries/"
    "injuries_{season}.parquet"
)
NFLVERSE_DEPTH = (
    "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/"
    "depth_charts_{season}.parquet"
)
NFLVERSE_WEEKLY = (
    "https://github.com/nflverse/nflverse-data/releases/download/stats_player/"
    "stats_player_week_{season}.parquet"
)

OUT = "OUT"
DOUBTFUL = "DOUBTFUL"
QUESTIONABLE = "QUESTIONABLE"
ACTIVE = "ACTIVE"

#: Statuses a prop may be bet on. Doubtful is excluded with Out rather than
#: with Questionable because the two are not the same kind of uncertainty: a
#: questionable player is usually playing and the market has priced the chance
#: he does not, while a doubtful one is usually inactive and the prop is a coin
#: flip on whether the bet exists at all.
PLAYABLE = frozenset({ACTIVE, QUESTIONABLE})

#: Roster ``status`` values that mean the player is not available this week.
#: ``DEV`` (practice squad) is not here — an elevated practice-squad player can
#: play, and they almost never carry a posted prop line anyway.
_ROSTER_OUT = {
    "RES": "reserve/injured",
    "INA": "inactive",
    "CUT": "not on the roster",
    "RET": "retired",
    "EXE": "exempt list",
}

#: Roster ``status_description_abbr`` values that mean out even while ``status``
#: still reads ACT. ``I01``/``I02`` is the designation carried for the upcoming
#: game; on every settled week of 2026 no player holding it recorded a stat.
_ABBR_OUT = {"I01": "inactive", "I02": "inactive"}

_REPORT = {"out": OUT, "doubtful": DOUBTFUL, "questionable": QUESTIONABLE}

#: Offensive depth-chart positions. Everything else on the chart is defence or
#: special teams, where ``pos_abb`` is a slot name rather than a position.
_DEPTH_POSITIONS = {"QB", "RB", "WR", "TE"}

#: Below this a vacancy is not worth reporting. A fifth receiver going on
#: injured reserve moves nobody's line, and reporting it would bury the cases
#: that do under cases that do not.
MIN_VACATED_SHARE = 0.08

#: A position group has to hold at least this much of its team's projected
#: total for a market before a vacancy in it means anything.
#:
#: Without it the share is computed against whatever denominator happens to
#: exist, and the largest vacancies on the board are things like "the Rams'
#: receivers lost 94% of their projected *rushing* yards" — 3.9 yards between
#: three men. Expressed relative to the team rather than as a floor in yards so
#: that one number covers yardage and receptions alike. A backup quarterback
#: inheriting a starter's rushing still clears it comfortably, which is the
#: case this must not throw away.
MIN_GROUP_SHARE = 0.10

#: Stat columns the vacated share can be measured on — one per bettable market,
#: plus the two volume counts, which are the cleanest read on a role.
USAGE_COMPONENTS = ("rushing_yards", "receiving_yards", "receptions",
                    "passing_yards", "carries", "targets")

#: Weeks of the current season needed before it replaces the prior one as the
#: usage baseline. Matches ``usage.MIN_CURRENT_WEEKS``.
MIN_USAGE_WEEKS = 2

_TTL_SECONDS = 2 * 3600
_cache: dict = {"key": None, "value": None, "fetched_at": 0.0}
_usage_cache: dict = {"key": None, "value": None, "fetched_at": 0.0}


@dataclass
class Player:
    """One player's availability, joined from all three feeds."""
    key: str
    player_id: Optional[str]
    player: Optional[str]
    team: Optional[str]
    position: Optional[str]
    status: str = ACTIVE
    reason: Optional[str] = None
    roster_status: Optional[str] = None
    report_status: Optional[str] = None
    practice_status: Optional[str] = None
    injury: Optional[str] = None
    depth_rank: Optional[int] = None

    @property
    def playable(self) -> bool:
        return self.status in PLAYABLE

    def as_dict(self) -> dict:
        return {
            "status": self.status, "reason": self.reason,
            "roster_status": self.roster_status,
            "report_status": self.report_status,
            "practice_status": self.practice_status,
            "injury": self.injury, "depth_rank": self.depth_rank,
        }


@dataclass
class Board:
    """Every player the feeds know about, indexed both ways.

    Two indexes because the join has two qualities of key. ``player_id`` is a
    gsis id and is exact; the name key is the fallback for a player the feeds
    and the projections spell differently, and it is why ``names.norm_name``
    is shared rather than reimplemented here.
    """
    season: int
    week: int
    by_id: dict = field(default_factory=dict)
    by_key: dict = field(default_factory=dict)
    depth_as_of: Optional[str] = None
    injury_rows: int = 0
    errors: list = field(default_factory=list)

    def get(self, player_id: Optional[str] = None,
            key: Optional[str] = None) -> Optional[Player]:
        if player_id and player_id in self.by_id:
            return self.by_id[player_id]
        if key and key in self.by_key:
            return self.by_key[key]
        return None

    @property
    def out(self) -> list:
        return [p for p in self.by_id.values() if not p.playable]

    def summary(self) -> dict:
        counts: dict = {}
        for p in self.by_id.values():
            counts[p.status] = counts.get(p.status, 0) + 1
        return {
            "season": self.season, "week": self.week,
            "players": len(self.by_id),
            "counts": counts,
            "depth_as_of": self.depth_as_of,
            "injury_report_rows": self.injury_rows,
            "errors": self.errors,
        }


@dataclass
class Vacancy:
    """What one team's position group lost, and to whom it goes."""
    team: str
    position: str
    component: str
    share: float
    players: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"team": self.team, "position": self.position,
                "component": self.component, "share": round(self.share, 3),
                "players": self.players}


def _verdict(roster_status, abbr, report_status, practice_status, injury):
    """``(status, reason)`` from the three feeds, hardest evidence first.

    Order matters. A roster designation is a fact about the week; an injury
    report is a forecast of one, and a Wednesday non-participation is barely
    even that. Reading them in the other order would let a "Questionable" tag
    left over from a stale report override an IR placement.
    """
    if roster_status in _ROSTER_OUT:
        return OUT, _ROSTER_OUT[roster_status]
    if abbr in _ABBR_OUT:
        return OUT, _ABBR_OUT[abbr]

    reported = _REPORT.get((report_status or "").strip().lower())
    if reported:
        return reported, f"{report_status.strip()}{f' ({injury})' if injury else ''}"

    # No report filed yet. A player who did not practise at all is the only
    # pre-report signal worth carrying, and it is carried as the softest
    # status there is — most of them play. It exists so that a Tuesday board,
    # built before a single club has filed, is not simply blind.
    if (practice_status or "").strip().lower().startswith("did not"):
        return QUESTIONABLE, f"did not practise{f' ({injury})' if injury else ''}"
    return ACTIVE, None


def _fetch(season: int, week: int) -> Board:
    import pandas as pd

    from .names import norm_name

    board = Board(season=season, week=week)

    def read(url, **kw):
        return pd.read_parquet(url.format(season=season), **kw)

    # --- roster: the designation, and the spine of the index ----------------
    try:
        r = read(NFLVERSE_ROSTER, columns=[
            "week", "team", "position", "full_name", "gsis_id", "status",
            "status_description_abbr"])
        # The requested week if it is published, otherwise the most recent one
        # before it — a roster carries forward, unlike an injury report.
        weeks = [w for w in r.week.dropna().unique() if int(w) <= week]
        r = r[r.week == max(weeks)] if weeks else r.iloc[0:0]
    except Exception as e:                       # never cost the board its props
        logger.warning("[nfl-avail] rosters %s failed: %s", season, e)
        board.errors.append(f"rosters: {e}")
        r = None

    # --- injuries: the official report, filed team by team through the week -
    injuries: dict = {}
    try:
        inj = read(NFLVERSE_INJURIES)
        # This week only. Carrying last week's report forward would apply a
        # settled status to an unplayed game, which is worse than no status.
        inj = inj[inj.week == week]
        board.injury_rows = len(inj)
        for row in inj.itertuples(index=False):
            injuries[getattr(row, "gsis_id", None)] = (
                getattr(row, "report_status", None),
                getattr(row, "practice_status", None),
                getattr(row, "report_primary_injury", None)
                or getattr(row, "practice_primary_injury", None),
            )
    except Exception as e:
        logger.warning("[nfl-avail] injuries %s failed: %s", season, e)
        board.errors.append(f"injuries: {e}")

    # --- depth chart: positional rank, from the latest snapshot -------------
    depth: dict = {}
    try:
        d = read(NFLVERSE_DEPTH,
                 columns=["dt", "team", "player_name", "gsis_id", "pos_abb",
                          "pos_rank"])
        d = d[d.dt == d.dt.max()]
        board.depth_as_of = str(d.dt.iloc[0]) if len(d) else None
        d = d[d.pos_abb.isin(_DEPTH_POSITIONS)]
        # A player can appear more than once (a back listed at RB and on a
        # return unit); his role is the best rank he holds.
        for gsis, rank in d.groupby("gsis_id").pos_rank.min().items():
            depth[gsis] = int(rank)
    except Exception as e:
        logger.warning("[nfl-avail] depth charts %s failed: %s", season, e)
        board.errors.append(f"depth: {e}")

    if r is None:
        return board

    for row in r.itertuples(index=False):
        gsis = getattr(row, "gsis_id", None)
        name = getattr(row, "full_name", None)
        report, practice, injury = injuries.get(gsis, (None, None, None))
        status, reason = _verdict(
            getattr(row, "status", None),
            getattr(row, "status_description_abbr", None),
            report, practice, injury,
        )
        p = Player(
            key=norm_name(name), player_id=gsis, player=name,
            team=getattr(row, "team", None),
            position=getattr(row, "position", None),
            status=status, reason=reason,
            roster_status=getattr(row, "status", None),
            report_status=report, practice_status=practice, injury=injury,
            depth_rank=depth.get(gsis),
        )
        if gsis:
            board.by_id[gsis] = p
        # Names collide (two Josh Joneses). The id index is exact and is tried
        # first, so a collision only costs the fallback — but prefer the
        # unavailable one when it happens, since a missed absence is the
        # expensive error and a spurious one only declines a bet.
        prev = board.by_key.get(p.key)
        if p.key and (prev is None or (prev.playable and not p.playable)):
            board.by_key[p.key] = p
    return board


def week_board(season: int, week: int, force: bool = False) -> Board:
    """Cached availability for one week.

    Two hours, which is shorter than it looks: the depth chart republishes
    about twice a day and the injury report lands team by team, so the thing
    this TTL bounds is how long a fresh absence sits unseen — and the board
    around it has a ten-minute TTL, so anything longer here would make the
    hourly rebuild pointless.
    """
    key = (season, week)
    now = time.time()
    cached = _cache["value"]
    if (not force and _cache["key"] == key and cached is not None
            and now - _cache["fetched_at"] < _TTL_SECONDS):
        return cached
    try:
        board = _fetch(season, week)
    except Exception as e:
        logger.warning("[nfl-avail] %s wk%s failed: %s", season, week, e)
        return cached if _cache["key"] == key and cached else Board(season, week,
                                                                    errors=[str(e)])
    if board.by_id or board.errors:
        _cache.update({"key": key, "value": board, "fetched_at": now})
    return board


def usage_baseline(season: int, week: int, force: bool = False) -> dict:
    """``{gsis_id: {component: per-game value}}`` from the weeks already played.

    The baseline has to be realised usage rather than the projection set, and
    that is not a preference — it is forced. **The projection engine drops a
    player the moment he lands on injured reserve**, so by the time a vacancy
    exists the man who created it is no longer in the projections to be counted.
    Jordan Mason is absent from Minnesota's week-3 projections entirely; a share
    computed over what remains would report a backfield that lost nothing,
    while Aaron Jones is projected for 31 rushing yards after running for 72 a
    game beside him.

    Trailing weeks of the current season once there are two of them, the prior
    season before that — the same switch ``usage`` makes, for the same reason.
    """
    key = (season, week)
    now = time.time()
    if (not force and _usage_cache["key"] == key
            and now - _usage_cache["fetched_at"] < _TTL_SECONDS
            and _usage_cache["value"] is not None):
        return _usage_cache["value"]
    try:
        out = _compute_usage(season, week)
    except Exception as e:
        logger.warning("[nfl-avail] usage baseline %s wk%s failed: %s",
                       season, week, e)
        return _usage_cache["value"] or {} if _usage_cache["key"] == key else {}
    _usage_cache.update({"key": key, "value": out, "fetched_at": now})
    return out


def _compute_usage(season: int, week: int) -> dict:
    import pandas as pd

    def load(s):
        df = pd.read_parquet(NFLVERSE_WEEKLY.format(season=s),
                             columns=["season", "week", "season_type", "team",
                                      "position", "player_id",
                                      "player_display_name", *USAGE_COMPONENTS])
        return df[df.season_type == "REG"]

    cur = load(season)
    cur = cur[cur.week < week]
    use = cur if cur.week.nunique() >= MIN_USAGE_WEEKS else load(season - 1)
    if use.empty:
        return {}

    out: dict = {}
    for pid, rows in use.groupby("player_id"):
        games = max(rows.week.nunique(), 1)
        rec = {c: float(rows[c].fillna(0).sum()) / games for c in USAGE_COMPONENTS}
        rec["team"] = rows.team.iloc[-1]
        rec["position"] = rows.position.iloc[-1]
        rec["player"] = rows.player_display_name.iloc[-1]
        out[pid] = rec
    return out


def vacancies(board: Board, component: str,
              baseline: Optional[dict] = None) -> dict:
    """``{(team, position): Vacancy}`` — the share of a group that will not play.

    Attribution is to the player's **current** team and position, taken from
    the roster feed, not to the team he produced the yards for. Without that,
    every week-1 and week-2 board (where the baseline is last season) credits a
    vacancy to whichever club a player has just left.
    """
    baseline = baseline if baseline is not None else {}
    totals: dict = {}
    team_totals: dict = {}
    lost: dict = {}
    who: dict = {}

    for pid, rec in baseline.items():
        value = rec.get(component) or 0.0
        if value <= 0:
            continue
        p = board.by_id.get(pid)
        team = (p.team if p else None) or rec.get("team")
        pos = (p.position if p else None) or rec.get("position")
        if not team or not pos:
            continue
        cell = (team, pos)
        totals[cell] = totals.get(cell, 0.0) + value
        team_totals[team] = team_totals.get(team, 0.0) + value
        if p is not None and not p.playable:
            lost[cell] = lost.get(cell, 0.0) + value
            who.setdefault(cell, []).append({
                "player": p.player or rec.get("player"),
                "status": p.status, "reason": p.reason,
                "baseline": round(value, 1),
            })

    out: dict = {}
    for cell, missing in lost.items():
        team, pos = cell
        total = totals.get(cell, 0.0)
        team_total = team_totals.get(team, 0.0)
        if total <= 0 or team_total <= 0:
            continue
        # Is this group where the market lives? See MIN_GROUP_SHARE.
        if total / team_total < MIN_GROUP_SHARE:
            continue
        share = missing / total
        if share < MIN_VACATED_SHARE:
            continue
        out[cell] = Vacancy(
            team=team, position=pos, component=component, share=share,
            players=sorted(who.get(cell, []), key=lambda d: -d["baseline"]),
        )
    return out
