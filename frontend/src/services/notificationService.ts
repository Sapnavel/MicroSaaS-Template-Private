import { api } from "./api";
import type { NotificationChannel, NotificationRecord, NotificationStatus } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the Notification & Alert Hub module
 * -- see PRPs/notification-hub-prp.md "ENDPOINTS". Every exported function
 * converts to/from the camelCase `NotificationRecord` type in
 * `types/index.ts` at the boundary, same pattern as `wardService.ts` /
 * `billingService.ts`.
 *
 * Error bodies here are plain `{"detail": ...}` -- no structured fields
 * beyond `detail` (unlike Ward's `IllegalBedStatusTransitionError`), so
 * callers just use `extractErrorMessage(error, fallback)` from
 * `utils/errors.ts`, same simpler pattern `billingService.ts` uses -- no
 * custom Error subclasses needed here.
 *
 * `branchId` handling mirrors `wardService.ts`/`billingService.ts`: a
 * caller with their own branch never sends `branch_id` (the backend derives
 * it from the session), and only a `system_admin` supplies it explicitly as
 * a query param on the GET history endpoint.
 */

interface NotificationWire {
  id: string;
  user_id: string | null;
  patient_id: string | null;
  branch_id: string | null;
  channel: NotificationChannel;
  template: string;
  payload: Record<string, unknown>;
  status: NotificationStatus;
  created_at: string;
}

export interface ListNotificationsParams {
  patientId?: string;
  userId?: string;
  status?: NotificationStatus;
  branchId?: string;
}

function toNotificationRecord(wire: NotificationWire): NotificationRecord {
  return {
    id: wire.id,
    userId: wire.user_id,
    patientId: wire.patient_id,
    branchId: wire.branch_id,
    channel: wire.channel,
    template: wire.template,
    payload: wire.payload,
    status: wire.status,
    createdAt: wire.created_at,
  };
}

export async function listNotifications(
  params: ListNotificationsParams = {},
): Promise<NotificationRecord[]> {
  const query: Record<string, string> = {};
  if (params.patientId) {
    query.patient_id = params.patientId;
  }
  if (params.userId) {
    query.user_id = params.userId;
  }
  if (params.status) {
    query.status = params.status;
  }
  if (params.branchId) {
    query.branch_id = params.branchId;
  }
  const { data } = await api.get<NotificationWire[]>("/api/v1/notifications", {
    params: Object.keys(query).length > 0 ? query : undefined,
  });
  return data.map(toNotificationRecord);
}

/** `system_admin` only -- see the PRP's ENDPOINTS table; 409s if the row is
 * not currently `status === "failed"`. */
export async function retryNotification(id: string): Promise<NotificationRecord> {
  const { data: wire } = await api.patch<NotificationWire>(`/api/v1/notifications/${id}/retry`, {});
  return toNotificationRecord(wire);
}
