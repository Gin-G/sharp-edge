"""Abstract database interface — implemented by SQLite and PostgreSQL backends.

Every method that touches user-owned data takes `user_id` so visitors are
isolated from one another. The session cookie carries the uid; routes pull
it via the get_uid dependency.
"""

from abc import ABC, abstractmethod
from typing import Optional

# Every model_picks column except `metrics` — see BetDatabase.list_picks.
PICK_COLUMNS = (
    "screen, pick_date, batter_id, batter, team, pitcher_id, opposing_pitcher, "
    "venue, score, rank, tags, source, result, hr_actual, hits_actual, "
    "pa_actual, created_at, resolved_at"
)


class BetDatabase(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def upsert_bet(self, user_id: str, bet: dict) -> None: ...

    @abstractmethod
    async def upsert_bets(self, user_id: str, bets: list[dict]) -> int: ...

    @abstractmethod
    async def query_bets(
        self,
        user_id: str,
        status: Optional[str] = None,
        league: Optional[str] = None,
        sportsbook: Optional[str] = None,
        bet_type: Optional[str] = None,
        sport: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]: ...

    @abstractmethod
    async def get_summary_stats(
        self,
        user_id: str,
        league: Optional[str] = None,
        sportsbook: Optional[str] = None,
        bet_type: Optional[str] = None,
        since: Optional[str] = None,
    ) -> dict: ...

    @abstractmethod
    async def get_breakdown(
        self,
        user_id: str,
        group_by: str = "league",
        since: Optional[str] = None,
    ) -> list[dict]: ...

    @abstractmethod
    async def get_calendar_data(
        self,
        user_id: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> list[dict]: ...

    @abstractmethod
    async def get_sync_state(self, user_id: str, key: str) -> Optional[str]: ...

    @abstractmethod
    async def set_sync_state(self, user_id: str, key: str, value: str) -> None: ...

    @abstractmethod
    async def import_pikkit_csv(self, user_id: str, csv_path: str) -> int: ...

    # ------------------------------------------------------------------
    # Model pick tracking — daily screen picks vs actual outcomes.
    # Not user-scoped: the screens are public data, so the track record is
    # shared. pick_date is an ISO "YYYY-MM-DD" string in and out.
    # ------------------------------------------------------------------

    @abstractmethod
    async def insert_picks(self, rows: list[dict]) -> int:
        """Insert pick rows, skipping any (screen, pick_date, batter_id)
        already present so re-runs never clobber an existing record or its
        resolved outcome. Returns the number actually inserted."""

    @abstractmethod
    async def list_picks(
        self,
        screen: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
        unresolved_only: bool = False,
        include_metrics: bool = False,
    ) -> list[dict]:
        """Picks for a screen, newest first. ``metrics`` is a per-pick JSON
        blob that nothing in the app reads back — it's kept for later model
        analysis — so it is omitted unless asked for; over a full season it
        dominates the row size."""

    @abstractmethod
    async def pick_dates(self, screen: str) -> set[str]:
        """Every pick_date that already has at least one row for a screen —
        lets a catch-up run skip days it has already generated."""

    @abstractmethod
    async def update_pick_results(
        self, screen: str, pick_date: str, results: list[dict]
    ) -> int:
        """Settle picks for one date. Each result dict carries batter_id,
        result ('WIN'/'LOSS'/'VOID'), hr_actual, hits_actual, pa_actual.
        Returns the number of rows updated."""
