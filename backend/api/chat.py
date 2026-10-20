from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, StrictBool
from starlette.concurrency import run_in_threadpool

router = APIRouter()

class ChatBody(BaseModel):
    message: str
    session_id: str | None = None

class ConfirmBody(BaseModel):
    session_id: str
    confirmation_id: str
    approved: StrictBool

async def get_current_user():
    return "u_demo" 

@router.post("/api/chat")
async def chat_endpoint(body: ChatBody, request: Request):
    user_id = await get_current_user()
    agent = request.app.state.agent
    try:
        r = await run_in_threadpool(
            agent.chat,
            user_id=user_id,
            session_id=body.session_id,
            message=body.message,
        )
        return r.model_dump(mode="json")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/chat/confirm")
async def confirm_endpoint(body: ConfirmBody, request: Request):
    user_id = await get_current_user()
    agent = request.app.state.agent
    try:
        r = await run_in_threadpool(
            agent.confirm,
            user_id=user_id,
            session_id=body.session_id,
            confirmation_id=body.confirmation_id,
            approved=body.approved,
        )
        return r.model_dump(mode="json")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))