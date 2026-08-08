import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.rate_limit import check_write_rate_limit
from app.core.security import decode_token
from app.routers import (
    admin,
    appointments,
    auth,
    billing,
    consultations,
    dashboard,
    directory,
    emergency,
    lab,
    notifications,
    patient_portal,
    patients,
    pharmacy,
    queue,
    waitlist,
    wards,
)
from app.websocket import queue_board

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Hospital Management & Appointment Booking System", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# HMS Project Completion Prompt gap ("secure headers"). Applied to every
# response. `/docs`/`/redoc` are FastAPI's own self-contained HTML pages
# (Swagger UI / ReDoc, loaded from a CDN by default) -- a strict
# `default-src 'none'` CSP would break them, so those two paths (plus the
# `/openapi.json` they fetch) get no CSP at all rather than a hand-tuned
# CDN allowlist that would just need updating every time FastAPI's docs
# dependency changes. Every other route on this API only ever returns JSON
# (or, for `GET /billing/invoices/{id}/receipt`, a PDF `Response` -- see
# routers/billing.py) -- neither needs to load scripts/styles/frames, so
# `default-src 'none'` is the correct, non-breaking default there.
_NO_CSP_PATHS = {"/docs", "/redoc", "/openapi.json"}


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    if request.url.path not in _NO_CSP_PATHS:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _write_rate_limit(request: Request, call_next):
    """HMS Project Completion Prompt gap ("rate limiting where
    appropriate") -- see `config.py`'s `write_rate_limit_per_minute` and
    `core/rate_limit.py`'s `check_write_rate_limit` docstrings for the full
    reasoning. Keyed per authenticated user (decoded best-effort straight
    from the bearer token -- this middleware runs before FastAPI's own
    `Depends(get_current_user)` resolves, so it can't reuse that dependency;
    a token that fails to decode here still hits the real auth dependency
    downstream and gets a proper 401 there, this middleware only needs
    *a* key to rate-limit by) or per client IP for unauthenticated/
    unparseable requests.
    """
    if request.method in _MUTATING_METHODS:
        key = f"ip:{request.client.host}" if request.client else "ip:unknown"
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:]
            try:
                payload = decode_token(token)
                subject = payload.get("sub")
                if subject:
                    key = f"user:{subject}"
            except HTTPException:
                pass
        check_write_rate_limit(key)
    return await call_next(request)

# Implemented
app.include_router(appointments.router)
app.include_router(emergency.router)
app.include_router(queue_board.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(patients.router)
app.include_router(consultations.router)
app.include_router(consultations.patient_allergy_router)
app.include_router(lab.router)
app.include_router(pharmacy.router)
app.include_router(wards.router)
app.include_router(billing.router)
app.include_router(notifications.router)
app.include_router(dashboard.router)
app.include_router(queue.router)
app.include_router(directory.router)
app.include_router(patient_portal.router)
app.include_router(waitlist.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
