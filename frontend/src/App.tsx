import { BrowserRouter, Route, Routes } from "react-router-dom";

import ProtectedRoute from "./components/auth/ProtectedRoute";
import { AuthProvider } from "./context/AuthContext";
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
import RegisterPage from "./pages/RegisterPage";

// Module pages are scaffolded per docs/ARCHITECTURE.md build-out order.
// Only a placeholder home route exists until further modules are implemented.
function HomePage(): JSX.Element {
  return (
    <main>
      <h1>Hospital Management System</h1>
      <p>Frontend scaffold — pages are added module by module.</p>
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
