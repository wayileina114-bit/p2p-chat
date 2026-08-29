# -*- coding: utf-8 -*-
import os, sys, tempfile
sys.stdout.reconfigure(encoding="utf-8")
TMP = tempfile.mkdtemp(prefix="p2p_menu_")
import chat_gui as cg
cg.DATA_DIR = os.path.join(TMP, "history")
cg.DOWNLOADS_DIR = os.path.join(TMP, "downloads")
os.makedirs(cg.DATA_DIR, exist_ok=True); os.makedirs(cg.DOWNLOADS_DIR, exist_ok=True)
import customtkinter as ctk
cg.ChatApp._auto_connect_on_startup = lambda self: None
root = ctk.CTk()
root.geometry("900x640+80+60")
app = cg.ChatApp(root, name="smoke")
root.update()
# menubar 是否挂上了
try:
    m = root.nametowidget(root.cget("menu"))
    print("menubar 存在:", m, "项数:", m.index("end") + 1 if m.index("end") is not None else "?")
except Exception as e:
    print("menubar 不存在:", e)
# toolwindow 下 menu 是否可见——winfo_ismapped 无法直接测菜单栏，用高度推断
print("root 高度:", root.winfo_height(), "（toolwindow 无标题栏，菜单栏是否占位未知）")
root.destroy()
print("DONE")
