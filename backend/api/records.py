from fastapi import APIRouter, Depends
# 注意：这里的导入路径取决于 B 同学的文件到底叫什么名字
# 假设他给你的文件叫 b_sqlite.py
from backend.database.b_sqlite import SqlRepository

router = APIRouter()

# 初始化 B 同学的 SQLite 仓库
repo = SqlRepository(path="data/campusflow.db")

async def get_current_user():
    return "u_demo"

@router.get("/api/records")
async def get_records(user_id: str = Depends(get_current_user)):
    drafts = repo.list_affair_drafts(user_id)
    tickets = repo.list_repair_tickets(user_id)

    return {
        "affairs": [d.to_public() for d in drafts],
        "repairs": [t.to_public() for t in tickets]
    }