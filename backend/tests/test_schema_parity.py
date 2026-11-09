"""The documented copy schema and CLI migration path must preserve the same API."""

from __future__ import annotations

import re
from pathlib import Path

SUPABASE = Path(__file__).resolve().parents[1] / "openseek" / "supabase"
TABLE = re.compile(r"create table if not exists public\.([a-z_]+)\s*\((.*?)\n\);",
                   re.IGNORECASE | re.DOTALL)
FUNCTION = re.compile(r"(create or replace function public\.([a-z_]+)\b.*?\$\$;)",
                      re.IGNORECASE | re.DOTALL)
VIEW = re.compile(r"create or replace view public\.pool_packages\b.*?;",
                  re.IGNORECASE | re.DOTALL)


def normalize(sql: str) -> str:
    return " ".join(sql.split()).lower()


def table_definitions(sql: str) -> dict[str, str]:
    return {name.lower(): normalize(body) for name, body in TABLE.findall(sql)}


def function_definitions(sql: str) -> dict[str, str]:
    return {name.lower(): normalize(statement) for statement, name in FUNCTION.findall(sql)}


def test_schema_sql_and_migrations_match_tables_and_columns():
    schema = (SUPABASE / "schema.sql").read_text()
    migrations = "\n".join(path.read_text() for path in sorted((SUPABASE / "migrations").glob("*.sql")))
    schema_tables = table_definitions(schema)
    migration_tables = table_definitions(migrations)
    assert schema_tables, "schema.sql must create tables"
    assert migration_tables.keys() == schema_tables.keys()
    assert migration_tables == schema_tables


def test_remote_rpc_definitions_and_service_role_privileges_match():
    schema = (SUPABASE / "schema.sql").read_text()
    migrations = "\n".join(path.read_text() for path in sorted((SUPABASE / "migrations").glob("*.sql")))
    remote = (Path(__file__).resolve().parents[1] / "openseek" / "remote.py").read_text()
    current_rpcs = set(re.findall(r'\.rpc\("([a-z_]+)"', remote))
    assert current_rpcs
    schema_functions = function_definitions(schema)
    migration_functions = function_definitions(migrations)
    for name in current_rpcs:
        assert name in schema_functions
        assert name in migration_functions
        assert schema_functions[name] == migration_functions[name], name
        for sql in (schema, migrations):
            assert re.search(rf"revoke all on function public\.{name}\([^;]+\) from public\s*;",
                             sql, re.IGNORECASE)
            assert re.search(rf"grant execute on function public\.{name}\([^;]+\) to service_role\s*;",
                             sql, re.IGNORECASE)


def test_view_security_and_rls_privileges_are_explicit_in_both_paths():
    schema = (SUPABASE / "schema.sql").read_text()
    migrations = "\n".join(path.read_text() for path in sorted((SUPABASE / "migrations").glob("*.sql")))
    rls_tables = lambda sql: {name.lower() for name in re.findall(
        r"alter table public\.([a-z_]+) enable row level security\s*;", sql, re.IGNORECASE)}
    assert rls_tables(schema) == rls_tables(migrations) == set(table_definitions(schema))

    schema_view, = VIEW.findall(schema)
    migration_view, = VIEW.findall(migrations)
    assert normalize(schema_view) == normalize(migration_view)
    assert re.search(r"with\s*\(\s*security_invoker\s*=\s*true\s*\)", schema_view,
                     re.IGNORECASE)
    for sql in (schema, migrations):
        assert re.search(
            r"revoke all on public\.pool_packages from public\s*,\s*anon\s*,\s*authenticated\s*;",
            sql, re.IGNORECASE)
        assert re.search(r"grant select on public\.pool_packages to service_role\s*;",
                         sql, re.IGNORECASE)
