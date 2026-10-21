import streamlit as st
import requests

st.title("📋 我的办理记录")
st.write("这里展示你的预约、报修和事务记录。")

API_URL = "http://127.0.0.1:8000/api/records"

try:
    resp = requests.get(API_URL)
    if resp.status_code == 200:
        data = resp.json()
        
        st.subheader("事务申请草稿")
        if data.get("affairs"):
            st.dataframe(data["affairs"], use_container_width=True)
        else:
            st.info("暂无事务申请草稿")

        st.subheader("报修工单")
        if data.get("repairs"):
            st.dataframe(data["repairs"], use_container_width=True)
        else:
            st.info("暂无报修工单")
    else:
        st.warning("获取数据失败")
except Exception as e:
    st.error(f"无法连接后端: {e}")