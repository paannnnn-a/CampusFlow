import streamlit as st
import requests

st.set_page_config(page_title="CampusFlow 校园智能服务", page_icon="🎓", layout="wide")
st.title("🎓 CampusFlow 校园智能服务")

API_URL = "http://127.0.0.1:8000/api/chat"
CONFIRM_URL = "http://127.0.0.1:8000/api/chat/confirm"

# 初始化状态
if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = None
if "pending" not in st.session_state:
    st.session_state.pending = None # 用来保存待确认的动作

# 渲染历史聊天
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        # 如果有对应的后台轨迹，显示在一个折叠框里
        if "trace" in msg and msg["trace"]:
            with st.expander("🔍 查看系统后台执行轨迹 (Tool Trace)"):
                st.json(msg["trace"])

# --- 渲染确认卡片 ---
if st.session_state.pending:
    p = st.session_state.pending
    st.warning("⚠️ **需要您确认的操作**")
    st.info(f"**工具名称**：`{p.get('tool_name')}`\n\n**参数**：`{p.get('arguments')}`")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("✅ 确认执行", type="primary", use_container_width=True):
            with st.spinner("执行中..."):
                try:
                    resp = requests.post(CONFIRM_URL, json={
                        "session_id": st.session_state.session_id,
                        "confirmation_id": p.get("confirmation_id"),
                        "approved": True
                    })
                    data = resp.json()
                    # 把执行结果存入聊天记录
                    st.session_state.messages.append({"role": "assistant", "content": data.get("answer"), "trace": data.get("trace")})
                    st.session_state.pending = None # 清空待确认状态
                    st.rerun() # 刷新页面
                except Exception as e:
                    st.error(f"确认失败: {e}")
    with col2:
        if st.button("❌ 取消操作", use_container_width=True):
            try:
                resp = requests.post(CONFIRM_URL, json={
                    "session_id": st.session_state.session_id,
                    "confirmation_id": p.get("confirmation_id"),
                    "approved": False
                })
                data = resp.json()
                st.session_state.messages.append({"role": "assistant", "content": data.get("answer")})
            except Exception as e:
                pass
            st.session_state.pending = None
            st.rerun()

# --- 聊天输入框 ---
if prompt := st.chat_input("请问有什么可以帮您？"):
    # 存用户消息并显示
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # 发请求给后端
    with st.chat_message("assistant"):
        with st.spinner("CampusFlow 正在思考..."):
            try:
                payload = {"message": prompt}
                if st.session_state.session_id:
                    payload["session_id"] = st.session_state.session_id
                
                response = requests.post(API_URL, json=payload)
                data = response.json()
                
                # 保存返回的 session_id
                st.session_state.session_id = data.get("session_id")
                
                # 显示回答
                answer = data.get("answer", "（没有回复）")
                trace = data.get("trace", [])
                
                # 检查是否触发了写操作确认
                if data.get("status") == "awaiting_confirmation" and data.get("pending_confirmation"):
                    st.session_state.pending = data.get("pending_confirmation")
                    st.session_state.messages.append({"role": "assistant", "content": answer, "trace": trace})
                    st.rerun() # 刷新页面，把确认卡片弹出来
                else:
                    st.markdown(answer)
                    if trace:
                        with st.expander("🔍 查看系统后台执行轨迹 (Tool Trace)"):
                            st.json(trace)
                    st.session_state.messages.append({"role": "assistant", "content": answer, "trace": trace})
                    
            except Exception as e:
                st.error(f"连接后端失败: {e}")