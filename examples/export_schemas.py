"""运行 python -m examples.export_schemas；从代码导出供 B/C/D 评审的协议。"""
import json
from pathlib import Path

from backend.agent.contracts import AgentResponse
from backend.agent.mock_tools import MockCampus


def main():
    target = Path(__file__).absolute().parents[1] / "docs"
    target.mkdir(exist_ok=True)
    documents = {
        "tool_schemas.json": MockCampus().registry().schemas(),
        "agent_response.schema.json": AgentResponse.model_json_schema(),
    }
    for name, data in documents.items():
        (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Exported tool_schemas.json and agent_response.schema.json")


if __name__ == "__main__":
    main()
