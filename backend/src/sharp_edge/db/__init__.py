"""Database package — exports create_database factory and BetDatabase ABC."""

from .base import BetDatabase


async def create_database(url: str) -> BetDatabase:
    """Instantiate and connect the appropriate database backend."""
    if url.startswith("postgresql"):
        from .postgres import PostgresDatabase
        db = PostgresDatabase(url)
    else:
        from .sqlite import SQLiteDatabase
        db = SQLiteDatabase(url)
    await db.connect()
    return db


__all__ = ["BetDatabase", "create_database"]
