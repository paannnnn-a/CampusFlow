from contextlib import asynccontextmanager
from fastapi import FastAPI
from backend.agent.service import AgentService
from backend.api.chat import router as chat_router
from backend.api.records import router as records_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("正在初始化真实 Agent...")
    app.state.agent = AgentService.demo() 
    yield

# 1. 必须先创建 app
app = FastAPI(lifespan=lifespan)

# 2. 然后再把路由挂上去
app.include_router(chat_router)
app.include_router(records_router)

@app.get("/")
def read_root():
    return {"status": "ok", "msg": "API is running"}