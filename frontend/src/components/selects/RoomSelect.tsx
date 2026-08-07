import { useEffect, useState } from "react";

import { listRooms } from "../../services/roomService";
import type { Room } from "../../types";
import { extractErrorMessage } from "../../utils/errors";

export interface RoomSelectProps {
  value: string;
  onChange: (value: string) => void;
  branchId: string;
  roomType?: string;
  id?: string;
  className?: string;
  disabled?: boolean;
  required?: boolean;
}

/**
 * Reusable dropdown replacement for a raw "type a room UUID into a text
 * box" field. Re-fetches `GET /api/v1/rooms` (via `roomService.ts`)
 * whenever `branchId`/`roomType` change -- same dependent-fetch shape as
 * `DoctorSelect`.
 */
export default function RoomSelect({
  value,
  onChange,
  branchId,
  roomType,
  id,
  className = "auth-input",
  disabled = false,
  required = false,
}: RoomSelectProps): JSX.Element {
  const [rooms, setRooms] = useState<Room[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!branchId) {
      setRooms(null);
      setLoadError(null);
      return;
    }
    let cancelled = false;
    setRooms(null);
    setLoadError(null);
    listRooms({ branchId, roomType })
      .then((found) => {
        if (!cancelled) {
          setRooms(found);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setLoadError(extractErrorMessage(error, "Could not load rooms."));
          setRooms([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [branchId, roomType]);

  if (!branchId) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Select a branch first…</option>
      </select>
    );
  }

  if (rooms === null) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">Loading…</option>
      </select>
    );
  }

  if (rooms.length === 0) {
    return (
      <select id={id} className={className} value="" disabled>
        <option value="">{loadError ?? "No rooms found"}</option>
      </select>
    );
  }

  return (
    <select
      id={id}
      className={className}
      value={value}
      disabled={disabled}
      required={required}
      onChange={(event) => onChange(event.target.value)}
    >
      <option value="">Select a room…</option>
      {rooms.map((room) => (
        <option key={room.id} value={room.id}>
          {room.name}
        </option>
      ))}
    </select>
  );
}
