import streamlit as st
import requests

# 设置网页标题
st.set_page_config(page_title="CampusFlow 校园智能服务", page_icon="🎓")
st.title("🎓 CampusFlow 校园智能服务")

# 你的后端 API 地址
API_URL = "http://127.0.0.1:8000/api/chat"

# 初始化网页的会话记忆
if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = None

# 渲染历史聊天记录
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 聊天输入框
if prompt := st.chat_input("请问有什么可以帮您？(例如：今天有什么课？)"):
    # 1. 把用户的输入显示在页面上
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # 2. 调用你的 FastAPI 后端
    with st.chat_message("assistant"):
        with st.spinner("CampusFlow 正在思考..."):
            try:
                payload = {"message": prompt}
                if st.session_state.session_id:
                    payload["session_id"] = st.session_state.session_id
                
                # 发送 POST 请求给你的 API
                response = requests.post(API_URL, json=payload)
                response.raise_for_status() # 如果报错就抛出异常
                
                data = response.json()
                
                # 获取 Agent 的回答
                answer = data.get("answer", "（没有收到回答）")
                st.markdown(answer)
                
                # 保存 session_id 用于多轮对话
                st.session_state.session_id = data.get("session_id")
                
                # 渲染下方的工具调用轨迹（可选，答辩用）
                trace = data.get("trace", [])
                if trace:
                    with st.expander("🔍 查看系统后台执行轨迹 (Tool Trace)"):
                        st.json(trace)
                        
                st.session_state.messages.append({"role": "assistant", "content": answer})
                
            except Exception as e:
                st.error(f"连接后端失败: {e}")
                st.info("请检查 uvicorn 后端是否正常启动。")