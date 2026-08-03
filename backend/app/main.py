import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import (
    admin,
    appointments,
    auth,
    billing,
    consultations,
    dashboard,
    emergency,
    lab,
    notifications,
    patients,
    pharmacy,
    queue,
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
