# Database migrations

`infra/init.sql` is the canonical schema and runs automatically the first
time the TimescaleDB container starts on a fresh volume. After that, schema
changes go in `infra/migrations/NNN_*.sql` and must be applied manually.

## Applying a migration

From the project root:

```bash
docker compose exec -T timescaledb psql -U qsde -d qsde \
    < infra/migrations/001_phase0_fixes.sql
```

Or, if `psql` is on your host:

```bash
psql "postgresql://qsde:qsde_dev_2026@localhost:5432/qsde" \
    -f infra/migrations/001_phase0_fixes.sql
```

All migrations are idempotent — they use `IF NOT EXISTS` / dynamic
constraint discovery so re-running is safe.

## Resetting the schema (dev only)

If you have no data worth keeping (Phase 0 typically), the cleanest path
is to wipe the volume and let `init.sql` rebuild from scratch:

```bash
docker compose down -v       # -v deletes the named volumes
docker compose up -d
```

After this, you do **not** need to apply migrations — `init.sql` already
contains the post-migration schema.

## Migration history

| File | Date | Notes |
|---|---|---|
| `001_phase0_fixes.sql` | 2026-05-10 | Fix audit bugs 2/3/4/5: PIT-correct fundamentals PK (adds `filing_date` to PK), adds `market_cap`/`enterprise_value`/`ev_to_revenue`/`roic`/`fcf_per_share` columns, adds UNIQUE constraint on `bulk_deals` natural key. |
