# PRP: Auth & RBAC/ABAC Module

> Implementation blueprint for parallel agent execution.
> This is a **module PRP** inside the existing Hospital Management & Appointment
> Booking System scaffold — it does not start a new product. Read
> `docs/ARCHITECTURE.md` and `database/schema.sql` before touching any file
> listed below; this PRP builds on top of them, it does not replace them.

---

## METADATA

| Field | Value |
|-------|-------|
| **Product** | Hospital Management & Appointment Booking System |
| **Module** | Auth & RBAC/ABAC Engine |
| **Version** | 1.0 |
| **Created** | 2026-07-28 |
| **Complexity** | Medium-High (security-critical, no room for shortcuts) |

---

## MODULE OVERVIEW

**Description:** Full authentication and authorization layer: JWT access/refresh
tokens with signing-key rotation, login for all 8 roles, an RBAC+ABAC dependency
layer that scopes every request by tenant (`hospital_group_id`) and branch
(`branch_id`), and Redis-backed token revocation on logout.

**Why this module, why now:** Every other module (`routers/patients.py`,
`routers/consultations.py`, etc.) is currently a stub that depends on
`get_current_user` / `require_role` / `authorize` from `app/core/security.py`.
This PRP finishes that dependency so subsequent module PRPs have a real,
tenant-aware auth layer to build against instead of the current
proof-of-concept version (no key rotation, no revocation, no tenant claim).

**What already exists (do not recreate):**
- `database/schema.sql` — `users`, `refresh_tokens`, `permissions`,
  `role_permissions` tables, `user_role` enum with all 8 roles, `branches` →
  `hospital_groups` tenancy chain.
- `backend/app/models/user.py` — `User` ORM model, `UserRole` enum.
- `backend/app/models/tenant.py` — `Branch`, `HospitalGroup` ORM models.
- `backend/app/core/security.py` — `hash_password`, `verify_password`,
  `create_access_token`, `create_refresh_token`, `decode_token`,
  `get_current_user`, `require_role`, and an ABAC `policy`/`authorize`
  registry with a few example policies.
- `backend/app/core/locking.py` — Redis client pattern to copy for the
  blocklist (`redis.Redis.from_url(settings.redis_url, ...)`).
- `backend/app/routers/auth.py` — stub with a TODO list; this PRP replaces
  the stub with real endpoints.

**MVP Scope:**
- [ ] JWT access/refresh tokens signed with a rotatable keyring (`kid`-based)
- [ ] Login endpoints covering all 8 roles (staff portal + patient portal)
- [ ] Self-service patient registration; admin-provisioned staff accounts
  (role/branch is never client-settable)
- [ ] RBAC/ABAC dependency layer carrying `hospital_group_id` + `branch_id`
  in the token and enforcing them by default
- [ ] Redis blocklist for revoked access tokens; DB-tracked revocation for
  refresh tokens; logout revokes both

---

## TECH STACK

| Layer | Technology | Skill Reference |
|-------|------------|-----------------|
| Backend | FastAPI + Python 3.11+ | skills/BACKEND.md |
| Frontend | React + TypeScript + Vite | skills/FRONTEND.md |
| Database | PostgreSQL + SQLAlchemy (no new migration required — see below) | skills/DATABASE.md |
| Auth | JWT (PyJWT) + bcrypt (passlib) + Redis (blocklist) | skills/BACKEND.md |
| UI | Chakra UI / Tailwind (match whatever `frontend/` already uses) | skills/FRONTEND.md |
| Testing | pytest + Vitest/RTL | skills/TESTING.md |

**No schema migration needed.** `refresh_tokens` (token_hash, expires_at,
revoked) already covers refresh-token revocation. Access-token revocation is
intentionally Redis-only and self-expiring (see Security Design) — it does
not need a table.

---

## SECURITY DESIGN (read before implementing)

### 1. Signing-key rotation

Single-secret `settings.secret_key` is replaced by a **keyring**:

```env
JWT_SIGNING_KEYS={"k1": "base64-secret-1", "k2": "base64-secret-2"}
JWT_CURRENT_KID=k2
```

- All new tokens (access + refresh) are signed with `JWT_CURRENT_KID`'s
  secret and carry `kid` in the **JWT header** (`jwt.encode(..., headers={"kid": current_kid})`).
- `decode_token` reads the `kid` from the unverified header
  (`jwt.get_unverified_header`), looks up the matching secret in the
  keyring, and verifies with that secret. Unknown `kid` → 401.
- **Rotation procedure:** add a new `kid` to `JWT_SIGNING_KEYS`, deploy,
  then flip `JWT_CURRENT_KID` to it. Keep the old `kid` in the keyring
  until every refresh token signed with it has expired
  (`REFRESH_TOKEN_EXPIRE_DAYS`), then remove it. This is why verification
  is keyring-based, not single-key: old tokens must keep verifying during
  the overlap window.
- Extend `app/config.py` with `jwt_signing_keys: dict[str, str]` and
  `jwt_current_kid: str` (Pydantic `Settings`, parsed from the JSON env var).

### 2. Token claims

```json
{
  "sub": "<user_id>",
  "jti": "<uuid4>",
  "type": "access" | "refresh",
  "role": "doctor",
  "branch_id": "<uuid or null for system_admin>",
  "hospital_group_id": "<uuid or null for system_admin>",
  "exp": ...
}
```

`jti` is new — required for the blocklist (below). `branch_id` /
`hospital_group_id` are new — required so ABAC checks don't need a DB
round-trip to find the tenant on every request.

### 3. RBAC/ABAC dependency layer

- **RBAC** (coarse): `require_role(*roles)` already exists — keep it.
- **ABAC** (fine): extend the existing `policy`/`authorize` registry in
  `core/security.py` so `authorize()` does two things in order:
  1. **Tenant guard (default-deny, not opt-in):** if the resource has a
     `branch_id` attribute, it must equal the caller's `branch_id`
     *unless* the caller's role is `system_admin` (cross-branch) — this
     check happens before any registered policy runs, so a missing or
     wrong policy can never accidentally leak cross-branch data.
  2. **Registered policy** for `(role, resource_type, action)`, as today.
- Add a `SecurityContext` (or reuse `User` — decide based on whether you
  want branch/tenant on every `User` load or only from the token; **prefer
  reading branch_id/hospital_group_id from the validated JWT claims**, not
  a fresh DB query, so a user moved to a different branch mid-session
  doesn't silently gain access via a stale `User` row — invalidate by
  requiring re-login after a branch change instead).

### 4. Redis blocklist on logout

- `POST /auth/logout` invalidates **both** tokens presented:
  - Access token: add `jti` to Redis `SET blocklist:{jti} "1" EX <seconds-until-exp>`
    (TTL = token's own remaining lifetime, computed from its `exp` claim —
    entries self-expire, the blocklist never grows unbounded).
  - Refresh token: look up its `token_hash` in `refresh_tokens` and set
    `revoked = true`.
- `get_current_user` checks `redis.exists(f"blocklist:{jti}")` before
  trusting an otherwise-valid access token. This is one extra Redis round
  trip per request — acceptable; do not skip it to save latency, revocation
  is a hard security requirement here (HIPAA/GDPR — CLAUDE.md compliance intent).
- `POST /auth/refresh` checks `refresh_tokens.revoked` before issuing a new
  access token.

### 5. Login endpoints for all 8 roles

Do **not** write 8 separate login functions — role is a property of the
`User` row, not of the endpoint. Two endpoints cover all 8 roles, split by
**portal** because patients self-register and staff do not:

- `POST /auth/login` — staff portal: doctor, nurse, front_desk, lab_tech,
  pharmacist, billing_admin, system_admin. Requires `is_verified=true` and
  `branch_id IS NOT NULL` (except `system_admin`, which is cross-branch).
- `POST /auth/patient/login` — patient portal: role=patient only. Kept as
  a separate route (not just a shared function) because patient auth is
  the most likely place to later add MFA/OTP without touching staff auth.

Both call one shared `authenticate_user(db, email, password) -> User`
helper — do not duplicate the password-check logic.

### 6. Rate limiting (OWASP — required, not optional)

Both login endpoints must be rate-limited per IP + per email (e.g.
`slowapi` or a Redis token-bucket keyed on `f"login_attempts:{email}"`).
This was flagged as a REVIEW-AGENT checklist item in `.claude/commands/execute-prp.md`
— implement it up front rather than waiting for review to catch it.

---

## ENDPOINTS

| Method | Endpoint | Auth | Description |
|--------|----------|------|--------------|
| POST | /auth/register | none | Patient self-registration only. Server sets `role=patient`; client cannot set role or branch_id. |
| POST | /auth/login | none, rate-limited | Staff login (7 non-patient roles). Returns access + refresh token pair. |
| POST | /auth/patient/login | none, rate-limited | Patient login. Same token shape as staff login. |
| POST | /auth/refresh | refresh token | Checks `refresh_tokens.revoked`, issues new access token (and rotates the refresh token — see rotation note below). |
| POST | /auth/logout | access token | Blocklists the access token's `jti` in Redis, revokes the presented refresh token in DB. |
| GET | /auth/me | access token | Returns current user profile (no PHI beyond the user's own record). |
| POST | /admin/staff | system_admin only | Provision a staff account: sets role + branch_id explicitly; this is the *only* path that creates non-patient users. |

**Refresh token rotation:** each call to `/auth/refresh` should revoke the
presented refresh token and issue a new one (rotate-on-use), not just mint
a new access token — this bounds the blast radius of a stolen refresh
token to a single use. Store the new token's row in `refresh_tokens`.

---

## FILES TO CREATE / MODIFY

**Modify:**
- `backend/app/config.py` — add `jwt_signing_keys`, `jwt_current_kid`,
  remove/deprecate the single `secret_key` for JWT purposes (keep it only
  if something else still uses it; check `core/encryption.py`'s
  `deterministic_hash`, which currently reuses `settings.secret_key` as an
  HMAC key — give that its own `hmac_key` setting instead of sharing the
  JWT secret across two purposes).
- `backend/app/core/security.py` — keyring-aware `create_access_token` /
  `create_refresh_token` / `decode_token`; add `jti`, `role`, `branch_id`,
  `hospital_group_id` to claims; add Redis blocklist check to
  `get_current_user`; extend `authorize()` with the tenant guard described
  above.
- `backend/app/routers/auth.py` — replace the stub with the endpoints
  table above.
- `backend/app/dependencies.py` — export any new dependency (e.g.
  `get_current_staff_user` if you find you need a role-narrowed variant).
- `.env.example`, `docker-compose.yml` — replace `SECRET_KEY` usage for
  JWT with `JWT_SIGNING_KEYS` / `JWT_CURRENT_KID`.

**Create:**
- `backend/app/models/token.py` — `RefreshToken` ORM model mirroring the
  existing `refresh_tokens` table (it has schema but no ORM model yet).
- `backend/app/schemas/auth.py` — `RegisterRequest`, `LoginRequest`,
  `TokenPair`, `UserProfile`, `StaffProvisionRequest` (Pydantic).
- `backend/app/services/auth_service.py` — `authenticate_user`,
  `register_patient`, `provision_staff`, `rotate_refresh_token`,
  `revoke_session` (the logout logic). Keep business logic out of the
  router, same pattern as `services/scheduling_engine.py`.
- `backend/app/core/rate_limit.py` — small Redis token-bucket helper for
  the login endpoints (reuse the Redis client pattern from `core/locking.py`).
- `frontend/src/context/AuthContext.tsx`, `frontend/src/hooks/useAuth.ts`,
  `frontend/src/services/authService.ts`
- `frontend/src/pages/LoginPage.tsx` (staff), `frontend/src/pages/PatientLoginPage.tsx`,
  `frontend/src/pages/RegisterPage.tsx`
- `frontend/src/components/auth/ProtectedRoute.tsx` — route guard that
  reads role from the decoded token/context and redirects if the role
  doesn't match the route's allowed roles.
- `backend/tests/test_auth.py`, `backend/tests/conftest.py` (test DB +
  client fixtures, per skills/TESTING.md)

---

## PHASE EXECUTION PLAN

This is a single-module PRP — no DATABASE-AGENT/DEVOPS-AGENT foundation
phase is needed (schema and infra already exist). Two phases:

**Phase 1: Backend security core (sequential — everything else depends on this)**
- BACKEND-AGENT: `config.py` keyring settings, `core/security.py` rewrite,
  `models/token.py`, `core/rate_limit.py`

**Validation Gate 1:**
```bash
ruff check backend/app/core backend/app/config.py backend/app/models/token.py
python -c "from app.core.security import create_access_token, decode_token; \
  t = create_access_token('00000000-0000-0000-0000-000000000000'); \
  print(decode_token(t))"
```

**Phase 2: Endpoints + frontend (parallel)**
- BACKEND-AGENT: `schemas/auth.py`, `services/auth_service.py`, `routers/auth.py`, admin staff-provisioning endpoint
- FRONTEND-AGENT: AuthContext, login/register pages, ProtectedRoute

**Validation Gate 2:**
```bash
ruff check backend/ && mypy backend/app --ignore-missing-imports
cd frontend && npm run lint && npm run type-check
```

**Phase 3: Quality (parallel)**
- TEST-AGENT: `backend/tests/test_auth.py` — cover: login success/failure
  per role, register can't set role/branch, refresh rotation, logout
  blocklists the token (assert a blocklisted token gets 401), rate limit
  triggers after N attempts, cross-branch ABAC denial, `system_admin`
  bypass, key-rotation (token signed with an old `kid` still verifies
  while that `kid` remains in the keyring, fails once removed).
- REVIEW-AGENT: OWASP checklist — brute force (rate limiting), token
  storage (httpOnly cookie vs localStorage — decide and document why),
  password policy, timing-safe comparison in password verification
  (passlib already handles this), no role/branch_id ever accepted from
  client input on registration.

**Final Validation:**
```bash
pytest backend/tests/test_auth.py -v --cov=backend/app --cov-fail-under=80
docker-compose up -d
curl -X POST localhost:8000/auth/register -H "Content-Type: application/json" \
  -d '{"email":"patient@example.com","password":"...", "full_name":"...", "dob":"...", ...}'
curl -X POST localhost:8000/auth/login -d '...'
```

---

## VALIDATION GATES

| Gate | Commands |
|------|----------|
| 1 | Keyring sign/verify round-trip (see Phase 1) |
| 2 | `ruff check backend/`, `mypy backend/app`, `npm run lint`, `npm run type-check` |
| 3 | `pytest backend/tests/test_auth.py --cov-fail-under=80`, `npm test` |
| Final | `docker-compose up -d`, register → login → me → refresh → logout → me (expect 401) |

---

## ENVIRONMENT VARIABLES (additions to `.env.example`)

```env
# JWT signing keyring (replaces bare SECRET_KEY for token signing)
JWT_SIGNING_KEYS={"k1":"generate-with-openssl-rand-base64-32"}
JWT_CURRENT_KID=k1
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7

# HMAC key for deterministic PHI-matching hashes (was sharing SECRET_KEY — split out)
HMAC_KEY=generate-a-separate-key-do-not-reuse-jwt-keys

# Rate limiting
LOGIN_RATE_LIMIT_PER_MINUTE=5
```

---

## NEXT STEP

Execute with parallel agents:
```
/execute-prp PRPs/auth-rbac-abac-prp.md
```
