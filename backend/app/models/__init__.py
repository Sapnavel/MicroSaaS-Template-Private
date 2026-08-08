"""SQLAlchemy models.

Core scheduling-critical modules are fully modeled here: tenancy, auth,
patients, resources, appointments, triage, queue, audit. Clinical
Consultation & Smart Prescription (database/schema.sql §7) was added by
BACKEND-AGENT, PRPs/clinical-consultation-prescription-prp.md Phase 1. Lab
(database/schema.sql §8) was added by BACKEND-AGENT,
PRPs/lab-module-prp.md Phase 1 (models + state machine only -- schemas,
service, and router are Phase 2). Pharmacy Inventory (database/schema.sql
§9) was added by BACKEND-AGENT, PRPs/pharmacy-module-prp.md Phase 1 (models
+ FEFO dispense engine only -- schemas, service, and router are Phase 2).
Ward/Bed/OT (database/schema.sql §10) was added by BACKEND-AGENT,
PRPs/ward-bed-ot-module-prp.md Phase 1 (models + admit/discharge/transfer/OT
booking engine only -- schemas, service, and router are Phase 2). Billing /
Ledger / Insurance Claims (database/schema.sql §11) was added by
BACKEND-AGENT, PRPs/billing-module-prp.md Phase 1 (models + invoice/claim
engine only -- schemas, service, and router are Phase 2). Notification &
Alert Hub (database/schema.sql §12, + a `branch_id` column this PRP added)
was added by BACKEND-AGENT, PRPs/notification-hub-prp.md Phase 1 (models +
provider abstraction + event-consuming engine only -- schemas, HTTP
service/router, and the `pika` consumer worker are Phase 2).

All modules scaffolded in database/schema.sql now have ORM models.
"""

from app.models.appointment import (  # noqa: F401
    Appointment,
    AppointmentEquipmentLock,
    AppointmentRoomLock,
    AppointmentSeries,
    AppointmentStatus,
    RecurrenceFrequency,
    WaitlistEntry,
    WaitlistStatus,
)
from app.models.audit import AuditLog  # noqa: F401
from app.models.billing import ClaimState, InsuranceClaim, Invoice, InvoiceItem, InvoiceStatus  # noqa: F401
from app.models.consultation import (  # noqa: F401
    Consultation,
    Diagnosis,
    Drug,
    DrugInteraction,
    PatientAllergy,
    Prescription,
    PrescriptionItem,
)
from app.models.lab import LabOrder, LabOrderStatus, LabSample  # noqa: F401
from app.models.notification import Notification, NotificationChannel, NotificationStatus  # noqa: F401
from app.models.patient import Patient, PatientDuplicateCandidate  # noqa: F401
from app.models.pharmacy import InventoryBatch, InventoryItem  # noqa: F401
from app.models.queue import QueueToken, TokenStatus  # noqa: F401
from app.models.resource import Doctor, DoctorShift, Equipment, Room, Specialty  # noqa: F401
from app.models.tenant import Branch, HospitalGroup  # noqa: F401
from app.models.token import RefreshToken  # noqa: F401
from app.models.triage import TriageLevel  # noqa: F401
from app.models.user import User, UserRole  # noqa: F401
from app.models.ward import Admission, Bed, BedStatus, OTSchedule, Ward  # noqa: F401
