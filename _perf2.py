# -*- coding: utf-8 -*-
import os, sys, tempfile, time
sys.stdout.reconfigure(encoding="utf-8")
TMP = tempfile.mkdtemp(prefix="p2p_perf2_")
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

# 细分 _rebuild_ui 各阶段耗时（手动复刻其内部步骤）
def timed(label, fn):
    t0 = time.time()
    fn()
    root.update_idletasks(); root.update()
    print("%-22s %6.0f ms" % (label, (time.time()-t0)*1000))

timed("关闭浮窗", lambda: None)
timed("清图片引用", lambda: setattr(app, "_images", []))
def do_destroy():
    for w in list(root.winfo_children()):
        try: w.destroy()
        except Exception: pass
timed("销毁全部控件", do_destroy)
timed("root.configure", lambda: root.configure(fg_color=cg.C("app_bg")))
timed("_build_ui", lambda: app._build_ui())
timed("_build_menu", lambda: app._build_menu())
timed("_apply_session_list", lambda: app._apply_session_list())
_saved = app.RENDER_MAX
app.RENDER_MAX = 60
timed("_render_feed(60)", lambda: app._render_feed())
app.RENDER_MAX = _saved
root.destroy()
