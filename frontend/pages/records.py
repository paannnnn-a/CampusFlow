# frontend/pages/records.py 的初始代码
import streamlit as st

st.set_page_config(page_title="我的记录", page_icon="📋")
st.title("📋 我的办理记录")

st.markdown("这里将展示你在 CampusFlow 产生的所有预约、申请和报修记录。")

# 造一点假数据来占位
mock_records = [
    {"id": "R001", "type": "座位预约", "detail": "LIB-A301-01", "status": "已成功"},
    {"id": "R002", "type": "活动报名", "status": "待审核"},
    {"id": "R003", "type": "报修工单", "detail": "空调不制冷", "status": "处理中"}
]

st.dataframe(mock_records, use_container_width=True)