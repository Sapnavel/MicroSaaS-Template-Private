#!/bin/bash
# Runs automatically via docker-entrypoint-initdb.d on a FRESH postgres
# volume only (see database/schema.sql's own init-script mount for the
# `hms` database this doesn't touch). HMS Project Completion Prompt gap:
# `backend/tests/conftest.py` defaults to a SEPARATE `hms_test` database on
# this same instance, not `hms` (see that file's module docstring) --
# `POSTGRES_DB` (hms) is the only database docker-entrypoint-initdb.d
# creates/initializes automatically, so `hms_test` needs this second script
# to get the same treatment: create the database, then apply the exact same
# schema.sql to it.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d postgres -c "CREATE DATABASE hms_test;"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d hms_test -f /docker-entrypoint-initdb.d/01-schema.sql
