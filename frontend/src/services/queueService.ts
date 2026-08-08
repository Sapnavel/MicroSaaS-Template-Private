import { api } from "./api";
import type { QueueToken, TokenStatus } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the Real-Time Queue & Digital Token
 * module -- see PRPs/realtime-queue-prp.md "ENDPOINTS". Every exported
 * function converts to/from the camelCase `QueueToken` type in
 * `types/index.ts` at the boundary, same pattern as `notificationService.ts`/
 * `billingService.ts`.
 *
 * Error bodies here are plain `{"detail": ...}` (a 404 for an unknown token,
 * a 409 for an illegal status transition, a 422 for a malformed check-in
 * body) -- no structured fields beyond `detail` (unlike Ward's
 * `IllegalBedStatusTransitionError`), so callers just use
 * `extractErrorMessage(error, fallback)` from `utils/errors.ts`, same
 * simpler pattern `notificationService.ts`/`billingService.ts` use -- no
 * custom Error subclasses needed here.
 */

interface QueueTokenWire {
  id: string;
  branch_id: string;
  appointment_id: string | null;
  department_id: number | null;
  token_number: number;
  status: TokenStatus;
  checked_in_at: string;
  called_at: string | null;
  completed_at: string | null;
  estimated_wait_minutes: number | null;
  is_priority: boolean;
}

function toQueueToken(wire: QueueTokenWire): QueueToken {
  return {
    id: wire.id,
    branchId: wire.branch_id,
    appointmentId: wire.appointment_id,
    departmentId: wire.department_id,
    tokenNumber: wire.token_number,
    status: wire.status,
    checkedInAt: wire.checked_in_at,
    calledAt: wire.called_at,
    completedAt: wire.completed_at,
    estimatedWaitMinutes: wire.estimated_wait_minutes,
    isPriority: wire.is_priority,
  };
}

/** `POST /api/v1/queue/check-in` with `{appointment_id}` -- branch/department
 * are derived server-side from the appointment, never supplied here (see
 * design decision #1). `isPriority` (HMS Project Completion Prompt gap,
 * "emergency queue priority") is OR-ed server-side with the appointment's
 * own `is_emergency` flag -- pass `true` here to mark priority even when
 * the appointment itself wasn't booked as an emergency (e.g. the patient's
 * condition worsened after arrival); omit/`false` otherwise, an
 * emergency-booked appointment still gets priority automatically. */
export async function checkInWithAppointment(
  appointmentId: string,
  isPriority: boolean = false,
): Promise<QueueToken> {
  const { data: wire } = await api.post<QueueTokenWire>("/api/v1/queue/check-in", {
    appointment_id: appointmentId,
    is_priority: isPriority,
  });
  return toQueueToken(wire);
}

/** `POST /api/v1/queue/check-in` with `{branch_id, department_id}` -- a
 * walk-in token with no appointment (and therefore no patient) linkage. See
 * design decision #1's "real, schema-imposed limitation" note. `isPriority`
 * -- see `checkInWithAppointment`'s docstring. */
export async function checkInWalkIn(
  branchId: string,
  departmentId: number,
  isPriority: boolean = false,
): Promise<QueueToken> {
  const { data: wire } = await api.post<QueueTokenWire>("/api/v1/queue/check-in", {
    branch_id: branchId,
    department_id: departmentId,
    is_priority: isPriority,
  });
  return toQueueToken(wire);
}

/** `PATCH /api/v1/queue/{token_id}/status`. 404 if the token doesn't exist,
 * 409 if `status` isn't a legal transition from the token's current status
 * (see the state machine in the PRP's design decision #3). */
export async function updateTokenStatus(tokenId: string, status: TokenStatus): Promise<QueueToken> {
  const { data: wire } = await api.patch<QueueTokenWire>(`/api/v1/queue/${tokenId}/status`, {
    status,
  });
  return toQueueToken(wire);
}

/** `GET /api/v1/queue/board` -- the initial snapshot for a page load, before
 * any WebSocket message has arrived. `branchId` is required by the backend;
 * `departmentId` is optional there, but `QueueBoardPage.tsx` always supplies
 * it since the WebSocket subscription needs a concrete department to scope
 * to anyway. */
export async function getQueueBoard(branchId: string, departmentId?: number): Promise<QueueToken[]> {
  const params: Record<string, string | number> = { branch_id: branchId };
  if (departmentId !== undefined) {
    params.department_id = departmentId;
  }
  const { data } = await api.get<QueueTokenWire[]>("/api/v1/queue/board", { params });
  return data.map(toQueueToken);
}

/** Live update pushed by `GET /ws/queue/{branch_id}/{department_id}` --
 * shaped per the PRP's "existing WebSocket endpoint" contract, camelCase at
 * the boundary same as every HTTP response above. `tokenId` is carried
 * alongside the (possibly partial) `QueueToken` fields so a caller can key
 * off it without assuming every field is present. */
export type QueueBoardUpdate = Partial<QueueToken> & { tokenId: string };

interface QueueBoardUpdateWire {
  token_id: string;
  status: TokenStatus;
  token_number: number;
  checked_in_at: string;
  called_at: string | null;
  completed_at: string | null;
  estimated_wait_minutes: number | null;
  appointment_id: string | null;
  is_priority: boolean;
}

function toQueueBoardUpdate(wire: QueueBoardUpdateWire): QueueBoardUpdate {
  return {
    tokenId: wire.token_id,
    status: wire.status,
    tokenNumber: wire.token_number,
    checkedInAt: wire.checked_in_at,
    calledAt: wire.called_at,
    completedAt: wire.completed_at,
    estimatedWaitMinutes: wire.estimated_wait_minutes,
    appointmentId: wire.appointment_id,
    isPriority: wire.is_priority,
  };
}

/** Derives the `ws://`/`wss://` origin from the same `VITE_API_URL` env var
 * `services/api.ts` uses for its axios base URL, so dev/prod both point at
 * the right host without hardcoding `localhost:8000`. */
function toWebSocketOrigin(httpUrl: string): string {
  if (httpUrl.startsWith("https://")) {
    return `wss://${httpUrl.slice("https://".length)}`;
  }
  if (httpUrl.startsWith("http://")) {
    return `ws://${httpUrl.slice("http://".length)}`;
  }
  return httpUrl;
}

const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const WS_ORIGIN = toWebSocketOrigin(API_URL);

/**
 * Subscribes to the existing `GET /ws/queue/{branch_id}/{department_id}`
 * broadcast channel (not part of this PRP, reused as-is -- design decision
 * #4). Returns an unsubscribe function that closes the socket; callers
 * (`QueueBoardPage.tsx`) invoke it from a `useEffect` cleanup whenever
 * `branchId`/`departmentId` change or the component unmounts, so at most one
 * socket is ever open per (branch, department) pair.
 *
 * No auto-reconnect is implemented here: this mirrors the fact that nothing
 * else in this codebase's frontend does WebSocket reconnection today (this
 * is the first module to use a WebSocket at all), and the PRP's own scope is
 * "reused as-is" for the channel, not "add reconnect logic" for it. If the
 * socket drops, the board simply stops receiving live patches until the
 * caller reloads the snapshot via `getQueueBoard` (e.g. by clicking
 * "Refresh" again) -- a documented, deliberate scope boundary, not an
 * oversight.
 */
export function subscribeToQueueBoard(
  branchId: string,
  departmentId: number,
  onMessage: (update: QueueBoardUpdate) => void,
): () => void {
  const socket = new WebSocket(`${WS_ORIGIN}/ws/queue/${branchId}/${departmentId}`);

  socket.onmessage = (event: MessageEvent<string>) => {
    try {
      const wire = JSON.parse(event.data) as QueueBoardUpdateWire;
      onMessage(toQueueBoardUpdate(wire));
    } catch {
      // Malformed/unexpected message -- ignore rather than crash the page.
    }
  };

  return () => {
    socket.close();
  };
}
