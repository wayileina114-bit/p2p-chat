# -*- coding: utf-8 -*-
import os, sys, tempfile, time
sys.stdout.reconfigure(encoding="utf-8")
TMP = tempfile.mkdtemp(prefix="p2p_perf_")
import chat_gui as cg
cg.DATA_DIR = os.path.join(TMP, "history")
cg.DOWNLOADS_DIR = os.path.join(TMP, "downloads")
os.makedirs(cg.DATA_DIR, exist_ok=True); os.makedirs(cg.DOWNLOADS_DIR, exist_ok=True)
import customtkinter as ctk
cg.ChatApp._auto_connect_on_startup = lambda self: None
root = ctk.CTk()
root.geometry("900x640+80+60")
app = cg.ChatApp(root, name="smoke")
# 预置 60 条消息
key = app._group_key("roomA")
app._ensure_group_session("roomA")
for i in range(60):
    app._append_message(key, "bob", "测试消息 %d" % i, False, mid="m%d" % i)
app._switch_to(key)
app._flush_feed_render()
root.update()
for target in ("light", "dark", "anime", "dark"):
    t0 = time.time()
    app._set_theme(target)
    # 遮罩过渡下多次 update 模拟真实渲染
    for _ in range(4):
        root.update_idletasks(); root.update()
    print("切换到 %-6s 耗时 %.0f ms" % (target, (time.time()-t0)*1000))
root.destroy()
