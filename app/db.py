from __future__ import annotations

import time
from pathlib import Path
from contextlib import AbstractContextManager

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("database pool is not initialised")
    return _pool


def init_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # The database container can still be starting up; build a fresh pool
        # per attempt because a pool whose open() failed cannot be reused.
        last_err: Exception | None = None
        for _ in range(60):
            pool = ConnectionPool(
                settings.database_url,
                min_size=settings.db_min_conn,
                max_size=settings.db_max_conn,
                kwargs={"row_factory": dict_row, "autocommit": False},
                open=False,
            )
            try:
                pool.open(wait=True)
                _pool = pool
                break
            except Exception as exc:  # pragma: no cover - startup timing
                last_err = exc
                try:
                    pool.close()
                except Exception:
                    pass
                time.sleep(1)
        else:  # pragma: no cover - startup timing
            raise RuntimeError(f"could not connect to database: {last_err}")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def init_schema() -> None:
    """Apply the schema. The image already initialised it via
    /docker-entrypoint-initdb.d; this also works for external databases."""
    sql = Path(settings.schema_path).read_text(encoding="utf-8")
    pool = get_pool()
    with pool.connection() as conn:
        conn.execute(sql)
        conn.commit()


def conn() -> AbstractContextManager[Connection]:
    return get_pool().connection()
