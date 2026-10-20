from contextlib import asynccontextmanager
from fastapi import FastAPI
from backend.agent.service import AgentService
from backend.api.chat import router as chat_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("正在初始化真实 Agent...")
    # 真正调用 A 写的 AgentService
    app.state.agent = AgentService.demo() 
    yield

app = FastAPI(lifespan=lifespan)
app.include_router(chat_router)

@app.get("/")
def read_root():
    return {"status": "ok", "msg": "API is running"}