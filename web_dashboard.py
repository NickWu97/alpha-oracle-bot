# web_dashboard.py
import streamlit as st
import pandas as pd
import time
from database import db
from config import config

st.set_page_config(page_title="Alpha Oracle Pro v16", layout="wide")
st.title("🤖 Alpha Oracle Pro v16 即時儀表板")

# 載入歷史交易
trades = db.load_trades(limit=200)
if trades:
    df = pd.DataFrame(trades)
    df["created_at"] = pd.to_datetime(df["created_at"])
    df = df.sort_values("created_at", ascending=False)
    st.subheader("📊 近期交易記錄")
    st.dataframe(df[["created_at","coin","side","entry","close","close_type","pnl","score"]])
    
    # 統計
    col1, col2, col3 = st.columns(3)
    wins = df[df["close_type"].isin(["TP1","TP2","TP3","LOCK"])]
    losses = df[df["close_type"]=="SL"]
    col1.metric("總交易筆數", len(df))
    col2.metric("勝率", f"{len(wins)/len(df)*100:.1f}%" if len(df)>0 else "0%")
    col3.metric("總損益 %", f"{df['pnl'].sum():+.2f}%")
    
    st.subheader("📈 每日損益曲線")
    daily = df.groupby(df["created_at"].dt.date)["pnl"].sum().reset_index()
    st.line_chart(daily.set_index("created_at"))
else:
    st.info("尚無交易記錄")

# 即時持倉（簡化，實際需從 tracker 獲取）
st.subheader("📌 即時追蹤中訊號")
active = db.get_active_signals()
if active:
    for sig in active:
        st.write(f"{sig['order_id'][-8:]}: {sig['signal_json'][:100]}...")
else:
    st.write("無活躍訊號")

# 自動刷新
if st.button("🔄 手動刷新"):
    st.rerun()
st.caption(f"最後更新: {time.strftime('%Y-%m-%d %H:%M:%S')}")
