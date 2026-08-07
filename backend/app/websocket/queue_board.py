"""Live queue board over WebSocket, scoped per (branch_id, department_id).

Kept process-local for the scaffold (an in-memory connection registry). In a
multi-pod deployment, back this with Redis pub/sub so a broadcast from any
pod reaches clients connected to any other pod — the ConnectionManager
interface below is deliberately the only thing that would need to change.

Auth (whole-system review finding H1): this endpoint used to accept ANY
connection with no token at all, unlike every REST sibling in this codebase
— anyone who could reach the API could stream live token/queue data for any
branch with no Bearer token. Browsers can't set an `Authorization` header on
a WebSocket upgrade request, so the token is passed as a query parameter
instead (`?token=...`) and validated with the same `decode_token`/blocklist
logic `get_current_user` uses for REST requests, before `accept()` is ever
called. Role/branch gating mirrors `routers/queue.py`'s `GET /queue/board`
`_READ_ROLES` (front_desk/nurse/doctor/system_admin) and its branch scoping
(system_admin may watch any branch; every other role only their own,
resolved via the token-frozen `get_caller_branch_id`, never a live DB read).
"""

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from app.core.security import decode_token, get_caller_branch_id, is_token_blocklisted
from app.database import get_db
from app.models.user import User, UserRole

router = APIRouter()

_READ_ROLES = {UserRole.front_desk, UserRole.nurse, UserRole.doctor, UserRole.system_admin}


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


async def _authenticate(websocket: WebSocket, db: Session, branch_id: str) -> bool:
    """Validate the `?token=` query param before any connection is
    accepted. Returns True and lets the caller proceed on success; on any
    failure, closes the socket with 1008 (policy violation) and returns
    False. Deliberately rejects BEFORE `websocket.accept()` -- accepting
    first and closing after would still complete the WS handshake and
    briefly register the connection, which is unnecessary exposure for an
    unauthenticated caller."""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="missing token")
        return False

    try:
        payload = decode_token(token)
    except HTTPException:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid or expired token")
        return False

    if payload.get("type") != "access":
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="wrong token type")
        return False

    jti = payload.get("jti")
    if jti and is_token_blocklisted(jti):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="token has been revoked")
        return False

    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="user not found or inactive")
        return False

    if user.role not in _READ_ROLES:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="role not permitted")
        return False

    if user.role != UserRole.system_admin:
        user.token_claims = payload  # type: ignore[attr-defined]
        caller_branch_id = get_caller_branch_id(user)
        if caller_branch_id is None or caller_branch_id != branch_id:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="branch mismatch")
            return False

    return True


@router.websocket("/ws/queue/{branch_id}/{department_id}")
async def queue_board_socket(
    websocket: WebSocket, branch_id: str, department_id: str, db: Session = Depends(get_db)
) -> None:
    if not await _authenticate(websocket, db, branch_id):
        return

    await manager.connect(websocket, branch_id, department_id)
    try:
        while True:
            # Clients don't send anything meaningful here; this is a
            # server-push channel. We still need to await recv to detect
            # disconnects promptly.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, branch_id, department_id)
