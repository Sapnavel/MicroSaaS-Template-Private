import { api } from "./api";
import type { Room } from "../types";

/**
 * This file is the only place in the frontend allowed to know about the
 * backend's snake_case wire format for the room lookup endpoint. Converts
 * to/from the camelCase `Room` type in `types/index.ts` at the boundary,
 * same pattern as `doctorService.ts`.
 *
 * Backs `components/selects/RoomSelect.tsx`. `branchId` is required by the
 * backend (this list is always branch-scoped); `roomType` narrows it
 * further and is omitted from the query entirely when not supplied.
 */

interface RoomWire {
  id: string;
  name: string;
  room_type: string;
  branch_id: string;
}

function toRoom(wire: RoomWire): Room {
  return {
    id: wire.id,
    name: wire.name,
    roomType: wire.room_type,
    branchId: wire.branch_id,
  };
}

export interface ListRoomsParams {
  branchId: string;
  roomType?: string;
}

/** `GET /api/v1/rooms?branch_id=&room_type=(optional)`. */
export async function listRooms(params: ListRoomsParams): Promise<Room[]> {
  const query: Record<string, string> = { branch_id: params.branchId };
  if (params.roomType) {
    query.room_type = params.roomType;
  }
  const { data } = await api.get<RoomWire[]>("/api/v1/rooms", { params: query });
  return data.map(toRoom);
}
