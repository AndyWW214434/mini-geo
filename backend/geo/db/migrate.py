"""Apply SQL migrations in order. Each file runs once, in its own transaction."""

from __future__ import annotations

from pathlib import Path

import psycopg

from geo.config import settings

MIGRATIONS = Path(__file__).parent / "migrations"


def migrate(database_url: str | None = None) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(database_url or settings().database_url, autocommit=True) as conn:
        conn.execute("select pg_advisory_lock(7741001)")  # replicas starting together migrate one at a time
        conn.execute("create table if not exists public.schema_migrations "
                     "(name text primary key, applied_at timestamptz not null default now())")
        done = {r[0] for r in conn.execute("select name from public.schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("insert into public.schema_migrations (name) values (%s)", (path.name,))
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    print("applied:", migrate() or "nothing new")
