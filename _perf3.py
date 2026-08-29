# -*- coding: utf-8 -*-
import os, sys, tempfile, time
sys.stdout.reconfigure(encoding="utf-8")
TMP = tempfile.mkdtemp(prefix="p2p_perf3_")
import chat_gui as cg
cg.DATA_DIR = os.path.join(TMP, "history")
cg.DOWNLOADS_DIR = os.path.join(TMP, "downloads")
os.makedirs(cg.DATA_DIR, exist_ok=True); os.makedirs(cg.DOWNLOADS_DIR, exist_ok=True)
import customtkinter as ctk
cg.ChatApp._auto_connect_on_startup = lambda self: None
root = ctk.CTk()
root.geometry("900x640+80+60")
app = cg.ChatApp(root, name="smoke")
key = app._group_key("roomA")
app._ensure_group_session("roomA")
for i in range(60):
    app._append_message(key, "bob", "测试消息 %d" % i, False, mid="m%d" % i)
app._switch_to(key)
app._flush_feed_render()
root.update()
app._set_theme("anime")  # 先切走（基线）
for _ in range(4):
    root.update_idletasks(); root.update()
for target in ("dark", "light", "anime"):
    t0 = time.time()
    app._set_theme(target)
    for _ in range(4):
        root.update_idletasks(); root.update()
    print("切换到 %-6s 首屏耗时 %.0f ms" % (target, (time.time()-t0)*1000))
    assert root.state() == "normal"
# 延迟补全后消息都渲染了吗（验证补全机制）
time.sleep(0.5)
for _ in range(6):
    root.update_idletasks(); root.update()
    time.sleep(0.1)
n = len(app._bubble_frames)
print("补全后气泡数:", n)
root.destroy()
