import { useState } from "react";

import BranchSelect from "../components/selects/BranchSelect";
import {
  getNoShowRate,
  getOccupancy,
  getRevenue,
  getStockAlerts,
  getWaitTimes,
} from "../services/dashboardService";
import type {
  DailyRevenue,
  DepartmentWaitTime,
  ExpiringBatch,
  LowStockItem,
  NoShowRate,
  WardOccupancy,
} from "../types";
import { extractErrorMessage } from "../utils/errors";

const DEFAULT_DAYS = 30;

interface DashboardData {
  occupancy: WardOccupancy[];
  waitTimes: DepartmentWaitTime[];
  revenue: DailyRevenue[];
  noShowRates: NoShowRate[];
  lowStock: LowStockItem[];
  expiringSoon: ExpiringBatch[];
}

/**
 * Executive & Operational Dashboard -- see
 * PRPs/executive-dashboard-prp.md "MODULE OVERVIEW" / "ENDPOINTS". Unlike
 * every prior page, this entire route is `system_admin`-only (design
 * decision #1) -- there is no other role that ever reaches this component,
 * so (per the FRONTEND-AGENT brief) there is no internal role-gating logic
 * here at all; `App.tsx`'s `ProtectedRoute allowedRoles={["system_admin"]}`
 * is the entire access control surface for this page.
 *
 * Judgment calls (documented per FRONTEND-AGENT instructions):
 *  - **Branch filter**: a `<BranchSelect allowAll>` (frontend
 *    UUID-to-dropdown conversion follow-up). Left blank ("All branches"),
 *    every endpoint aggregates across all branches (per design decision #1
 *    -- system_admin has no branch of their own, so unlike other
 *    system_admin pages, an *empty* branch ID is itself a valid, meaningful
 *    "all branches" query here, not a blocked state).
 *  - **One shared "days" window**, not five independent ones: the PRP's
 *    endpoints default to 7 days (wait-times) or 30 days (revenue,
 *    no-shows) independently, but exposing three separate day-window
 *    inputs for a single "load everything" action added UI clutter with
 *    no real benefit for an internal admin page -- one shared numeric
 *    input (default 30) is sent as `days` to wait-times/revenue/no-shows;
 *    occupancy and stock-alerts have no `days` param at all per the API
 *    contract, so it's simply not sent to those two.
 *  - **One combined loading/error state**, not five independent ones --
 *    all five endpoints are fetched by a single "Refresh" action; a
 *    partial failure (e.g. one endpoint 500s) is surfaced as one error
 *    banner rather than five separately-tracked states, since this is a
 *    single-role internal admin page loading everything together, not a
 *    multi-actor collaborative surface (per the FRONTEND-AGENT brief).
 *  - **Stock alerts rendering**: split into two sub-tables (Low Stock,
 *    Expiring Soon) as `data-table`s, mirroring the same two shapes
 *    `PharmacyInventoryPage.tsx` renders (that page uses `list-plain`
 *    cards instead of tables for these two lists; this page uses
 *    `data-table` throughout for visual consistency with its other four
 *    sections instead, since this page's five sections are meant to read
 *    as one uniform report rather than a form-heavy workflow page).
 *  - **No auto-load on mount**: since an empty branch ID is a valid "all
 *    branches" query (unlike other system_admin pages where a blank
 *    branch blocks loading), the user must still click "Refresh" once to
 *    trigger the first load -- auto-fetching on mount for a cross-branch,
 *    potentially-expensive aggregate report felt like the wrong default
 *    for an admin who may want to set a branch filter first.
 */
export default function ExecutiveDashboardPage(): JSX.Element {
  const [branchId, setBranchId] = useState<string>("");
  const [days, setDays] = useState<string>(String(DEFAULT_DAYS));

  const [data, setData] = useState<DashboardData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);

  const load = async (): Promise<void> => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const trimmedBranchId = branchId.trim() || undefined;
      const parsedDays = Number(days);
      const effectiveDays = Number.isInteger(parsedDays) && parsedDays > 0 ? parsedDays : undefined;

      const [occupancy, waitTimes, revenue, noShowRates, stockAlerts] = await Promise.all([
        getOccupancy({ branchId: trimmedBranchId }),
        getWaitTimes({ branchId: trimmedBranchId, days: effectiveDays }),
        getRevenue({ branchId: trimmedBranchId, days: effectiveDays }),
        getNoShowRate({ branchId: trimmedBranchId, days: effectiveDays }),
        getStockAlerts({ branchId: trimmedBranchId }),
      ]);

      setData({
        occupancy,
        waitTimes,
        revenue,
        noShowRates,
        lowStock: stockAlerts.lowStock,
        expiringSoon: stockAlerts.expiringSoon,
      });
    } catch (error) {
      setLoadError(extractErrorMessage(error, "Could not load the executive dashboard."));
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <main className="page-container">
      <h1>Executive dashboard</h1>

      <div className="section-header">
        <h2>Filters</h2>
      </div>
      <div className="patient-form">
        <label className="auth-label" htmlFor="dashboard-branch-id">
          Branch
        </label>
        <BranchSelect id="dashboard-branch-id" value={branchId} onChange={setBranchId} allowAll />
        <label className="auth-label" htmlFor="dashboard-days">
          Window (days)
        </label>
        <input
          id="dashboard-days"
          className="auth-input"
          type="number"
          min={1}
          value={days}
          onChange={(event) => setDays(event.target.value)}
        />
        <p className="field-hint">
          Applies to wait times, revenue, and no-show rate -- occupancy and stock alerts are always
          current.
        </p>
        <button className="auth-submit" type="button" disabled={isLoading} onClick={() => void load()}>
          {isLoading ? "Loading..." : "Refresh"}
        </button>
      </div>

      {loadError !== null && (
        <p className="auth-error" role="alert">
          {loadError}
        </p>
      )}

      {data === null && !isLoading && loadError === null && (
        <p className="empty-state">Click "Refresh" to load the dashboard.</p>
      )}

      {data !== null && (
        <>
          <div className="section-header">
            <h2>Ward occupancy</h2>
          </div>
          {data.occupancy.length === 0 ? (
            <p className="empty-state">No data for the selected window.</p>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Branch</th>
                  <th>Ward</th>
                  <th>Type</th>
                  <th>Total beds</th>
                  <th>Occupied</th>
                  <th>Occupancy</th>
                </tr>
              </thead>
              <tbody>
                {data.occupancy.map((ward) => (
                  <tr key={ward.wardId}>
                    <td>{ward.branchId}</td>
                    <td>{ward.wardName}</td>
                    <td>{ward.wardType}</td>
                    <td>{ward.totalBeds}</td>
                    <td>{ward.occupiedBeds}</td>
                    <td>{(ward.occupancyPct * 100).toFixed(1)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="section-header">
            <h2>Department wait times</h2>
          </div>
          {data.waitTimes.length === 0 ? (
            <p className="empty-state">No data for the selected window.</p>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Branch</th>
                  <th>Department</th>
                  <th>Avg. wait (min)</th>
                  <th>Sample size</th>
                </tr>
              </thead>
              <tbody>
                {data.waitTimes.map((entry, index) => (
                  <tr key={`${entry.branchId}-${entry.departmentId ?? "none"}-${index}`}>
                    <td>{entry.branchId}</td>
                    <td>{entry.departmentName ?? "Unspecified"}</td>
                    <td>{entry.avgWaitMinutes.toFixed(1)}</td>
                    <td>{entry.sampleSize}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="section-header">
            <h2>Daily revenue</h2>
          </div>
          {data.revenue.length === 0 ? (
            <p className="empty-state">No data for the selected window.</p>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Branch</th>
                  <th>Day</th>
                  <th>Invoices</th>
                  <th>Gross billed</th>
                  <th>Collected</th>
                  <th>Outstanding</th>
                </tr>
              </thead>
              <tbody>
                {data.revenue.map((row, index) => (
                  <tr key={`${row.branchId}-${row.day}-${index}`}>
                    <td>{row.branchId}</td>
                    <td>{new Date(row.day).toLocaleDateString()}</td>
                    <td>{row.invoiceCount}</td>
                    <td>{row.grossBilled.toFixed(2)}</td>
                    <td>{row.collected.toFixed(2)}</td>
                    <td>{row.outstanding.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="section-header">
            <h2>No-show rate</h2>
          </div>
          {data.noShowRates.length === 0 ? (
            <p className="empty-state">No data for the selected window.</p>
          ) : (
            <>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Branch</th>
                    <th>Considered</th>
                    <th>No-shows</th>
                    <th>Actual rate</th>
                    <th>Avg. predicted risk</th>
                  </tr>
                </thead>
                <tbody>
                  {data.noShowRates.map((row, index) => (
                    <tr key={`${row.branchId}-${index}`}>
                      <td>{row.branchId}</td>
                      <td>{row.totalConsidered}</td>
                      <td>{row.noShowCount}</td>
                      <td>{(row.noShowRate * 100).toFixed(1)}%</td>
                      <td>
                        {row.avgPredictedNoShowRisk !== null
                          ? `${(row.avgPredictedNoShowRisk * 100).toFixed(1)}%`
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="field-hint">{data.noShowRates[0].noShowRiskScoreNote}</p>
            </>
          )}

          <div className="section-header">
            <h2>Stock alerts</h2>
          </div>
          <h3>Low stock</h3>
          {data.lowStock.length === 0 ? (
            <p className="empty-state">No data for the selected window.</p>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Drug</th>
                  <th>Branch</th>
                  <th>Current qty</th>
                  <th>Reorder threshold</th>
                </tr>
              </thead>
              <tbody>
                {data.lowStock.map((item) => (
                  <tr key={item.inventoryItemId}>
                    <td>{item.drugName}</td>
                    <td>{item.branchId}</td>
                    <td>{item.currentQuantity}</td>
                    <td>{item.reorderThreshold}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h3>Expiring soon</h3>
          {data.expiringSoon.length === 0 ? (
            <p className="empty-state">No data for the selected window.</p>
          ) : (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Drug</th>
                  <th>Batch</th>
                  <th>Quantity</th>
                  <th>Expiry date</th>
                </tr>
              </thead>
              <tbody>
                {data.expiringSoon.map((batch) => (
                  <tr key={batch.batchId}>
                    <td>{batch.drugName}</td>
                    <td>{batch.batchNumber}</td>
                    <td>{batch.quantity}</td>
                    <td>{batch.expiryDate}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </main>
  );
}
