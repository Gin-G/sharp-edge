"""Abstract database interface — implemented by SQLite and PostgreSQL backends.

Every method that touches user-owned data takes `user_id` so visitors are
isolated from one another. The session cookie carries the uid; routes pull
it via the get_uid dependency.
"""

from abc import ABC, abstractmethod
from typing import Optional


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
