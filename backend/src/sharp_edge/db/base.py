"""Abstract database interface — implemented by SQLite and PostgreSQL backends."""

from abc import ABC, abstractmethod
from typing import Optional


class BetDatabase(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def upsert_bet(self, bet: dict) -> None: ...

    @abstractmethod
    async def upsert_bets(self, bets: list[dict]) -> int: ...

    @abstractmethod
    async def query_bets(
        self,
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
        league: Optional[str] = None,
        sportsbook: Optional[str] = None,
        bet_type: Optional[str] = None,
        since: Optional[str] = None,
    ) -> dict: ...

    @abstractmethod
    async def get_breakdown(
        self, group_by: str = "league", since: Optional[str] = None
    ) -> list[dict]: ...

    @abstractmethod
    async def get_calendar_data(
        self, since: Optional[str] = None, until: Optional[str] = None
    ) -> list[dict]: ...

    @abstractmethod
    async def get_sync_state(self, key: str) -> Optional[str]: ...

    @abstractmethod
    async def set_sync_state(self, key: str, value: str) -> None: ...

    @abstractmethod
    async def import_pikkit_csv(self, csv_path: str) -> int: ...
