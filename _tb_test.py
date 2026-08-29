# -*- coding: utf-8 -*-
import os, sys, ctypes
sys.stdout.reconfigure(encoding="utf-8")
import tkinter as _tk
root = _tk.Tk()
root.geometry("600x400+100+100")
root.attributes("-toolwindow", True)
root.update()
hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
GWL_EXSTYLE = -20
style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
style = (style & ~0x80) | 0x40000   # 去 TOOLWINDOW 加 APPWINDOW
ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
root.update()
style2 = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
print("new exstyle:", hex(style2), "APPWINDOW:", bool(style2 & 0x40000), "TOOLWINDOW:", bool(style2 & 0x80))
root.iconify(); root.update()
print("iconify state:", root.state())
root.deiconify(); root.update()
print("deiconify state:", root.state())
root.state("zoomed"); root.update()
print("zoomed:", root.state(), root.geometry())
root.state("normal"); root.update()
# resizable 边框
root.resizable(True, True)
print("resizable ok")
root.destroy()
print("DONE")
