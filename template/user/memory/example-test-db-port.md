---
name: example-test-db-port
description: EXAMPLE — the integration tests of my-app expect PostgreSQL on port 5433, not 5432
metadata:
  type: project
---

The integration tests of `my-app` connect to PostgreSQL on `127.0.0.1:5433` (a second local
instance), because 5432 is used by another project. Decided 2026-01-15.

**Why:** two projects on one machine, each with its own database version.
**How to apply:** start the test database with `make test-db` before running `pytest -m integration`.
