"""
Contia Copilot — Chat Routes

Endpoints:
  POST /chatbot/chat   — streaming SSE chat (authenticated)
  POST /chatbot/reset  — client-side hint to clear history (stateless, no-op on server)
"""

import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.routes.auth import get_current_user
from app.services.chatbot_service import ChatbotService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chatbot", tags=["Contia Copilot"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str          # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    include_context: Optional[bool] = True  # inject live DB context into system prompt


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def get_chatbot_service() -> ChatbotService:
    return ChatbotService()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/chat",
    summary="Contia Copilot — streaming chat",
    description=(
        "Send a conversation history and receive a streaming SSE response from "
        "Contia Copilot. The server injects a live snapshot of the authenticated "
        "user's invoices, vouchers, and VAT data into the system prompt so the "
        "assistant can answer questions grounded in real data."
    ),
)
async def chat(
    request: ChatRequest,
    current_user: dict = Depends(get_current_user),
    service: ChatbotService = Depends(get_chatbot_service),
):
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages list cannot be empty")

    # Validate roles
    for msg in request.messages:
        if msg.role not in ("user", "assistant"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid message role '{msg.role}'. Must be 'user' or 'assistant'."
            )

    # Convert Pydantic models to plain dicts for the service
    messages = [{"role": m.role, "content": m.content} for m in request.messages]

    return StreamingResponse(
        service.stream_response(
            messages=messages,
            user=current_user,
            include_context=request.include_context,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering for SSE
        },
    )


@router.post(
    "/reset",
    summary="Reset chat session (client-side hint)",
    description=(
        "Stateless endpoint — the server holds no session state. "
        "The frontend should clear its local message history on receiving 200."
    ),
)
def reset_chat(current_user: dict = Depends(get_current_user)):
    """No-op on the server. Tells the client it's safe to wipe local history."""
    return {"message": "Chat session reset. Clear your local message history."}
