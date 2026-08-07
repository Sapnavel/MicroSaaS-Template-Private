import { BrowserRouter, Link, Route, Routes } from "react-router-dom";

import ProtectedRoute from "./components/auth/ProtectedRoute";
import { AuthProvider } from "./context/AuthContext";
import { useAuth } from "./hooks/useAuth";
import type { Role } from "./types";
import BedMatrixPage from "./pages/BedMatrixPage";
import ClaimsPage from "./pages/ClaimsPage";
import ConsultationPage from "./pages/ConsultationPage";
import DuplicateReviewPage from "./pages/DuplicateReviewPage";
import ExecutiveDashboardPage from "./pages/ExecutiveDashboardPage";
import InvoicePage from "./pages/InvoicePage";
import LabOrderPage from "./pages/LabOrderPage";
import LabWorklistPage from "./pages/LabWorklistPage";
import LoginPage from "./pages/LoginPage";
import NotificationHistoryPage from "./pages/NotificationHistoryPage";
import OTSchedulePage from "./pages/OTSchedulePage";
import PatientLoginPage from "./pages/PatientLoginPage";
import PatientRegisterPage from "./pages/PatientRegisterPage";
import PatientSearchPage from "./pages/PatientSearchPage";
import PharmacyDispensePage from "./pages/PharmacyDispensePage";
import PharmacyInventoryPage from "./pages/PharmacyInventoryPage";
import PrescriptionPage from "./pages/PrescriptionPage";
import QueueBoardPage from "./pages/QueueBoardPage";
import RegisterPage from "./pages/RegisterPage";

/**
 * Module directory for the home route, filtered to the routes each role can
 * actually reach per `<Route>`'s own `allowedRoles` below -- kept as a plain
 * array here so this list and each route's gate can be eyeballed side by
 * side rather than duplicating role logic. Only top-level, param-free pages
 * are listed (e.g. not `/consultations/:id`, which is only ever reached from
 * an existing appointment/consultation elsewhere, never as a standalone
 * destination).
 */
interface ModuleLink {
  path: string;
  label: string;
  description: string;
  icon: string;
  category: string;
  roles: Role[];
}

const MODULE_LINKS: ModuleLink[] = [
  {
    path: "/patients/search",
    label: "Patient search",
    description: "Look up an existing patient by name, DOB, or phone.",
    icon: "🔍",
    category: "Patients",
    roles: ["front_desk", "nurse", "doctor", "system_admin"],
  },
  {
    path: "/patients/register",
    label: "Patient registration",
    description: "Register a new patient and issue an MRN.",
    icon: "📝",
    category: "Patients",
    roles: ["front_desk", "nurse", "doctor", "system_admin"],
  },
  {
    path: "/patients/duplicates",
    label: "Duplicate review",
    description: "Resolve possible duplicate patient records.",
    icon: "🧬",
    category: "Patients",
    roles: ["front_desk", "system_admin"],
  },
  {
    path: "/queue/board",
    label: "Queue board",
    description: "Live check-in queue and digital token board.",
    icon: "🎫",
    category: "Clinical",
    roles: ["front_desk", "nurse", "doctor", "system_admin"],
  },
  {
    path: "/lab/orders/new",
    label: "New lab order",
    description: "Order lab tests for a patient.",
    icon: "🧪",
    category: "Clinical",
    roles: ["doctor", "system_admin"],
  },
  {
    path: "/lab/worklist",
    label: "Lab worklist",
    description: "Process pending lab samples and results.",
    icon: "🔬",
    category: "Clinical",
    roles: ["nurse", "lab_tech", "system_admin"],
  },
  {
    path: "/pharmacy/inventory",
    label: "Pharmacy inventory",
    description: "Stock levels, reorder thresholds, batch receiving.",
    icon: "💊",
    category: "Pharmacy",
    roles: ["pharmacist", "system_admin"],
  },
  {
    path: "/pharmacy/dispense",
    label: "Pharmacy dispense",
    description: "Dispense a prescription against current stock.",
    icon: "💉",
    category: "Pharmacy",
    roles: ["pharmacist", "system_admin"],
  },
  {
    path: "/wards/beds",
    label: "Bed matrix",
    description: "Ward-by-ward bed availability and status.",
    icon: "🛏️",
    category: "Wards",
    roles: ["front_desk", "nurse", "doctor", "system_admin"],
  },
  {
    path: "/wards/ot-schedule",
    label: "OT schedule",
    description: "Operating theatre bookings and scheduling.",
    icon: "🗓️",
    category: "Wards",
    roles: ["doctor", "nurse", "system_admin"],
  },
  {
    path: "/billing/invoices",
    label: "Invoices",
    description: "View and create invoices for a patient.",
    icon: "🧾",
    category: "Billing",
    roles: ["front_desk", "billing_admin", "system_admin"],
  },
  {
    path: "/billing/claims",
    label: "Insurance claims",
    description: "Track and adjudicate insurance claims.",
    icon: "📄",
    category: "Billing",
    roles: ["billing_admin", "system_admin"],
  },
  {
    path: "/notifications/history",
    label: "Notification history",
    description: "Delivery status of outbound alerts and reminders.",
    icon: "🔔",
    category: "Admin",
    roles: ["front_desk", "system_admin"],
  },
  {
    path: "/dashboard/executive",
    label: "Executive dashboard",
    description: "Occupancy, wait times, revenue, and stock alerts.",
    icon: "📊",
    category: "Admin",
    roles: ["system_admin"],
  },
];

const MODULE_CATEGORY_ORDER = ["Patients", "Clinical", "Pharmacy", "Wards", "Billing", "Admin"];

function HomePage(): JSX.Element {
  const { user } = useAuth();
  const links = MODULE_LINKS.filter((link) => user !== null && link.roles.includes(user.role));
  const categories = MODULE_CATEGORY_ORDER.filter((category) =>
    links.some((link) => link.category === category),
  );

  return (
    <main className="page-container">
      <h1>Modules</h1>
      {links.length === 0 && <p className="empty-state">No modules are available for your role.</p>}
      {categories.map((category) => (
        <section key={category} className="module-section">
          <h2 className="module-section-title">{category}</h2>
          <div className="module-grid">
            {links
              .filter((link) => link.category === category)
              .map((link) => (
                <Link key={link.path} to={link.path} className="module-card">
                  <span className="module-card-icon" aria-hidden="true">
                    {link.icon}
                  </span>
                  <span className="module-card-label">{link.label}</span>
                  <span className="module-card-description">{link.description}</span>
                </Link>
              ))}
          </div>
        </section>
      ))}
    </main>
  );
}

// Rendered inline per the auth PRP — every other route target already has
// (or will get) its own page file, but this one is just a static message.
function UnauthorizedPage(): JSX.Element {
  return (
    <main className="auth-page">
      <h1>Not authorized</h1>
      <p>You do not have permission to view this page.</p>
    </main>
  );
}

export default function App(): JSX.Element {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/patient/login" element={<PatientLoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/unauthorized" element={<UnauthorizedPage />} />
          <Route
            path="/patients/search"
            element={
              <ProtectedRoute allowedRoles={["front_desk", "nurse", "doctor", "system_admin"]}>
                <PatientSearchPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/patients/register"
            element={
              <ProtectedRoute allowedRoles={["front_desk", "nurse", "doctor", "system_admin"]}>
                <PatientRegisterPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/patients/duplicates"
            element={
              <ProtectedRoute allowedRoles={["front_desk", "system_admin"]}>
                <DuplicateReviewPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/consultations/:id"
            element={
              <ProtectedRoute allowedRoles={["doctor", "nurse", "system_admin"]}>
                <ConsultationPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/consultations/:id/prescriptions"
            element={
              <ProtectedRoute allowedRoles={["doctor", "system_admin"]}>
                <PrescriptionPage />
              </ProtectedRoute>
            }
          />
          {/* Lab module -- see PRPs/lab-module-prp.md "ENDPOINTS". Create
              (/new) is gated tighter than view (/:id): POST /lab/orders is
              doctor(own)/system_admin only, while GET /lab/orders/{id} also
              allows nurse/lab_tech, who may land here from a link on the
              worklist just to look up a specific order -- see the judgment
              call documented in LabOrderPage.tsx. */}
          <Route
            path="/lab/orders/new"
            element={
              <ProtectedRoute allowedRoles={["doctor", "system_admin"]}>
                <LabOrderPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/lab/orders/:id"
            element={
              <ProtectedRoute allowedRoles={["doctor", "nurse", "lab_tech", "system_admin"]}>
                <LabOrderPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/lab/worklist"
            element={
              <ProtectedRoute allowedRoles={["nurse", "lab_tech", "system_admin"]}>
                <LabWorklistPage />
              </ProtectedRoute>
            }
          />
          {/* Pharmacy Inventory module -- see PRPs/pharmacy-module-prp.md
              "ENDPOINTS". Both routes share the same auth: pharmacist for
              their own branch, system_admin for any branch (explicit
              branch_id, since they have none of their own). */}
          <Route
            path="/pharmacy/inventory"
            element={
              <ProtectedRoute allowedRoles={["pharmacist", "system_admin"]}>
                <PharmacyInventoryPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/pharmacy/dispense"
            element={
              <ProtectedRoute allowedRoles={["pharmacist", "system_admin"]}>
                <PharmacyDispensePage />
              </ProtectedRoute>
            }
          />
          {/* Real-Time Queue & Digital Token module -- see
              PRPs/realtime-queue-prp.md "ENDPOINTS" / design decision #6.
              front_desk/nurse/system_admin get check-in + status-transition
              controls; doctor is read-only (QueueBoardPage.tsx gates every
              action button and the check-in form by role, same discipline
              as BedMatrixPage.tsx/InvoicePage.tsx). */}
          <Route
            path="/queue/board"
            element={
              <ProtectedRoute allowedRoles={["front_desk", "nurse", "doctor", "system_admin"]}>
                <QueueBoardPage />
              </ProtectedRoute>
            }
          />
          {/* Ward, Bed & OT module -- see
              PRPs/ward-bed-ot-module-prp.md "ENDPOINTS". The bed matrix is
              viewable by front_desk too (view-only -- BedMatrixPage.tsx
              gates every action button by role), while OT scheduling has no
              front_desk role at all (front_desk never appears in either of
              its endpoint auth lists). */}
          <Route
            path="/wards/beds"
            element={
              <ProtectedRoute allowedRoles={["front_desk", "nurse", "doctor", "system_admin"]}>
                <BedMatrixPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/wards/ot-schedule"
            element={
              <ProtectedRoute allowedRoles={["doctor", "nurse", "system_admin"]}>
                <OTSchedulePage />
              </ProtectedRoute>
            }
          />
          {/* Billing, Ledger & Insurance Claims module -- see
              PRPs/billing-module-prp.md "ENDPOINTS". Invoices are viewable
              by front_desk too (read-only -- InvoicePage.tsx gates every
              action button by role, same discipline as BedMatrixPage.tsx),
              while claim adjudication has no front_desk role at all (never
              appears in that endpoint's auth list). */}
          <Route
            path="/billing/invoices"
            element={
              <ProtectedRoute allowedRoles={["front_desk", "billing_admin", "system_admin"]}>
                <InvoicePage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/billing/claims"
            element={
              <ProtectedRoute allowedRoles={["billing_admin", "system_admin"]}>
                <ClaimsPage />
              </ProtectedRoute>
            }
          />
          {/* Notification & Alert Hub module -- see
              PRPs/notification-hub-prp.md "ENDPOINTS". front_desk is
              view-only (NotificationHistoryPage.tsx gates the Retry button
              by role, same discipline as InvoicePage.tsx/BedMatrixPage.tsx);
              PATCH .../retry is system_admin only. */}
          <Route
            path="/notifications/history"
            element={
              <ProtectedRoute allowedRoles={["front_desk", "system_admin"]}>
                <NotificationHistoryPage />
              </ProtectedRoute>
            }
          />
          {/* Executive & Operational Dashboard module -- see
              PRPs/executive-dashboard-prp.md "MODULE OVERVIEW" design
              decision #1. Unlike every prior module, the entire route is
              system_admin-only -- no other role ever appears in any of its
              five endpoints' auth lists, so there is no internal
              role-gating inside ExecutiveDashboardPage.tsx itself, just
              this route-level ProtectedRoute. This is the final module in
              docs/ARCHITECTURE.md §9's build-out order. */}
          <Route
            path="/dashboard/executive"
            element={
              <ProtectedRoute allowedRoles={["system_admin"]}>
                <ExecutiveDashboardPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <HomePage />
              </ProtectedRoute>
            }
          />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
