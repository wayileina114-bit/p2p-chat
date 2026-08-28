#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动器 + 运行环境自检 + 一键安装（纯标准库，无需任何第三方依赖即可运行）

用法：
    python launcher.py            # 图形界面：检测环境 → 缺啥一键装 → 自动启动聊天
    python launcher.py --cli      # 命令行：检测并打印；缺依赖自动安装
    python launcher.py --install  # 命令行：直接安装全部依赖
"""

import importlib
import subprocess
import sys
import threading

MIN_PY = (3, 9)

# (显示名, pip 包名, import 名, 用途说明)
REQS = [
    ("paho-mqtt",      "paho-mqtt",      "paho.mqtt.client", "文字 / 文件传输"),
    ("Pillow",         "Pillow",         "PIL",              "图片显示与预览"),
    ("customtkinter",  "customtkinter",  "customtkinter",    "圆角现代界面"),
    ("tkinterdnd2",    "tkinterdnd2",    "tkinterdnd2",      "拖拽发送文件/图片"),
    ("cryptography",   "cryptography",   "cryptography",     "端到端加密（可选）"),
]


def py_version_ok():
    return sys.version_info[:2] >= MIN_PY


def py_version_text():
    return "%d.%d.%d" % sys.version_info[:3]


def check_module(req):
    """req = (显示名, pip包名, import名, 说明)，返回 (是否已装, 说明)。"""
    try:
        importlib.import_module(req[2])
        return True, "已安装"
    except Exception:
        return False, "未安装"


def scan():
    rows = []
    vok = py_version_ok()
    rows.append(("Python 解释器", py_version_text(), vok,
                 "" if vok else "需要 >= %d.%d" % MIN_PY))
    for req in REQS:
        ok, msg = check_module(req)
        rows.append((req[0], req[3], ok, msg))
    return rows


def missing_reqs():
    return [r for r in REQS if not check_module(r)[0]]


def pip_install(reqs, log=None):
    """安装给定依赖集合，返回 (是否成功, 完整日志)。"""
    pkgs = [r[1] for r in reqs]
    cmd = [sys.executable, "-m", "pip", "install",
           "--disable-pip-version-check", *pkgs]

    def emit(s):
        if log:
            log(s)

    emit("执行：%s\n" % " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        out = []
        for line in proc.stdout:
            line = line.rstrip("\n")
            out.append(line)
            emit(line + "\n")
        proc.wait()
        emit("\n退出码：%d\n" % proc.returncode)
        return proc.returncode == 0, "\n".join(out)
    except Exception as e:
        emit("安装失败：%s\n" % e)
        return False, str(e)


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("P2P 聊天 · 环境检测")
    root.geometry("600x440")
    root.resizable(False, False)
    root.configure(bg="#f5f6fa")

    head = tk.Label(root, text="🚀 运行环境检测", font=("Microsoft YaHei", 15, "bold"),
                    bg="#f5f6fa", fg="#2c3e50")
    head.pack(pady=(16, 4))

    sub = tk.Label(root, text="首次运行会自动检查 Python 与所需组件，缺什么一键补齐。",
                   font=("Microsoft YaHei", 9), bg="#f5f6fa", fg="#888888")
    sub.pack(pady=(0, 10))

    body = tk.Frame(root, bg="#ffffff", highlightbackground="#e0e0e0", highlightthickness=1)
    body.pack(fill="both", expand=True, padx=16)

    text = tk.Text(body, height=16, wrap="word", state="disabled",
                   font=("Consolas", 10), bg="#ffffff", fg="#333333",
                   borderwidth=0, padx=12, pady=10)
    vsb = ttk.Scrollbar(body, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)

    status_var = tk.StringVar(value="正在检测…")
    status = tk.Label(root, textvariable=status_var, font=("Microsoft YaHei", 10),
                      bg="#f5f6fa", fg="#2c3e50")
    status.pack(pady=(10, 4))

    btns = tk.Frame(root, bg="#f5f6fa")
    btns.pack(pady=(0, 14))

    action_btn = tk.Button(btns, text="检测中…", width=24, font=("Microsoft YaHei", 10),
                           bg="#3b82f6", fg="#ffffff", activebackground="#2563eb",
                           activeforeground="#ffffff", relief="flat", cursor="hand2")
    action_btn.pack(side="left", padx=6)

    quit_btn = tk.Button(btns, text="退出", width=10, font=("Microsoft YaHei", 10),
                         bg="#e5e7eb", fg="#333333", activebackground="#d1d5db",
                         relief="flat", cursor="hand2", command=root.destroy)
    quit_btn.pack(side="left", padx=6)

    def set_text(s):
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("end", s)
        text.configure(state="disabled")

    def append_text(s):
        text.configure(state="normal")
        text.insert("end", s)
        text.see("end")
        text.configure(state="disabled")

    def render(rows):
        lines = []
        for name, desc, ok, msg in rows:
            mark = "  [OK]  " if ok else "  [缺]  "
            tail = ("    " + desc) if desc else ""
            extra = ("    " + msg) if msg else ""
            lines.append(mark + name.ljust(16) + tail + extra)
        return "\n".join(lines)

    def launch():
        root.destroy()
        try:
            import chat_gui
        except Exception as e:
            _error_box("启动失败", "无法加载 chat_gui：\n%s" % e)
            return
        chat_gui.main()

    def start_install():
        pkgs = missing_reqs()
        if not pkgs:
            refresh()
            return
        action_btn.configure(state="disabled", text="正在安装…")
        quit_btn.configure(state="disabled")
        status_var.set("正在安装缺失组件，请稍候…")
        set_text("待安装：%s\n\n" % ", ".join(r[0] for r in pkgs))

        def append(s):
            root.after(0, lambda s=s: append_text(s))

        def done(ok, _log):
            quit_btn.configure(state="normal")
            if ok:
                status_var.set("安装完成，重新检测…")
                root.after(300, refresh)
            else:
                status_var.set("安装未完全成功，请查看上方日志")
                action_btn.configure(state="normal", text="重试安装", command=start_install)

        def work():
            ok, log = pip_install(pkgs, log=append)
            root.after(0, lambda: done(ok, log))

        threading.Thread(target=work, daemon=True).start()

    def refresh():
        rows = scan()
        set_text(render(rows))
        if not py_version_ok():
            status_var.set("Python 版本过低，请先安装 %d.%d 或更高版本" % MIN_PY)
            action_btn.configure(state="disabled", text="Python 版本过低", command=None)
            return
        all_ok = all(r[2] for r in rows)
        if all_ok:
            status_var.set("环境就绪，正在启动聊天…")
            action_btn.configure(state="normal", text="启动聊天", command=launch)
            root.after(600, launch)
        else:
            status_var.set("检测到缺失组件，点下方按钮一键安装")
            action_btn.configure(state="normal", text="一键安装缺失组件", command=start_install)

    root.after(150, refresh)
    root.mainloop()


def _error_box(title, msg):
    import tkinter as tk
    from tkinter import messagebox
    r = tk.Tk()
    r.withdraw()
    messagebox.showerror(title, msg)
    r.destroy()


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

def run_cli():
    print("=" * 56)
    print("P2P 聊天 - 运行环境检测")
    print("Python 解释器：", sys.executable)
    print("=" * 56)

    if not py_version_ok():
        print("[缺] Python 版本过低：%s（需要 >= %d.%d）" % (py_version_text(), *MIN_PY))
        return 1

    for name, desc, ok, msg in scan():
        mark = "[OK]" if ok else "[缺]"
        print("%s  %-14s %-16s %s" % (mark, name, desc, msg))

    pkgs = missing_reqs()
    if pkgs:
        print("\n检测到缺失组件，开始自动安装：", ", ".join(r[0] for r in pkgs))
        ok, _log = pip_install(pkgs, log=lambda s: print(s, end=""))
        if not ok:
            print("安装失败，请手动执行：python -m pip install "
                  "paho-mqtt pillow customtkinter tkinterdnd2")
            return 1
        print("\n安装完成，重新检测：")
        for name, desc, ok2, msg in scan():
            print("%s  %-14s %-16s %s" % ("[OK]" if ok2 else "[缺]", name, desc, msg))
        if not all(r[2] for r in scan()):
            return 1
    else:
        print("\n环境全部就绪 ✅")

    print("\n可执行：python chat_gui.py  或  python launcher.py  启动聊天")
    return 0


def main():
    if "--install" in sys.argv:
        ok, _log = pip_install(REQS, log=lambda s: print(s, end=""))
        return 0 if ok else 1
    if "--cli" in sys.argv:
        return run_cli()
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())