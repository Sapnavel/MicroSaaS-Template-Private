"""Live queue board over WebSocket, scoped per (branch_id, department_id).

Kept process-local for the scaffold (an in-memory connection registry). In a
multi-pod deployment, back this with Redis pub/sub so a broadcast from any
pod reaches clients connected to any other pod — the ConnectionManager
interface below is deliberately the only thing that would need to change.
"""

from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    @staticmethod
    def _topic(branch_id: str, department_id: str) -> str:
        return f"{branch_id}:{department_id}"

    async def connect(self, websocket: WebSocket, branch_id: str, department_id: str) -> None:
        await websocket.accept()
        self._connections[self._topic(branch_id, department_id)].add(websocket)

    def disconnect(self, websocket: WebSocket, branch_id: str, department_id: str) -> None:
        self._connections[self._topic(branch_id, department_id)].discard(websocket)

    async def broadcast(self, branch_id: str, department_id: str, message: dict) -> None:
        for ws in list(self._connections[self._topic(branch_id, department_id)]):
            await ws.send_json(message)


manager = ConnectionManager()


@router.websocket("/ws/queue/{branch_id}/{department_id}")
async def queue_board_socket(websocket: WebSocket, branch_id: str, department_id: str) -> None:
    await manager.connect(websocket, branch_id, department_id)
    try:
        while True:
            # Clients don't send anything meaningful here; this is a
            # server-push channel. We still need to await recv to detect
            # disconnects promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, branch_id, department_id)
