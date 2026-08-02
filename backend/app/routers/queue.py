"""Real-time queue / digital token module — STUB.

Core model (QueueToken) and the WebSocket broadcast channel
(app/websocket/queue_board.py) already exist.

TODO:
- POST /api/v1/queue/check-in -> issue QueueToken, broadcast to queue board
- PATCH /api/v1/queue/{token_id}/status -> waiting/in_consultation/delayed/skipped/done,
  broadcast on every transition
- On consultation end, call
  scheduling_engine.recalculate_downstream_wait_times() and broadcast updated
  estimates for every affected token
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/queue", tags=["queue"])
