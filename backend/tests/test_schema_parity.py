"""The documented copy path and the CLI migration path must create the same tables."""

from __future__ import annotations

import re
from pathlib import Path

SUPABASE = Path(__file__).resolve().parents[1] / "openseek" / "supabase"
CREATE = re.compile(r"create table if not exists public\.([a-z_]+)", re.IGNORECASE)


def tables(sql: str) -> set[str]:
    return set(CREATE.findall(sql))


def test_schema_sql_and_migrations_create_the_same_tables():
    copy_path = tables((SUPABASE / "schema.sql").read_text())
    migration_path: set[str] = set()
    for migration in (SUPABASE / "migrations").glob("*.sql"):
        migration_path |= tables(migration.read_text())
    assert copy_path, "schema.sql must create tables"
    assert migration_path == copy_path
