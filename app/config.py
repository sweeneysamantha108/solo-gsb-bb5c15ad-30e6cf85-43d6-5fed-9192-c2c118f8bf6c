from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env(
            "DATABASE_URL",
            "postgresql://royalty:royalty@db:5432/royalty",
        )
    )
    db_min_conn: int = field(default_factory=lambda: int(_env("DB_MIN_CONN", "1")))
    db_max_conn: int = field(default_factory=lambda: int(_env("DB_MAX_CONN", "10")))
    schema_path: str = field(
        default_factory=lambda: _env("SCHEMA_PATH", "/app/sql/schema.sql")
    )
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8000")))
    # Document numbering: e.g. STMT-202601-0, 追补单 STMT-202601-1 ...
    doc_prefix: str = field(default_factory=lambda: _env("DOC_PREFIX", "STMT"))


settings = Settings()
