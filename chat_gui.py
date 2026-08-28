#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2P 聊天（图形界面版 · QQ 式单窗口 + 多房间） chat_gui.py
=========================================================
- 传输：免费公共 MQTT 服务器（无需公网 IP / 无需搭服务器 / 免注册）
- 功能：多房间群聊、自动发现在线成员、私聊（走全局隐藏通道，不依赖房间）、发图片 / 任意文件
- 布局：单窗口，左侧「会话列表」（群聊 + 私聊 + 在线成员 + 搜索），右侧当前会话聊天区
- 历史：聊天记录自动存到 程序/exe 所在目录 history/ 文件夹，断开重连、重启后自动恢复
- 文件：接受方先弹窗确认「接收/拒绝」，同意后才开始传输
- 提速：256KB 大分片 + 二进制直传（不再用 base64）
- 交互：图片/文件可直接拖拽到输入框或聊天区发送
- 文件校验：MD5 + 大小校验，收到的文件保存到程序目录下 downloads/ 文件夹

打包成 exe：
    pip install paho-mqtt pillow customtkinter tkinterdnd2 pyinstaller
    pyinstaller --noconfirm --onedir --windowed --name P2PChat chat_gui.py
"""

import hashlib
import json
import math
import mimetypes
import os
import re
import struct
import sys
import threading
import time
import uuid

try:
    import paho.mqtt.client as mqtt
    _MQTT_OK = True
except Exception:
    mqtt = None
    _MQTT_OK = False

try:
    import importlib.util
    _HAS_PIL = importlib.util.find_spec("PIL") is not None
except Exception:
    _HAS_PIL = False
Image = None  # 惰性加载：首次显示图片时才真正 import PIL，加快启动

try:
    import customtkinter as ctk
    from customtkinter import CTkImage
    _HAS_CTK = True
except Exception:
    ctk = None
    CTkImage = None
    _HAS_CTK = False

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:
    TkinterDnD = None
    DND_FILES = None
    _HAS_DND = False

try:
    from cryptography.fernet import Fernet
    _HAS_CRYPTO = True
except Exception:
    Fernet = None
    _HAS_CRYPTO = False

# 拖拽功能是否在运行时真正可用（成功后才会注册 drop target，避免 tkdnd 加载失败导致崩溃）
_DND_READY = False


def _derive_fernet(passphrase):
    """从口令派生 Fernet 加密器（PBKDF2-SHA256 派生 AES 密钥）；无口令或无库时返回 None。"""
    if not passphrase or not _HAS_CRYPTO:
        return None
    try:
        import base64
        import hashlib
        key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"),
                                  b"p2pchat-e2e-v1", 200000, 32)
        return Fernet(base64.urlsafe_b64encode(key))
    except Exception:
        return None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

APP_VERSION = "2.2.2"            # 程序版本（每次更新时 +1）
UPDATE_OWNER = "wayileina114-bit"  # GitHub 仓库所有者（自动检查更新用）
UPDATE_REPO = "p2p-chat"           # GitHub 仓库名（自动检查更新用）

DEFAULT_BROKER = "broker.emqx.io"
DEFAULT_PORT = 1883
CHUNK_SIZE = 256 * 1024          # 每个分片 256KB（二进制直传，无 base64 开销）
MAX_FILE = 200 * 1024 * 1024     # 单文件上限 200MB
OFFER_TIMEOUT = 60.0             # 送文件请求 60 秒无人应答则取消
RECV_TIMEOUT = 120.0             # 接收方收不齐数据的超时（秒），超时清理残留分片
PRESENCE_TTL = 120.0             # 在线名单过期时间（秒），超时视为下线（兜底 will 丢失）
MAX_TEXT = 10000                 # 单条文字消息长度上限（防异常/恶意超长消息撑爆界面）

FONT = "Microsoft YaHei UI"
HINT = "输入文字，回车发送；也可直接把图片 / 文件拖到这里"

EMOJIS = [
    "😀","😁","😂","🤣","😊","😇","🙂","😉","😍","🥰",
    "😘","😜","🤪","🤔","🤨","😐","😶","🙄","😏","😣",
    "😥","😮","🤯","😴","🥱","😷","🤒","🥵","🥶","😎",
    "🤓","🥳","😡","😠","🤬","😱","😨","😰","😢","😭",
    "😤","😩","🥺","🤗","🤭","🤫","😌","😪","🤤","🥴",
    "😳","🤡","👋","🤝","👍","👎","👏","🙏","💪","🤞",
    "🤟","✌️","🤙","❤️","🧡","💛","💚","💙","💜","🖤",
    "🤍","💔","💯","✨","⭐","🌟","⚡","🔥","💥","💫",
    "🌈","☀️","🌙","🍀","🌹","🌸","🎉","🎊","🎁","🏆",
    "🥇","🚀","✈️","🚗","⏰","⌛","📌","📎","🔒","🔑",
    "🔍","💡","📈","📉","🎵","🎶","🎮","🎯","🏀","⚽",
]


# ---------------------------------------------------------------------------
# 主题（暗色参考 Discord，亮色参考 QQ；可切换 + 持久化）
# ---------------------------------------------------------------------------

THEMES = {
    "dark": {
        "app_bg": "#1e1f22", "panel": "#2b2d31", "panel_2": "#313338",
        "input_bg": "#383a40", "input_hover": "#404249",
        "accent": "#5865f2", "accent_hover": "#4752c4",
        "mine_bubble": "#5865f2", "mine_text": "#ffffff",
        "other_bubble": "#2b2d31", "other_text": "#dbdee1",
        "text": "#dbdee1", "text_2": "#949ba4", "text_mute": "#6d6f78",
        "hover": "#35373c", "selected_bg": "#404249", "selected_text": "#ffffff",
        "online": "#23a55a", "danger": "#f23f43",
        "warn_bg": "#3b3423", "warn_text": "#e6c86b", "section": "#949ba4",
        "mute": "#6d6f78", "ok": "#23a55a", "err": "#f23f43",
    },
    "light": {
        "app_bg": "#eef1f6", "panel": "#ffffff", "panel_2": "#f7f8fb",
        "input_bg": "#f2f4f8", "input_hover": "#e9ebf0",
        "accent": "#1f6feb", "accent_hover": "#1a5fd0",
        "mine_bubble": "#d9e6ff", "mine_text": "#142a52",
        "other_bubble": "#ffffff", "other_text": "#1d1d1f",
        "text": "#1d1d1f", "text_2": "#3a4150", "text_mute": "#9aa0ab",
        "hover": "#f0f2f6", "selected_bg": "#dbe7ff", "selected_text": "#1f6feb",
        "online": "#1a7f37", "danger": "#e5484d",
        "warn_bg": "#fff7e6", "warn_text": "#8a6f1a", "section": "#9aa0ab",
        "mute": "#9aa0ab", "ok": "#1a7f37", "err": "#e5484d",
    },
}

_APPEARANCE = "dark"


def set_appearance(mode, apply_ctk=True):
    """切换全局主题；mode 取 dark / light，非法值回退 dark。

    apply_ctk=False 时只更新内部配色表 _APPEARANCE，不调用 ctk.set_appearance_mode。
    用于主题切换时先重建界面（用新配色）、最后再切 ctk 外观模式，避免标题栏重绘的
    after(1) 焦点恢复回调指向已销毁的旧控件而抛 "bad window path name"。
    """
    global _APPEARANCE
    if mode not in THEMES:
        mode = "dark"
    _APPEARANCE = mode
    if apply_ctk:
        try:
            ctk.set_appearance_mode(mode)
        except Exception:
            pass


def C(key):
    """取当前主题色；未知 key 回退主文字色，避免界面崩溃。"""
    pal = THEMES.get(_APPEARANCE, THEMES["dark"])
    return pal.get(key, pal.get("text", "#ffffff"))


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def is_image(mime: str) -> bool:
    return bool(mime) and mime.startswith("image/")


def fmt_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _md5_file(path, chunk=256 * 1024):
    """流式计算文件 MD5，避免把大文件一次性读进内存。"""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
    except Exception:
        return ""
    return h.hexdigest()


def collect_env_report():
    """实时检测运行环境（pip 包名, 说明, 是否可用）。独立于启动时的缓存标志。"""
    import importlib
    import platform
    checks = [
        ("paho-mqtt", "paho.mqtt.client", "文字 / 文件传输"),
        ("Pillow", "PIL", "图片显示与预览"),
        ("customtkinter", "customtkinter", "圆角现代界面"),
        ("tkinterdnd2", "tkinterdnd2", "拖拽发送文件/图片"),
        ("cryptography", "cryptography", "端到端加密（可选）"),
    ]
    items = []
    for pip_name, mod_name, desc in checks:
        try:
            importlib.import_module(mod_name)
            items.append((pip_name, desc, True))
        except Exception:
            items.append((pip_name, desc, False))
    missing = [p for p, _d, ok in items if not ok]
    return {
        "python": "%d.%d.%d" % sys.version_info[:3],
        "platform": platform.platform(),
        "broker": "%s:%d" % (DEFAULT_BROKER, DEFAULT_PORT),
        "items": items,
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# 数据存储：历史 / 身份 / 房间列表，统一放到 程序(exe)/脚本 所在目录
# ---------------------------------------------------------------------------


def _base_dir():
    """数据根目录：优先 exe/脚本 所在目录（便携）；不可写时退回 APPDATA 下的 P2P聊天目录。

    安装版默认装到 Program Files（普通用户只读），若仍往 exe 目录写历史/头像会失败，
    表现为「头像读取失败、ID 每次变化、聊天记录不保存」。所以这里做可写性探测 + 回退。
    """
    base = (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    try:
        probe = os.path.join(base, ".p2pchat_write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return base
    except Exception:
        pass
    appdata = (os.environ.get("APPDATA")
               or os.environ.get("LOCALAPPDATA")
               or os.path.expanduser("~"))
    d = os.path.join(appdata, "P2P聊天")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


DATA_DIR = os.path.join(_base_dir(), "history")      # 历史 + 身份 + 房间列表
DOWNLOADS_DIR = os.path.join(_base_dir(), "downloads")


def _safe_name(s):
    """把昵称 / 房间名转成安全的文件名（去掉非法字符）。"""
    s = (s or "").strip()
    if not s:
        s = "未命名"
    for ch in '\\/:*?"<>|\n\r\t':
        s = s.replace(ch, "_")
    s = s.strip(". ")
    return s[:60] or "未命名"


def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass


def _load_identity():
    """本机固定身份 ID，跨重启稳定（私聊地址）。"""
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "identity.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
            cid = str(d.get("cid", "")).strip()
            if cid:
                return cid
    except Exception:
        pass
    cid = "chuid-" + uuid.uuid4().hex[:16]
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"cid": cid}, f)
    except Exception:
        pass
    return cid


def _load_profile():
    """读取本机资料：用户ID / 昵称 / 头像路径（无则返回默认空值）。"""
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "profile.json")
    d = {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        pass
    cid = str(d.get("cid", "")).strip() or _load_identity()
    return {
        "cid": cid,
        "name": str(d.get("name", "")).strip(),
        "avatar": str(d.get("avatar", "")).strip(),
    }


def _save_profile(name, avatar):
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "profile.json")
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"cid": _load_identity(), "name": (name or "").strip(),
                       "avatar": avatar or ""}, f, ensure_ascii=False)
    except Exception:
        pass


def _copy_avatar(src_path):
    """把用户选的图片缩略后存成 history/avatar.png，返回保存路径（失败返回空）。

    强制完整解码验证：损坏 / 截断 / 无法读取的图片直接返回空，绝不把坏图复制成头像，
    否则下次启动加载头像时会在渲染阶段崩溃（CTkImage 惰性解码）。
    """
    _ensure_data_dir()
    dst = os.path.join(DATA_DIR, "avatar.png")
    try:
        from PIL import Image
        img = Image.open(src_path)
        img.load()  # 强制解码，截断/损坏图片在此抛异常
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.thumbnail((256, 256))
        img = img.convert("RGB")  # 统一去掉 alpha，避免显示异常
        img.save(dst, "PNG")
        return dst
    except Exception:
        return ""


def _load_ctk_image(path, w, h):
    """加载头像/图片为 CTkImage；失败返回 None。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        from PIL import Image
        from customtkinter import CTkImage
        img = Image.open(path)
        img.load()  # 强制完整解码，避免惰性解码在渲染阶段才崩溃
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img = img.copy()  # 独立内存副本，避免句柄/惰性加载问题
        return CTkImage(light_image=img, dark_image=img, size=(w, h))
    except Exception:
        return None


def _circular_ctk_image(path, size):
    """把头像裁剪成圆形并返回 CTkImage（聊天头像用）；失败返回 None。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        from PIL import Image, ImageDraw
        from customtkinter import CTkImage
        img = Image.open(path)
        img.load()
        img = img.convert("RGBA").copy()
        img = img.resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        return CTkImage(light_image=out, dark_image=out, size=(size, size))
    except Exception:
        return None


def _make_thumb_base64(path, max_size=240, quality=62):
    """生成图片的小缩略图并返回 base64（用于聊天内联预览）；失败返回空串。"""
    try:
        import base64
        import io
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None  # 允许超大原图（手机照片/全景）也能出缩略图
        img = Image.open(path)
        try:
            img.draft("RGB", (max_size * 2, max_size * 2))  # JPEG 先降采样，加速解码
        except Exception:
            pass
        img = img.convert("RGB")
        img.thumbnail((max_size, max_size))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


def _name_color(name):
    """根据昵称生成稳定的头像底色（Discord 风格彩色首字母）。"""
    palette = ["#5865f2", "#3ba55d", "#faa61a", "#ed4245", "#eb459e",
               "#00a8fc", "#9146ff", "#f47fff"]
    h = 0
    for ch in str(name):
        h = (h * 31 + ord(ch)) & 0xffffffff
    return palette[h % len(palette)]


def _play_notify_sound():
    """新消息提示音（Windows 用系统提示音，其它平台静默跳过）。"""
    if os.name != "nt":
        return
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def _notify_windows(title, msg):
    """Windows 原生气泡/吐司通知（Shell_NotifyIcon，无第三方依赖）。
    失败静默返回 False，调用方回退到任务栏闪烁。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                        ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

        class _NID(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", ctypes.c_wchar * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", ctypes.c_wchar * 256),
                ("uTimeout", wintypes.UINT),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", _GUID),
                ("hBalloonIcon", wintypes.HICON),
            ]

        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        nid = _NID()
        nid.cbSize = ctypes.sizeof(_NID)
        nid.uID = 0x1234
        nid.uFlags = 0x10 | 0x2  # NIF_INFO | NIF_ICON
        nid.szInfo = str(msg)[:255]
        nid.szInfoTitle = str(title)[:63]
        nid.dwInfoFlags = 0x1  # NIIF_INFO
        nid.uTimeout = 10000
        nid.hIcon = user32.LoadIconW(None, 32516)  # IDI_INFORMATION
        shell32.Shell_NotifyIconW(0, ctypes.byref(nid))  # NIM_ADD（首次即弹出气泡）
        shell32.Shell_NotifyIconW(1, ctypes.byref(nid))  # NIM_MODIFY（确保弹出）
        # 气泡过期后删除托盘图标，避免残留一个空白图标
        import threading

        def _cleanup():
            try:
                d = _NID()
                d.cbSize = ctypes.sizeof(_NID)
                d.uID = 0x1234
                shell32.Shell_NotifyIconW(2, ctypes.byref(d))  # NIM_DELETE
            except Exception:
                pass

        threading.Timer(12.0, _cleanup).start()
        return True
    except Exception:
        return False


def _load_rooms():
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "rooms.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            arr = json.load(f)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
    except Exception:
        pass
    return []


def _save_rooms(rooms):
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "rooms.json")
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(list(rooms), f, ensure_ascii=False)
    except Exception:
        pass


def _load_settings():
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "settings.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
            return d
    except Exception:
        return {}


def _save_settings(d):
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "settings.json")
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def _update_settings(key, value):
    """合并更新单个设置项，避免整表覆盖时丢掉其它键。"""
    d = _load_settings()
    d[key] = value
    _save_settings(d)


# --------------------------- 历史（按会话） ---------------------------


def _fmt_time(ts):
    """把时间戳格式化成 HH:MM，无效则返回空串。"""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


def _day_label(ts):
    """日期分隔标签：今天 / 昨天 / 具体日期。"""
    try:
        import datetime as _dt
        d = _dt.datetime.fromtimestamp(float(ts))
        today = _dt.date.today()
        if d.date() == today:
            return "今天"
        if (today - d.date()).days == 1:
            return "昨天"
        return d.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _norm_msg(m):
    msg = {"name": str(m.get("name", "？")), "text": m.get("text", ""), "mine": bool(m.get("mine"))}
    if m.get("img_path"):
        msg["img_path"] = str(m["img_path"])
    if m.get("file_path"):
        msg["file_path"] = str(m["file_path"])
    if m.get("system"):
        msg["system"] = True
    if m.get("mid"):
        msg["mid"] = str(m["mid"])
    if m.get("read_by"):
        msg["read_by"] = list(m["read_by"])
    if m.get("delivered_by"):
        msg["delivered_by"] = list(m["delivered_by"])
    if m.get("recalled"):
        msg["recalled"] = True
        if m.get("recalled_by"):
            msg["recalled_by"] = str(m["recalled_by"])
    if m.get("preview_tid"):
        msg["preview_tid"] = str(m["preview_tid"])
    if m.get("ts"):
        msg["ts"] = m["ts"]
    return msg


def _group_path(room):
    return os.path.join(DATA_DIR, "group_" + _safe_name(room) + ".json")


def _dm_path(cid):
    return os.path.join(DATA_DIR, "dm_" + _safe_name(cid or "x") + ".json")


def _load_group_history(room, limit):
    msgs = []
    try:
        with open(_group_path(room), "r", encoding="utf-8") as f:
            raw = json.load(f)
        for m in (raw.get("messages") or []):
            if isinstance(m, dict) and isinstance(m.get("text"), str):
                msgs.append(_norm_msg(m))
    except Exception:
        pass
    return msgs[-limit:]


def _save_group_history(room, msgs):
    _ensure_data_dir()
    try:
        with open(_group_path(room), "w", encoding="utf-8") as f:
            json.dump({"room": room, "messages": msgs}, f, ensure_ascii=False)
    except Exception:
        pass


def _load_dm_history(cid, limit):
    name = ""
    msgs = []
    try:
        with open(_dm_path(cid), "r", encoding="utf-8") as f:
            raw = json.load(f)
        name = str(raw.get("name", ""))
        for m in (raw.get("messages") or []):
            if isinstance(m, dict) and isinstance(m.get("text"), str):
                msgs.append(_norm_msg(m))
    except Exception:
        pass
    return name, msgs[-limit:]


def _save_dm_history(cid, name, msgs):
    _ensure_data_dir()
    try:
        with open(_dm_path(cid), "w", encoding="utf-8") as f:
            json.dump({"name": name, "messages": msgs}, f, ensure_ascii=False)
    except Exception:
        pass


def _delete_group_history(room):
    try:
        p = _group_path(room)
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass


def _delete_dm_history(cid):
    try:
        p = _dm_path(cid)
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass


def _scan_group_rooms():
    """扫描历史里的群聊房间名（配合 rooms.json 之外的兜底 / 旧数据迁移）。"""
    _ensure_data_dir()
    names = []
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("group_") and fn.endswith(".json"):
                room = ""
                try:
                    with open(os.path.join(DATA_DIR, fn), "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    room = str(raw.get("room", "")).strip()
                except Exception:
                    pass
                if not room:
                    room = fn[len("group_"):-len(".json")]
                if room:
                    names.append(room)
    except Exception:
        pass
    return names


def _scan_dm_sessions():
    _ensure_data_dir()
    out = []
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("dm_") and fn.endswith(".json"):
                cid = fn[len("dm_"):-len(".json")]
                name = ""
                try:
                    with open(os.path.join(DATA_DIR, fn), "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    name = str(raw.get("name", ""))
                except Exception:
                    pass
                out.append((cid, name))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# 后端：MQTT（多房间 + 全局私聊通道，不依赖界面，可独立测试）
# ---------------------------------------------------------------------------


class MqttBackend:
    NS = "p2pchat-opensquilla"
    DM_FILE_PREFIX = "@"          # 私聊文件传输伪房间名前缀：@对方cid

    def __init__(self, nickname, cid, broker=DEFAULT_BROKER, port=DEFAULT_PORT,
                 on_text=None, on_peers=None, on_file=None, on_status=None, on_dm=None,
                 on_recall=None, on_read=None, on_typing=None, on_delivered=None, passphrase=""):
        self.nickname = nickname or "匿名"
        self.cid = cid
        self.broker = broker
        self.port = port
        self.fernet = _derive_fernet(passphrase)

        self.on_text = on_text
        self.on_peers = on_peers
        self.on_file = on_file
        self.on_status = on_status
        self.on_dm = on_dm
        self.on_recall = on_recall
        self.on_read = on_read
        self.on_typing = on_typing
        self.on_delivered = on_delivered
        self._delivered_acked = set()  # 已自动回过“已送达”的消息 mid

        self.online = False
        self.running = False
        self._connected_once = False
        self._client = None
        self.rooms = {}              # 房间名 -> roomid
        self._room_by_id = {}        # roomid -> 房间名
        self.presence = {}           # cid -> {"name":.., "rooms":[..]}（全局在线名单）
        self._subscribed = set()

        self._receivers = {}         # 接收方：tid -> 状态
        self._offers = {}            # 接收方：tid -> 对方发来的文件请求
        self._pending = {}           # 发送方：tid -> 待对方同意的文件

    # --------------------------- 主题 ---------------------------

    def _topic_presence(self):
        return f"{self.NS}/presence/{self.cid}"

    def _topic_dm(self):
        return f"{self.NS}/dms/{self.cid}"

    @staticmethod
    def _roomid(room):
        return hashlib.sha256((room or "").strip().encode("utf-8")).hexdigest()[:16]

    def _topic_room(self, room):
        return f"{self.NS}/rooms/{self._roomid(room)}"

    def _file_topic_base(self, room):
        """文件传输话题基址：群聊用房间哈希，私聊（@对方cid）用专属 dmfiles 通道。"""
        if str(room).startswith(self.DM_FILE_PREFIX):
            return f"{self.NS}/dmfiles/{str(room)[len(self.DM_FILE_PREFIX):]}"
        return f"{self.NS}/rooms/{self._roomid(room)}"

    # --------------------------- paho 客户端 ---------------------------

    def _build(self):
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.cid)
        except (AttributeError, TypeError):
            c = mqtt.Client(client_id=self.cid)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.on_disconnect = self._on_disconnect
        c.reconnect_delay_set(1, 30)
        try:
            c.max_inflight_messages_set(128)
        except Exception:
            pass
        try:
            c.max_queued_messages_set(0)
        except Exception:
            pass
        c.will_set(self._topic_presence(), b"", qos=1, retain=True)
        return c

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        ok = (not rc.is_failure) if hasattr(rc, "is_failure") else (rc == 0)
        if ok:
            self.online = True
            self._subscribed = set()
            # 全局在线名单 + 自己的私聊收件箱
            client.subscribe(f"{self.NS}/presence/+", qos=1)
            self._subscribed.add(f"{self.NS}/presence/+")
            client.subscribe(self._topic_dm(), qos=1)
            self._subscribed.add(self._topic_dm())
            # 自己的私聊文件收件箱
            client.subscribe(f"{self.NS}/dmfiles/{self.cid}/#", qos=1)
            self._subscribed.add(f"{self.NS}/dmfiles/{self.cid}/#")
            # 已加入的所有房间
            for room in list(self.rooms.keys()):
                self._subscribe_room(room)
            self._publish_presence()
            msg = "已重新连接" if self._connected_once else "已连接"
            self._connected_once = True
            self._fire_status(True, msg)
        else:
            self.online = False
            self._fire_status(False, f"连接失败（{rc}）")

    def _on_disconnect(self, client, userdata, *args):
        self.online = False
        if self.running:
            self._fire_status(False, "连接断开，正在重连…")
        else:
            self._fire_status(False, "已断开")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        raw = msg.payload if isinstance(msg.payload, bytes) else bytes(msg.payload)
        ns = self.NS
        if topic.startswith(f"{ns}/presence/"):
            self._handle_presence(topic, raw)
        elif topic.startswith(f"{ns}/dms/"):
            self._handle_dm(topic, raw)
        elif topic.startswith(f"{ns}/rooms/"):
            self._handle_room(topic, raw)
        elif topic.startswith(f"{ns}/dmfiles/"):
            self._handle_dmfile(topic, raw)

    def _handle_room(self, topic, raw):
        parts = topic.split("/")     # [ns, 'rooms', roomid, ...]
        if len(parts) < 4:
            return
        roomid = parts[2]
        room = self._room_by_id.get(roomid)
        if room is None:
            return
        tail = parts[3:]
        if tail and tail[0] == "msg":
            self._handle_text(room, raw)
        elif len(tail) >= 2 and tail[0] == "file" and tail[1] == "ctrl":
            self._handle_ctrl(room, raw)
        elif len(tail) >= 2 and tail[0] == "file" and tail[1] == "data":
            self._handle_data(room, raw)

    def _handle_dmfile(self, topic, raw):
        # topic: {ns}/dmfiles/{target_cid}/file/ctrl|data（收件箱按自己的 cid 订阅）
        parts = topic.split("/")
        if len(parts) < 5:
            return
        tail = parts[3:]
        if not tail or tail[0] != "file":
            return
        if len(tail) >= 2 and tail[1] == "ctrl":
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return
            sender = str(data.get("from", ""))
            if sender:
                self._handle_ctrl(self.DM_FILE_PREFIX + sender, raw)
        elif len(tail) >= 2 and tail[1] == "data":
            self._handle_data("", raw)  # data 帧的 room 参数未使用，上下文取自 _receivers[tid]

    # --------------------------- 文本 / 在场 / 私聊 ---------------------------

    def _decrypt_data(self, data):
        """解密 {'enc': ...} 形式的载荷；无密钥或解密失败返回 None。"""
        if self.fernet is None:
            return None
        try:
            plain = self.fernet.decrypt(str(data.get("enc", "")).encode("ascii"))
            return json.loads(plain.decode("utf-8"))
        except Exception:
            return None

    def _handle_text(self, room, raw):
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return
        if isinstance(data, dict) and "enc" in data:
            data = self._decrypt_data(data)
            if data is None:
                self._fire_text(room, "🔒", "收到加密消息（未设置口令或口令不匹配）", False)
                return
        if data.get("kind") == "recall":
            self._fire_recall(room, str(data.get("mid", "")), str(data.get("cid", "")))
            return
        if data.get("kind") == "read":
            self._fire_read(room, str(data.get("mid", "")), str(data.get("cid", "")), str(data.get("name", "匿名")))
            return
        if data.get("kind") == "typing":
            self._fire_typing(room, str(data.get("name", "匿名"))[:60], str(data.get("cid", "")))
            return
        if data.get("kind") == "delivered":
            self._fire_delivered(room, str(data.get("mid", "")), str(data.get("cid", "")), str(data.get("name", "匿名")))
            return
        if data.get("cid") == self.cid:
            return
        self._auto_delivered(room, str(data.get("mid", "")), False)
        self._fire_text(room, str(data.get("name", "匿名"))[:60],
                        str(data.get("text", ""))[:MAX_TEXT], False, str(data.get("mid", "")))

    def _handle_presence(self, topic, raw):
        cid = topic.rsplit("/", 1)[-1]
        if cid == self.cid:
            return
        text = raw.decode("utf-8", "ignore")
        if text:
            try:
                data = json.loads(text)
            except Exception:
                data = {"name": "匿名"}
            name = str(data.get("name", "匿名"))[:60]
            rooms = (data.get("rooms") or [])[:100]
            try:
                ts = float(data.get("ts") or time.time())
            except Exception:
                ts = time.time()
            self.presence[cid] = {"name": name, "rooms": [str(r)[:60] for r in rooms], "ts": ts}
        else:
            self.presence.pop(cid, None)
        self._fire_peers()

    def _prune_presence(self):
        """剔除超过 PRESENCE_TTL 未更新的在线条目（兜底 will 消息丢失导致的幽灵在线）。"""
        now = time.time()
        stale = [cid for cid, p in self.presence.items()
                 if now - float(p.get("ts") or now) > PRESENCE_TTL]
        if not stale:
            return
        for cid in stale:
            self.presence.pop(cid, None)
        self._fire_peers()

    def _prune_loop(self):
        """后台心跳 + 清理：每 30 秒重新发布自己的在线状态（保活）并清理过期条目。

        若只靠上线时发布一次 presence，长时间静默的活跃用户会被 PRESENCE_TTL
        误判为下线；这里周期性刷新自己的 ts，保证「活跃即在线」，只有真正停止
        心跳的离线者才会被 TTL 清掉。
        """
        while self.running:
            time.sleep(30)
            if not self.running:
                break
            try:
                self._prune_presence()
            except Exception:
                pass
            try:
                if self.online and self._client is not None:
                    self._publish_presence()
            except Exception:
                pass

    def _handle_dm(self, topic, raw):
        target = topic.rsplit("/", 1)[-1]
        if target != self.cid:
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return
        if isinstance(data, dict) and "enc" in data:
            data = self._decrypt_data(data)
            if data is None:
                if self.on_dm:
                    self.on_dm("🔒", "🔒", "收到加密消息（未设置口令或口令不匹配）")
                return
        sender = data.get("cid", "")
        if data.get("kind") == "recall":
            self._fire_recall(self.DM_FILE_PREFIX + str(sender), str(data.get("mid", "")), str(sender))
            return
        if data.get("kind") == "read":
            self._fire_read(self.DM_FILE_PREFIX + str(sender), str(data.get("mid", "")), str(sender), str(data.get("name", "匿名")))
            return
        if data.get("kind") == "typing":
            self._fire_typing(self.DM_FILE_PREFIX + str(sender), str(data.get("name", "匿名"))[:60], str(sender))
            return
        if data.get("kind") == "delivered":
            self._fire_delivered(self.DM_FILE_PREFIX + str(sender), str(data.get("mid", "")), str(sender), str(data.get("name", "匿名")))
            return
        if not sender or sender == self.cid:
            return
        self._auto_delivered(self.DM_FILE_PREFIX + str(sender), str(data.get("mid", "")), True)
        if self.on_dm:
            self.on_dm(sender, str(data.get("name", "匿名"))[:60],
                       str(data.get("text", ""))[:MAX_TEXT], str(data.get("mid", "")))

    # --------------------------- 文件控制 ---------------------------

    def _handle_ctrl(self, room, raw):
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return
        kind = data.get("kind")
        if kind == "offer":
            self._on_offer(room, data)
        elif kind == "accept":
            self._on_accept(data)
        elif kind == "reject":
            self._on_reject(data)

    def _on_offer(self, room, data):
        if data.get("from") == self.cid:
            return
        tid = data.get("id")
        size = int(data.get("size", 0))
        if not tid or size < 0 or size > MAX_FILE:
            return
        try:
            total = int(data.get("total", 0))
        except Exception:
            total = 0
        if total != max(1, math.ceil(size / CHUNK_SIZE)):
            return
        data["room"] = room
        self._offers[tid] = data
        self._fire_file(room, "offer", {
            "tid": tid, "name": data.get("name", "file"), "size": size,
            "mime": data.get("mime", ""), "sname": data.get("sname", "匿名"),
            "thumb": data.get("thumb", ""),
        })

    def _on_accept(self, data):
        tid = data.get("id")
        p = self._pending.get(tid)
        if p is None or data.get("to") != self.cid:
            return
        p["accepted"] = True
        p["evt"].set()
        self._fire_file(p["room"], "accepted", {"name": p["name"], "size": p["size"]})

    def _on_reject(self, data):
        tid = data.get("id")
        if data.get("to") != self.cid:
            return
        p = self._pending.pop(tid, None)
        if p is not None:
            p["accepted"] = False
            p["evt"].set()
            self._fire_file(p["room"], "rejected", {"name": p["name"], "msg": "对方拒绝接收"})

    # --------------------------- 文件数据 ---------------------------

    def _handle_data(self, room, raw):
        if len(raw) < 17 or raw[0:1] != b"C":
            return
        try:
            tid = raw[1:13].decode("ascii")
            (idx,) = struct.unpack(">I", raw[13:17])
        except Exception:
            return
        r = self._receivers.get(tid)
        if r is None:
            return
        if idx >= r["total"]:
            return
        if idx in r["received"]:
            return
        piece = raw[17:]
        if r.get("enc"):
            if self.fernet is None:
                return
            try:
                piece = self.fernet.decrypt(piece)
            except Exception:
                return
        try:
            r["fh"].seek(idx * CHUNK_SIZE)
            r["fh"].write(piece)
        except Exception:
            return
        r["received"].add(idx)
        r["got"] += 1
        if r["got"] >= r["total"]:
            self._finish(tid)

    @staticmethod
    def _cleanup_part(tmp):
        try:
            if tmp and os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass

    def _finish(self, tid):
        r = self._receivers.pop(tid)
        try:
            r["fh"].close()
        except Exception:
            pass
        tmp = r.get("tmp")
        if r["got"] < r["total"]:
            self._cleanup_part(tmp)
            self._fire_file(r["room"], "error", {"name": r["name"], "msg": "分片缺失，接收失败"})
            return
        if os.path.getsize(tmp) != r["size"] or _md5_file(tmp) != r["md5"]:
            self._cleanup_part(tmp)
            self._fire_file(r["room"], "error", {"name": r["name"], "msg": "校验失败，文件可能损坏"})
            return
        # 文件名安全化：防止对方伪造带路径穿越的名字（如 ..\..\evil.exe）写出 downloads 目录
        safe_name = os.path.basename((r["name"] or "").replace("\\", "/"))
        if not safe_name or safe_name in (".", ".."):
            safe_name = "file"
        d = DOWNLOADS_DIR
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, safe_name)
        base, ext = os.path.splitext(path)
        k = 1
        while os.path.exists(path):
            path = f"{base}({k}){ext}"
            k += 1
        try:
            os.replace(tmp, path)
        except Exception:
            self._cleanup_part(tmp)
            self._fire_file(r["room"], "error", {"name": safe_name, "msg": "保存文件失败"})
            return
        self._fire_file(r["room"], "done", {
            "name": safe_name, "path": path, "size": r["size"],
            "mime": r["mime"], "sname": r.get("sname", "对方"), "tid": tid,
        })

    def _watch_recv(self, tid):
        """接收方看门狗：超过 RECV_TIMEOUT 仍未收齐数据则清理残留，避免内存/磁盘泄漏。"""
        time.sleep(RECV_TIMEOUT)
        r = self._receivers.get(tid)
        if r is None:
            return
        self._receivers.pop(tid, None)
        try:
            r["fh"].close()
        except Exception:
            pass
        self._cleanup_part(r.get("tmp"))
        self._fire_file(r["room"], "error", {"name": r["name"], "msg": "接收超时，传输中断"})

    # --------------------------- 对外方法 ---------------------------

    def start(self):
        if not _MQTT_OK:
            self._fire_status(False, "缺少 paho-mqtt 库（pip install paho-mqtt）")
            return
        self.running = True
        self._client = self._build()
        self._client.loop_start()
        threading.Thread(target=self._prune_loop, daemon=True).start()
        try:
            self._client.connect_async(self.broker, self.port, keepalive=30)
        except Exception as e:
            self._fire_status(False, f"连接失败：{e}")
            self.running = False

    def stop(self):
        self.running = False
        for p in self._pending.values():
            p["evt"].set()
        self._pending.clear()
        self._offers.clear()
        for r in self._receivers.values():
            try:
                r["fh"].close()
            except Exception:
                pass
            self._cleanup_part(r.get("tmp"))
        self._receivers.clear()
        self.presence.clear()
        client = self._client
        self._client = None
        self.online = False
        if client is None:
            return
        try:
            client.publish(self._topic_presence(), b"", qos=0, retain=True)
        except Exception:
            pass
        def _teardown():
            try:
                client.disconnect()
            except Exception:
                pass
            try:
                client.loop_stop(True)
            except TypeError:
                try:
                    client.loop_stop()
                except Exception:
                    pass
            except Exception:
                pass
        threading.Thread(target=_teardown, daemon=True).start()

    # --------------------------- 房间管理 ---------------------------

    def add_room(self, room):
        room = (room or "").strip()
        if not room or room in self.rooms:
            return False
        rid = self._roomid(room)
        self.rooms[room] = rid
        self._room_by_id[rid] = room
        if self.online and self._client:
            self._subscribe_room(room)
            self._publish_presence()
        return True

    def remove_room(self, room):
        room = (room or "").strip()
        if room not in self.rooms:
            return False
        rid = self.rooms.pop(room)
        self._room_by_id.pop(rid, None)
        if self.online and self._client:
            topic = f"{self._topic_room(room)}/#"
            try:
                self._client.unsubscribe(topic)
            except Exception:
                pass
            self._subscribed.discard(topic)
            self._publish_presence()
        return True

    def _subscribe_room(self, room):
        topic = f"{self._topic_room(room)}/#"
        if topic in self._subscribed:
            return
        self._client.subscribe(topic, qos=1)
        self._subscribed.add(topic)

    def _publish_presence(self):
        if self._client is None:
            return
        payload = json.dumps({
            "name": self.nickname,
            "rooms": list(self.rooms.keys()),
            "ts": int(time.time()),
        }, ensure_ascii=False)
        self._client.publish(self._topic_presence(), payload, qos=1, retain=True)

    def _publish_ctrl(self, room, obj):
        if self._client is None:
            return
        self._client.publish(self._file_topic_base(room) + "/file/ctrl",
                             json.dumps(obj, ensure_ascii=False), qos=1)

    def change_nick(self, new):
        new = (new or "").strip() or "匿名"
        self.nickname = new
        self._publish_presence()

    # --------------------------- 发送 ---------------------------

    def send_text(self, room, text):
        text = (text or "").strip()
        if not text or room not in self.rooms or not self.online or self._client is None:
            return False
        mid = uuid.uuid4().hex[:12]
        payload = json.dumps({"name": self.nickname, "text": text, "cid": self.cid, "mid": mid}, ensure_ascii=False)
        if self.fernet is not None:
            payload = json.dumps({"enc": self.fernet.encrypt(payload.encode("utf-8")).decode("ascii")})
        self._client.publish(self._topic_room(room) + "/msg", payload, qos=1)
        self._fire_text(room, self.nickname, text, True, mid)
        return True

    def send_dm(self, target_cid, text):
        text = (text or "").strip()
        if not text or not self.online or self._client is None or not target_cid:
            return False
        mid = uuid.uuid4().hex[:12]
        payload = json.dumps({"name": self.nickname, "text": text, "cid": self.cid, "mid": mid}, ensure_ascii=False)
        if self.fernet is not None:
            payload = json.dumps({"enc": self.fernet.encrypt(payload.encode("utf-8")).decode("ascii")})
        self._client.publish(f"{self.NS}/dms/{target_cid}", payload, qos=1)
        return True

    def send_recall(self, target, mid, is_dm=False):
        """撤回一条消息：向房间/私聊广播撤回指令。"""
        if not mid or not self.online or self._client is None:
            return False
        payload = json.dumps({"kind": "recall", "mid": mid, "cid": self.cid}, ensure_ascii=False)
        if is_dm:
            self._client.publish(f"{self.NS}/dms/{target}", payload, qos=1)
        else:
            self._client.publish(self._topic_room(target) + "/msg", payload, qos=1)
        return True

    def send_read(self, target, mid, is_dm=False):
        """发送已读回执：告知对方我已看到该消息。"""
        if not mid or not self.online or self._client is None:
            return False
        payload = json.dumps({"kind": "read", "mid": mid, "cid": self.cid, "name": self.nickname}, ensure_ascii=False)
        if is_dm:
            self._client.publish(f"{self.NS}/dms/{target}", payload, qos=1)
        else:
            self._client.publish(self._topic_room(target) + "/msg", payload, qos=1)
        return True

    def send_typing(self, target, is_dm=False):
        """广播“正在输入”状态（qos=0，瞬时、不持久化）。"""
        if not self.online or self._client is None or not target:
            return False
        payload = json.dumps({"kind": "typing", "cid": self.cid, "name": self.nickname}, ensure_ascii=False)
        if is_dm:
            self._client.publish(f"{self.NS}/dms/{target}", payload, qos=0)
        else:
            self._client.publish(self._topic_room(target) + "/msg", payload, qos=0)
        return True

    def send_delivered(self, target, mid, is_dm=False):
        """发送“已送达”回执：我已收到该消息（尚未打开也算送达）。"""
        if not mid or not self.online or self._client is None:
            return False
        payload = json.dumps({"kind": "delivered", "mid": mid, "cid": self.cid, "name": self.nickname}, ensure_ascii=False)
        if is_dm:
            self._client.publish(f"{self.NS}/dms/{target}", payload, qos=1)
        else:
            self._client.publish(self._topic_room(target) + "/msg", payload, qos=1)
        return True

    def _auto_delivered(self, room, mid, is_dm):
        """收到对方消息时自动回一次“已送达”（每条 mid 只回一次）。"""
        if not mid or mid in self._delivered_acked:
            return
        self._delivered_acked.add(mid)
        target = room
        if is_dm:
            target = str(room)[1:] if str(room).startswith(self.DM_FILE_PREFIX) else room
        self.send_delivered(target, mid, is_dm)

    def send_file(self, room, path):
        if room not in self.rooms or not self.online or self._client is None:
            return False
        return self._send_file_to(room, path)

    def send_file_dm(self, target_cid, path):
        if not self.online or self._client is None or not target_cid:
            return False
        return self._send_file_to(self.DM_FILE_PREFIX + str(target_cid), path)

    def _send_file_to(self, room, path):
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return False
        size = os.path.getsize(path)
        if size <= 0:
            self._fire_file(room, "error", {"name": os.path.basename(path), "msg": "文件为空，无法发送"})
            return False
        if size > MAX_FILE:
            self._fire_file(room, "error", {"name": os.path.basename(path), "msg": "超过 200MB 上限"})
            return False
        name = os.path.basename(path)
        mime = guess_mime(name)
        total = max(1, math.ceil(size / CHUNK_SIZE))
        md5 = _md5_file(path)
        tid = uuid.uuid4().hex[:12]

        self._pending[tid] = {
            "name": name, "size": size, "mime": mime, "total": total,
            "md5": md5, "path": path, "room": room,
            "accepted": False, "evt": threading.Event(), "enc": self.fernet is not None,
        }
        offer = {"kind": "offer", "id": tid, "from": self.cid, "sname": self.nickname,
                 "name": name, "size": size, "mime": mime, "total": total, "md5": md5}
        if self.fernet is not None:
            offer["enc"] = True
        if is_image(mime):
            thumb = _make_thumb_base64(path)
            if thumb:
                offer["thumb"] = thumb
        self._publish_ctrl(room, offer)
        threading.Thread(target=self._watch_send, args=(tid,), daemon=True).start()
        self._fire_file(room, "waiting", {"name": name, "size": size})
        return True

    def accept_file(self, tid):
        data = self._offers.pop(tid, None)
        if data is None:
            return
        room = data.get("room")
        if data.get("enc") and self.fernet is None:
            self._fire_file(room, "error", {"name": data["name"], "msg": "对方发送了加密文件，但你未设置加密口令"})
            return
        d = DOWNLOADS_DIR
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, ".p2pchat-part-" + tid)
        try:
            fh = open(tmp, "wb")
        except Exception:
            self._fire_file(room, "error", {"name": data["name"], "msg": "无法创建接收文件"})
            return
        self._receivers[tid] = {
            "name": data["name"], "size": data["size"], "mime": data["mime"],
            "total": data["total"], "md5": data["md5"],
            "received": set(), "got": 0, "fh": fh, "tmp": tmp,
            "sname": data.get("sname", "对方"), "room": room, "enc": data.get("enc"),
        }
        threading.Thread(target=self._watch_recv, args=(tid,), daemon=True).start()
        self._publish_ctrl(room, {"kind": "accept", "id": tid, "from": self.cid, "to": data["from"]})
        self._fire_file(room, "accepting", {"name": data["name"], "size": data["size"]})

    def reject_file(self, tid):
        data = self._offers.pop(tid, None)
        if data is None:
            return
        self._publish_ctrl(data.get("room"),
                           {"kind": "reject", "id": tid, "from": self.cid, "to": data["from"], "reason": "rejected"})

    # --------------------------- 文件发送流程 ---------------------------

    def _watch_send(self, tid):
        p = self._pending.get(tid)
        if p is None:
            return
        p["evt"].wait(OFFER_TIMEOUT)
        p = self._pending.get(tid)
        if p is None:
            return
        if not p.get("accepted"):
            self._pending.pop(tid, None)
            self._fire_file(p["room"], "error", {"name": p["name"], "msg": "对方未响应，已取消"})
            return
        self._send_chunks(tid)

    def _send_chunks(self, tid):
        p = self._pending.get(tid)
        if p is None:
            return
        total, name = p["total"], p["name"]
        room = p["room"]
        path = p["path"]
        data_topic = self._file_topic_base(room) + "/file/data"
        try:
            fh = open(path, "rb")
        except Exception:
            self._pending.pop(tid, None)
            self._fire_file(room, "error", {"name": name, "msg": "读取文件失败"})
            return
        try:
            for i in range(total):
                if not self.online or self._client is None:
                    self._pending.pop(tid, None)
                    self._fire_file(room, "error", {"name": name, "msg": "发送中断，连接已断开"})
                    return
                piece = fh.read(CHUNK_SIZE)
                if not piece:
                    break
                if p.get("enc") and self.fernet is not None:
                    piece = self.fernet.encrypt(piece)
                frame = b"C" + tid.encode("ascii") + struct.pack(">I", i) + piece
                try:
                    self._client.publish(data_topic, frame, qos=1)
                except Exception:
                    self._pending.pop(tid, None)
                    self._fire_file(room, "error", {"name": name, "msg": "发送出错"})
                    return
                if i % 8 == 0 or i + 1 == total:
                    self._fire_file(room, "progress", {"name": name, "percent": int((i + 1) / total * 100)})
        finally:
            fh.close()
        self._pending.pop(tid, None)
        self._fire_file(room, "sent", {"name": name, "size": p["size"], "mime": p["mime"], "path": p["path"]})

    # --------------------------- 回调触发 ---------------------------

    def _fire_text(self, room, name, text, mine=False, mid=None):
        if self.on_text:
            self.on_text(room, name, text, mine, mid)

    def _fire_recall(self, room, mid, who):
        if self.on_recall:
            self.on_recall(room, mid, who)

    def _fire_read(self, room, mid, cid, name):
        if self.on_read:
            self.on_read(room, mid, cid, name)

    def _fire_typing(self, room, name, cid):
        if self.on_typing:
            self.on_typing(room, name, cid)

    def _fire_delivered(self, room, mid, cid, name):
        if self.on_delivered:
            self.on_delivered(room, mid, cid, name)

    def _fire_peers(self):
        if self.on_peers:
            self.on_peers({cid: {"name": p["name"], "rooms": list(p.get("rooms") or [])}
                           for cid, p in self.presence.items()})

    def _fire_file(self, room, event, info):
        if self.on_file:
            self.on_file(room, event, info)

    def _fire_status(self, online, msg):
        if self.on_status:
            self.on_status(online, msg)


# ---------------------------------------------------------------------------
# 图形界面（customtkinter · QQ 式单窗口：左会话列表 + 右聊天区）
# ---------------------------------------------------------------------------


def _ver_parts(v):
    """把版本号字符串转成数字元组（如 2.1.0 / v2.1.0 -> (2,1,0)），便于比较排序。
    每段开头可能带非数字前缀（如 GitHub tag 的 "v"），需要跳过而不是当 0。"""
    out = []
    for tok in str(v).split("."):
        num = ""
        i = 0
        while i < len(tok) and not tok[i].isdigit():
            i += 1
        while i < len(tok) and tok[i].isdigit():
            num += tok[i]
            i += 1
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return out


class ChatApp:
    FEED_MAX = 400         # 每个会话持久化的历史消息上限
    RENDER_MAX = 200       # 切换会话时最多立即渲染的消息条数（更早的折叠；放开以换取流畅）
    GROUP_GAP = 300        # 同一发送者连续消息合并的间隔（秒，5 分钟）

    GROUP_PREFIX = "room|"
    DM_PREFIX = "dm|"

    def __init__(self, root, profile=None, name="", avatar=""):
        self.root = root
        self.cid = (profile or {}).get("cid") or _load_identity()
        self._profile_name = name
        self._avatar = avatar
        self.appearance = _APPEARANCE
        self.backend = None
        self._peers = {}            # cid -> {"name":.., "rooms":[..]}
        self._rooms = []            # 已加入房间（有序）
        self._sessions = {}         # key -> 会话（群聊 + 私聊）
        self._current = None
        self._images = []
        self._pending_offers = {}
        self._hint_active = True
        self._thumb_cache = {}      # 图片缩略图缓存：path -> CTkImage
        self._my_avatar_ctk = None  # 自己的圆形头像缓存
        self._last_title = ""       # 窗口标题缓存（避免频繁重设）
        self._read_acked = set()     # 已发送过已读回执的消息 mid
        self._stick_bottom = True   # 新消息到达前用户是否贴底（自动滚动判断）
        self._search_query = ""      # 会话内消息搜索关键词（空 = 未搜索）
        self._msg_search_after = None  # 消息搜索防抖 timer id
        self._suppress_auto_scroll = False  # 全量渲染时抑制逐条自动滚动，避免布局抖动/残影
        self._window_focused = True    # 窗口是否聚焦（后台/最小化时不聚焦，用于弹通知）
        self._typing_after = None      # “正在输入”提示的延时恢复 timer id
        self._typing_last = 0.0        # 上次发送“正在输入”广播的时间戳（节流用）
        self._search_after = None   # 搜索防抖 timer id
        self._list_after = None     # 会话列表防抖 timer id
        self.auto_connect = bool(_load_settings().get("auto_connect", True))
        self._history_expanded = False  # 是否已展开“更早消息”
        self.notify_sound = bool(_load_settings().get("notify_sound", True))
        self.encrypt_pass = str(_load_settings().get("encrypt_pass", "") or "")

        self.root.title("P2P 聊天")
        saved_geo = str(_load_settings().get("window_geometry", "") or "").strip()
        if saved_geo and re.fullmatch(r"\d{3,5}x\d{3,5}([+-]\d+[+-]\d+)?", saved_geo):
            self.root.geometry(saved_geo)
        else:
            self.root.geometry("1000x680")
        self.root.minsize(820, 560)
        self.root.configure(fg_color=C("app_bg"))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._build_menu()

        # 恢复房间与会话
        self._rooms = _load_rooms() or _scan_group_rooms()
        if not self._rooms:
            default = self.room_var.get().strip() or "默认房间"
            self._rooms = [default]
            _save_rooms(self._rooms)
        for room in self._rooms:
            self._ensure_group_session(room)
        for cid, name in _scan_dm_sessions():
            self._ensure_dm_session(cid, name)
        self._current = self._group_key(self._rooms[0])

        self._apply_session_list()
        self._render_feed()
        self._set_status("未连接", "mute")
        self._show_system("顶部输入房间名后点「＋ 加入」可加多个房间；点「连接」开始聊天。文字/图片/文件都能发。")
        if self.auto_connect:
            self.root.after(400, self._auto_connect_on_startup)

    # --------------------------- UI 构建 ---------------------------

    def _build_ui(self):
        # 顶部工具条：头像 / 昵称 / ID / 房间 / 加入 / 连接 / 主题
        top = ctk.CTkFrame(self.root, corner_radius=0, fg_color=C("panel"))
        top.pack(fill="x")

        # 右侧按钮（检查更新 / 主题）先占位：pack 顺序靠前，窗口较窄时也
        # 优先保留它们的空间，避免被左侧较宽的控件（昵称/房间/加入/连接）挤掉点不到。
        self.update_btn = ctk.CTkButton(top, text="🔄", width=40, height=32, corner_radius=8,
                                          fg_color=C("input_bg"), hover_color=C("input_hover"),
                                          text_color=C("text_2"), font=(FONT, 14),
                                          command=self._manual_check_update)
        self.update_btn.pack(side="right", padx=(0, 8), pady=12)
        self.theme_btn = ctk.CTkButton(top, text=("☀️" if self.appearance == "dark" else "🌙"),
                                        width=40, height=32, corner_radius=8,
                                        fg_color=C("input_bg"), hover_color=C("input_hover"),
                                        text_color=C("text_2"), font=(FONT, 14),
                                        command=self._toggle_theme)
        self.theme_btn.pack(side="right", padx=(0, 12), pady=12)

        self.top_avatar = ctk.CTkLabel(top, text="", width=34, height=34,
                                       corner_radius=17, fg_color=C("input_bg"), cursor="hand2")
        self.top_avatar.pack(side="left", padx=(14, 6), pady=12)
        self._render_top_avatar()
        self.top_avatar.bind("<Button-1>", lambda e: self._change_avatar())

        ctk.CTkLabel(top, text="昵称", text_color=C("text_mute"), font=(FONT, 11)).pack(side="left", padx=(0, 5), pady=12)
        self.nick_var = ctk.StringVar(value=self._profile_name)
        self.nick_var.trace_add("write", lambda *_: self._on_nick_changed())
        ctk.CTkEntry(top, textvariable=self.nick_var, width=108, height=32,
                     corner_radius=8, border_width=0, fg_color=C("input_bg"),
                     text_color=C("text"), font=(FONT, 12)).pack(side="left", padx=(0, 8), pady=12)

        self.id_btn = ctk.CTkButton(top, text="📋 复制ID", width=86, height=32,
                                    corner_radius=8, fg_color=C("input_bg"),
                                    text_color=C("text_2"), hover_color=C("input_hover"),
                                    font=(FONT, 11), command=self._copy_my_id)
        self.id_btn.pack(side="left", padx=(0, 10), pady=12)

        ctk.CTkLabel(top, text="房间", text_color=C("text_mute"), font=(FONT, 11)).pack(side="left", padx=(0, 5), pady=12)
        self.room_var = ctk.StringVar(value="")
        self.room_combo = ctk.CTkComboBox(top, variable=self.room_var, width=150, height=32,
                                          corner_radius=8, border_width=0, fg_color=C("input_bg"),
                                          button_color=C("input_hover"), button_hover_color=C("input_hover"),
                                          text_color=C("text"), dropdown_fg_color=C("panel"),
                                          dropdown_text_color=C("text"), dropdown_hover_color=C("hover"),
                                          font=(FONT, 12), dropdown_font=(FONT, 12),
                                          values=[])
        self.room_combo.pack(side="left", padx=(0, 8), pady=12)
        self.room_combo.bind("<FocusIn>", lambda e: self._refresh_room_combo())

        self.add_room_btn = ctk.CTkButton(top, text="＋ 加入", width=74, height=32,
                                          corner_radius=8, font=(FONT, 12, "bold"),
                                          fg_color=C("accent"), hover_color=C("accent_hover"),
                                          command=self._add_room_from_input)
        self.add_room_btn.pack(side="left", pady=12, padx=(0, 8))

        self.connect_btn = ctk.CTkButton(top, text="连接", width=84, height=32,
                                         corner_radius=8, font=(FONT, 12, "bold"),
                                         fg_color=C("accent"), hover_color=C("accent_hover"),
                                         command=self._toggle_connect)
        self.connect_btn.pack(side="left", pady=12, padx=(0, 8))

        # 状态栏
        self.status_var = ctk.StringVar(value="未连接")
        self.status_label = ctk.CTkLabel(self.root, textvariable=self.status_var,
                                         anchor="w", font=(FONT, 11),
                                         text_color=C("text_mute"), fg_color=C("app_bg"))
        self.status_label.pack(fill="x", padx=18, pady=(6, 4))

        # 主体
        body = ctk.CTkFrame(self.root, fg_color=C("app_bg"))
        body.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        # 左：会话列表
        left = ctk.CTkFrame(body, corner_radius=12, fg_color=C("panel"), width=248)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._schedule_session_refresh())
        ctk.CTkEntry(left, textvariable=self.search_var, height=32,
                     corner_radius=8, border_width=0, fg_color=C("input_bg"),
                     text_color=C("text"), placeholder_text_color=C("text_mute"),
                     placeholder_text="搜索会话 / 成员",
                     font=(FONT, 12)).pack(fill="x", padx=10, pady=(12, 6))
        self.session_frame = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.session_frame.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        # 右：当前会话聊天区
        right = ctk.CTkFrame(body, corner_radius=12, fg_color=C("panel_2"))
        right.pack(side="left", fill="both", expand=True)

        self.chat_title = ctk.CTkLabel(right, text="群聊", font=(FONT, 13, "bold"),
                                       text_color=C("text"), anchor="w")
        self.chat_title.pack(fill="x", padx=16, pady=(14, 2))

        # 会话内消息搜索栏（默认隐藏，Ctrl+F 打开）
        self.search_frame = ctk.CTkFrame(right, fg_color="transparent")
        self.search_entry = ctk.CTkEntry(self.search_frame, placeholder_text="搜索消息…",
                                         height=30, corner_radius=8, font=(FONT, 12),
                                         fg_color=C("input_bg"), text_color=C("text"),
                                         border_width=0)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(16, 6))
        self.search_entry.bind("<KeyRelease>", self._on_search_change)
        self.search_entry.bind("<Escape>", lambda e: self._close_search())
        ctk.CTkButton(self.search_frame, text="✕", width=28, height=28, corner_radius=8,
                      fg_color=C("input_bg"), text_color=C("text_2"),
                      hover_color=C("input_hover"), font=(FONT, 12),
                      command=self._close_search).pack(side="left", padx=(0, 16))

        self.feed = ctk.CTkScrollableFrame(right, fg_color="transparent", corner_radius=0)
        self.feed.pack(fill="both", expand=True, padx=6, pady=2)
        if _DND_READY:
            try:
                self.feed.drop_target_register(DND_FILES)
                self.feed.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        # 底部输入区
        ibar = ctk.CTkFrame(right, corner_radius=12, fg_color=C("panel"))
        ibar.pack(fill="x", padx=8, pady=(4, 10))

        self.input_box = ctk.CTkTextbox(ibar, height=72, corner_radius=10, border_width=0,
                                        fg_color=C("input_bg"), text_color=C("text_mute"),
                                        font=(FONT, 12), wrap="word")
        self.input_box.pack(side="left", fill="x", expand=True, padx=(14, 10), pady=14)
        self.input_box.insert("1.0", HINT)
        self.input_box.bind("<Return>", self._on_enter)
        self.input_box.bind("<Control-v>", self._on_paste)
        self.input_box.bind("<Control-V>", self._on_paste)
        self.input_box.bind("<KeyRelease>", self._on_input_key)
        self.input_box.bind("<FocusIn>", self._on_input_focus_in)
        self.input_box.bind("<FocusOut>", self._on_input_focus_out)
        if _DND_READY:
            try:
                self.input_box.drop_target_register(DND_FILES)
                self.input_box.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        # Ctrl+F 打开会话内消息搜索
        self.root.bind("<Control-f>", self._open_search)
        self.root.bind("<Control-F>", self._open_search)
        # 焦点跟踪：后台/最小化时收到新消息弹 Windows 通知
        self.root.bind("<FocusIn>", lambda e: setattr(self, "_window_focused", True))
        self.root.bind("<FocusOut>", lambda e: setattr(self, "_window_focused", False))

        btncol = ctk.CTkFrame(ibar, fg_color="transparent")
        btncol.pack(side="right", padx=(0, 14), pady=14)
        ctk.CTkButton(btncol, text="发送", width=88, height=34, corner_radius=8,
                      font=(FONT, 12, "bold"), fg_color=C("accent"),
                      hover_color=C("accent_hover"), command=self._send_text).pack(fill="x", pady=2)
        ctk.CTkButton(btncol, text="📎 文件/图片", width=88, height=30, corner_radius=8,
                      fg_color=C("input_bg"), text_color=C("text_2"), hover_color=C("input_hover"),
                      font=(FONT, 12), command=self._pick_file).pack(fill="x", pady=3)
        ctk.CTkButton(btncol, text="😊 表情", width=88, height=30, corner_radius=8,
                      fg_color=C("input_bg"), text_color=C("text_2"), hover_color=C("input_hover"),
                      font=(FONT, 12), command=self._toggle_emoji_panel).pack(fill="x", pady=3)

    # --------------------------- 菜单 / 环境检测 ---------------------------

    def _build_menu(self):
        try:
            menubar = tk.Menu(self.root)
            view_menu = tk.Menu(menubar, tearoff=0)
            view_menu.add_command(label="深色主题", command=lambda: self._set_theme("dark"))
            view_menu.add_command(label="浅色主题", command=lambda: self._set_theme("light"))
            menubar.add_cascade(label="视图", menu=view_menu)
            settings_menu = tk.Menu(menubar, tearoff=0)
            self._auto_conn_var = tk.BooleanVar(value=self.auto_connect)
            settings_menu.add_checkbutton(label="启动时自动连接", variable=self._auto_conn_var,
                                          command=self._toggle_auto_connect)
            self._sound_var = tk.BooleanVar(value=self.notify_sound)
            settings_menu.add_checkbutton(label="新消息提示音", variable=self._sound_var,
                                          command=self._toggle_notify_sound)
            settings_menu.add_separator()
            settings_menu.add_command(label="加密口令…", command=self._set_encrypt_pass)
            menubar.add_cascade(label="设置", menu=settings_menu)
            help_menu = tk.Menu(menubar, tearoff=0)
            help_menu.add_command(label="检查更新", command=self._manual_check_update)
            help_menu.add_command(label="环境检测 / 关于", command=self._show_about)
            help_menu.add_separator()
            help_menu.add_command(label="清空当前会话记录", command=self._clear_current_history)
            help_menu.add_command(label="清空所有会话记录", command=self._clear_all_history)
            help_menu.add_command(label="打开收件文件夹", command=self._open_downloads)
            menubar.add_cascade(label="帮助", menu=help_menu)
            self.root.config(menu=menubar)
        except Exception:
            pass

    def _toggle_auto_connect(self):
        self.auto_connect = bool(self._auto_conn_var.get())
        _update_settings("auto_connect", self.auto_connect)

    def _toggle_notify_sound(self):
        self.notify_sound = bool(self._sound_var.get())
        _update_settings("notify_sound", self.notify_sound)

    def _set_encrypt_pass(self):
        """设置端到端加密口令：留空则关闭加密。双方需用相同口令才能互看消息。"""
        try:
            from tkinter import simpledialog
            cur = self.encrypt_pass or ""
            v = simpledialog.askstring(
                "加密口令",
                "设置端到端加密口令（留空则关闭加密）：\n\n"
                "同一房间 / 私聊的双方必须使用相同口令，\n"
                "否则互相只能看到「🔒 加密消息」。",
                initialvalue=cur)
            if v is None:
                return
            v = v.strip()
            self.encrypt_pass = v
            _update_settings("encrypt_pass", v)
            if self.backend:
                self.backend.fernet = _derive_fernet(v)
            self._set_status("已开启端到端加密" if v else "已关闭端到端加密", "ok")
        except Exception:
            pass

    def _auto_connect_on_startup(self):
        if not (self.backend and self.backend.running):
            try:
                self._toggle_connect()
            except Exception:
                pass

    # --------------------------- 主题切换 ---------------------------

    def _toggle_theme(self):
        self._set_theme("light" if self.appearance == "dark" else "dark")

    def _set_theme(self, mode):
        if mode not in THEMES or mode == self.appearance:
            return
        self.appearance = mode
        _update_settings("appearance", mode)
        # 先更新配色并重建界面，最后再切换 ctk 外观模式。ctk.set_appearance_mode 会触发
        # Windows 标题栏重绘（withdraw/deiconify）并 after(1) 恢复焦点；若在重建前调用，
        # 焦点恢复会指向已销毁的旧控件而崩溃。
        set_appearance(mode, apply_ctk=False)
        self._rebuild_ui()
        try:
            ctk.set_appearance_mode(mode)
        except Exception:
            pass

    def _rebuild_ui(self):
        # 主题切换：销毁并重建全部控件（会话/历史状态保存在 self 里，不丢）
        nick = ""
        try:
            nick = self.nick_var.get().strip()
        except Exception:
            nick = self._profile_name
        for w in self.root.winfo_children():
            w.destroy()
        self._images = []
        self._thumb_cache = {}
        self._my_avatar_ctk = None
        self.root.configure(fg_color=C("app_bg"))
        self._build_ui()
        self._build_menu()
        self.nick_var.set(nick or self._profile_name or "未命名")
        self._apply_session_list()
        self._render_feed()
        self._update_window_title()

    # --------------------------- 搜索防抖 ---------------------------

    def _schedule_session_refresh(self):
        if self._search_after is not None:
            try:
                self.root.after_cancel(self._search_after)
            except Exception:
                pass
        self._search_after = self.root.after(60, self._apply_session_list)

    def _schedule_session_list(self):
        """合并短时间内的多次列表重建（presence 连串更新时避免抖动）。"""
        if self._list_after is not None:
            try:
                self.root.after_cancel(self._list_after)
            except Exception:
                pass
        self._list_after = self.root.after(100, self._apply_session_list)

    def _open_downloads(self):
        try:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            if os.name == "nt":
                os.startfile(DOWNLOADS_DIR)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", DOWNLOADS_DIR])
        except Exception as e:
            messagebox.showerror("打开失败", str(e))

    def _open_image(self, path):
        ImagePreview(self.root, path)

    def _render_top_avatar(self):
        img = _load_ctk_image(self._avatar, 34, 34) if self._avatar else None
        if img is not None:
            self.top_avatar.configure(image=img, text="", fg_color="transparent")
            self._avatar_ref = img
        else:
            name = self._profile_name or "?"
            self.top_avatar.configure(image=None, text=name[:1].upper(),
                                      fg_color=_name_color(name),
                                      text_color="#ffffff", font=(FONT, 15, "bold"))

    def _change_avatar(self):
        # 直接在主界面换头像，不再弹独立登录框
        path = filedialog.askopenfilename(
            title="选择头像",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("所有文件", "*.*")])
        if not path:
            return
        saved = _copy_avatar(path)
        if saved:
            self._avatar = saved
            self._my_avatar_ctk = None
            self._profile_name = self.nick_var.get().strip() or "未命名"
            _save_profile(self._profile_name, self._avatar)
            self._render_top_avatar()
            self._set_status("头像已更新", "ok")
        else:
            messagebox.showerror("头像", "读取该图片失败，请换一张试试。")

    def _copy_my_id(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.cid)
            self._set_status("已复制用户 ID：" + self.cid, "ok")
        except Exception:
            pass

    def _show_about(self):
        AboutDialog(self.root)

    def _fetch_releases(self):
        """拉取全部 GitHub Releases（最多 100 个）。"""
        import urllib.request
        url = (f"https://api.github.com/repos/{UPDATE_OWNER}/{UPDATE_REPO}"
               f"/releases?per_page=100")
        req = urllib.request.Request(url, headers={
            "User-Agent": "P2PChat-App",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _show_version_manager(self, rels):
        """用版本列表打开版本管理对话框（支持升级 + 回退）。"""
        versions = []
        for r in rels:
            tag = str(r.get("tag_name", "")).lstrip("vV").strip()
            if not tag:
                continue
            versions.append({
                "tag": tag,
                "body": (str(r.get("body", "")).strip()
                         .replace("\r\n", "\n").replace("\r", "\n")),
                "dl": self._pick_asset_url(r),
                "html": r.get("html_url", ""),
            })
        if not versions:
            self._set_status("没有可用版本", "err")
            return
        try:
            dlg = VersionManagerDialog(self.root, versions, APP_VERSION,
                                       self._download_and_run_installer)
            self.root.wait_window(dlg.top)
        except Exception:
            pass

    def check_for_update(self):
        """后台静默检查更新：若发现新版本，打开版本管理（展示累积更新内容）。"""
        def work():
            try:
                rels = self._fetch_releases()
                newest = ""
                for r in rels:
                    t = str(r.get("tag_name", "")).lstrip("vV").strip()
                    if t and _ver_parts(t) > _ver_parts(newest):
                        newest = t
                if newest and _ver_parts(newest) > _ver_parts(APP_VERSION):
                    self.root.after(0, lambda: self._show_version_manager(rels))
            except Exception:
                pass  # 检查失败静默忽略，不打扰用户

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _is_newer(latest, cur):
        """比较版本号字符串（如 1.2.0 > 1.1.0）。"""
        def parts(s):
            out = []
            for tok in str(s).split("."):
                num = ""
                for ch in tok:
                    if ch.isdigit():
                        num += ch
                    else:
                        break
                out.append(int(num) if num else 0)
            return out
        lp, cp = parts(latest), parts(cur)
        while len(lp) < len(cp):
            lp.append(0)
        while len(cp) < len(lp):
            cp.append(0)
        return lp > cp

    @staticmethod
    def _pick_asset_url(data):
        """从 GitHub release 数据里挑出安装包（P2PChat-Setup.exe）的下载地址。"""
        try:
            for a in (data.get("assets") or []):
                n = str(a.get("name", "")).lower()
                if n.endswith(".exe") and "setup" in n:
                    return str(a.get("browser_download_url", ""))
        except Exception:
            pass
        return ""

    def _prompt_update(self, latest, body, notes, download_url=""):
        try:
            dlg = UpdateDialog(self.root, latest, body, notes, download_url,
                               download_cb=None if not download_url else
                               (lambda u=download_url: self._download_and_run_installer(u)))
            self.root.wait_window(dlg.top)
        except Exception:
            pass

    def _download_and_run_installer(self, url):
        """后台下载新的安装包到本地，完成后启动它（自动升级，无需手动换安装包）。"""
        import urllib.request

        def work():
            dest = os.path.join(_base_dir(), "P2PChat-Setup-new.exe")
            self.root.after(0, lambda: self._set_status("正在下载新版本安装包…", "mute"))
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "P2PChat-App", "Accept": "application/octet-stream"})
                with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
                    total = 0
                    try:
                        total = int(r.headers.get("Content-Length") or 0)
                    except Exception:
                        total = 0
                    done = 0
                    last_pct = -1
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = int(done * 100 / total)
                            if pct != last_pct:
                                last_pct = pct
                                self.root.after(0, lambda p=pct: self._set_status(
                                    f"正在下载新版本安装包… {p}%", "accent"))
                self.root.after(0, lambda: self._run_installer(dest))
            except Exception as e:
                self.root.after(0, lambda e=e: self._set_status(
                    "下载失败：" + str(e), "err"))

        threading.Thread(target=work, daemon=True).start()

    def _run_installer(self, path):
        try:
            self._set_status("下载完成，即将启动安装程序…", "ok")
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess = __import__("subprocess")
                subprocess.Popen(["xdg-open", path])
        except Exception:
            self._set_status("已下载到：" + path + "（请手动运行）", "err")

    def _manual_check_update(self):
        """手动检查更新：打开版本管理，可查看所有版本并下载（含回退）。"""
        def work():
            try:
                rels = self._fetch_releases()
                self.root.after(0, lambda: self._show_version_manager(rels))
            except Exception:
                self.root.after(0, lambda: self._set_status(
                    "检查更新失败，请稍后重试", "err"))

        self._set_status("正在获取版本列表…", "mute")
        threading.Thread(target=work, daemon=True).start()

    # --------------------------- 会话管理 ---------------------------

    def _group_key(self, room):
        return self.GROUP_PREFIX + room

    def _dm_key(self, cid):
        return self.DM_PREFIX + cid

    def _ensure_group_session(self, room):
        key = self._group_key(room)
        s = self._sessions.get(key)
        if s is None:
            s = {"key": key, "kind": "group", "name": room, "room": room,
                 "unread": 0, "messages": _load_group_history(room, self.FEED_MAX)}
            self._sessions[key] = s
        return s

    def _ensure_dm_session(self, cid, name):
        key = self._dm_key(cid)
        s = self._sessions.get(key)
        if s is None:
            stored_name, msgs = _load_dm_history(cid, self.FEED_MAX)
            s = {"key": key, "kind": "dm", "cid": cid, "name": name or stored_name or "？",
                 "online": False, "unread": 0, "messages": msgs}
            self._sessions[key] = s
        if name:
            s["name"] = name
        return s

    def _switch_to(self, key):
        if key is None or key not in self._sessions:
            return
        self._current = key
        self._history_expanded = False
        if self._search_query:
            self._search_query = ""
            try:
                self.search_frame.pack_forget()
                self.search_entry.delete(0, "end")
            except Exception:
                pass
        s = self._sessions[key]
        s["unread"] = 0
        self._update_chat_title()
        self._reset_input_hint()
        self._render_feed()
        self._apply_session_list()
        self._update_window_title()

    def _start_dm(self, cid, name):
        s = self._ensure_dm_session(cid, name)
        s["online"] = True
        self._switch_to(s["key"])

    def _update_chat_title(self):
        s = self._sessions.get(self._current)
        if s is None:
            self.chat_title.configure(text="未选择会话")
            return
        if s["kind"] == "group":
            n = sum(1 for p in self._peers.values() if s["room"] in (p.get("rooms") or []))
            self.chat_title.configure(text=f"群聊 · {s['name']}（{n}人在线）")
        else:
            self.chat_title.configure(text=f"私聊 · {s['name']}")

    # --------------------------- 左侧会话/成员列表 ---------------------------

    def _apply_session_list(self):
        for w in self.session_frame.winfo_children():
            w.destroy()
        kw = (self.search_var.get() or "").strip().lower()
        dm_cids = {s["cid"] for s in self._sessions.values() if s["kind"] == "dm" and s["cid"]}

        groups = [r for r in self._rooms if (not kw) or kw in r.lower()]
        dms = sorted([s for s in self._sessions.values()
                      if s["kind"] == "dm" and ((not kw) or kw in s["name"].lower())],
                     key=lambda s: (-(s.get("unread") or 0), 0 if s["online"] else 1, s["name"]))
        online_others = [(cid, p["name"]) for cid, p in self._peers.items()
                         if cid != self.cid and cid not in dm_cids
                         and ((not kw) or kw in p["name"].lower())]

        total = len(groups) + len(dms) + len(online_others)
        if groups:
            self._add_section_header("群聊")
            for r in groups:
                self._add_group_item(r)
        if dms:
            self._add_section_header("私聊")
            for s in dms:
                self._add_dm_item(s)
        if online_others:
            self._add_section_header("在线成员")
            for cid, name in sorted(online_others, key=lambda x: x[1]):
                self._add_member_item(cid, name)

        if total == 0 and not kw:
            ctk.CTkLabel(self.session_frame, text="（暂无会话，先在上方加入房间）",
                         text_color=C("text_mute"), font=(FONT, 10)).pack(anchor="w", padx=10, pady=8)
        elif total == 0:
            ctk.CTkLabel(self.session_frame, text="（无匹配）",
                         text_color=C("text_mute"), font=(FONT, 10)).pack(anchor="w", padx=10, pady=8)

    def _add_section_header(self, text):
        ctk.CTkLabel(self.session_frame, text=text, text_color=C("section"),
                     font=(FONT, 10, "bold"), anchor="w").pack(fill="x", padx=6, pady=(10, 2))

    def _unread_badge(self, parent, n):
        txt = str(n) if n < 100 else "99+"
        w = max(20, 16 + (len(txt) - 1) * 8)
        return ctk.CTkLabel(parent, text=txt, width=w, height=20, corner_radius=10,
                            fg_color=C("danger"), text_color="#ffffff", font=(FONT, 10, "bold"))

    def _bind_row_hover(self, row, selected):
        def on_enter(_e):
            if not selected:
                row.configure(fg_color=C("hover"))
        def on_leave(_e):
            if not selected:
                row.configure(fg_color="transparent")
        row.bind("<Enter>", on_enter)
        row.bind("<Leave>", on_leave)

    def _session_preview(self, s):
        """返回会话最后一条消息的单行预览（QQ/Discord 风格）。"""
        msgs = s.get("messages") or []
        if not msgs:
            return ""
        m = msgs[-1]
        txt = str(m.get("text") or "").strip()
        if m.get("img_path"):
            txt = "🖼 [图片]"
        if not txt:
            return ""
        if m.get("mine"):
            txt = "我: " + txt
        txt = txt.replace("\n", " ").strip()
        return txt if len(txt) <= 22 else txt[:22] + "…"

    def _add_group_item(self, room):
        key = self._group_key(room)
        selected = key == self._current
        s = self._sessions.get(key)
        unread = (s.get("unread") or 0) if s else 0
        n = sum(1 for p in self._peers.values() if room in (p.get("rooms") or []))
        preview = self._session_preview(s) if s else ""
        row = ctk.CTkFrame(self.session_frame, corner_radius=8,
                           fg_color=(C("selected_bg") if selected else "transparent"))
        row.pack(fill="x", pady=1)
        hash_lbl = ctk.CTkLabel(row, text="#", width=18, anchor="n",
                                text_color=(C("accent") if selected else C("text_mute")),
                                font=(FONT, 13, "bold"), cursor="hand2")
        hash_lbl.pack(side="left", padx=(10, 0), pady=(6, 0))
        mid = ctk.CTkFrame(row, fg_color="transparent")
        mid.pack(side="left", fill="x", expand=True, padx=(2, 4), pady=4)
        ctk.CTkLabel(mid, text=room + (f"  {n}" if n else ""), anchor="w",
                     text_color=(C("selected_text") if selected else C("text")),
                     font=(FONT, 12, "bold" if selected else "normal"), cursor="hand2").pack(anchor="w")
        if preview:
            ctk.CTkLabel(mid, text=preview, anchor="w", text_color=C("text_mute"),
                         font=(FONT, 10), cursor="hand2").pack(anchor="w")
        if unread:
            self._unread_badge(row, unread).pack(side="right", padx=(0, 6))
        cross = ctk.CTkLabel(row, text="✕", width=24, text_color=C("text_mute"), cursor="hand2")
        cross.pack(side="right", padx=(0, 6))
        for w in [row, hash_lbl] + list(mid.winfo_children()):
            w.bind("<Button-1>", lambda e, k=key: self._switch_to(k))
        cross.bind("<Button-1>", lambda e, r=room: self._remove_room(r))
        self._bind_row_hover(row, selected)

    def _add_dm_item(self, s):
        key = s["key"]
        selected = key == self._current
        unread = s.get("unread") or 0
        preview = self._session_preview(s)
        row = ctk.CTkFrame(self.session_frame, corner_radius=8,
                           fg_color=(C("selected_bg") if selected else "transparent"))
        row.pack(fill="x", pady=1)
        dot = ctk.CTkLabel(row, text="●" if s["online"] else "○", width=18, anchor="n",
                           text_color=(C("online") if s["online"] else C("text_mute")),
                           font=(FONT, 11, "bold"), cursor="hand2")
        dot.pack(side="left", padx=(10, 0), pady=(6, 0))
        mid = ctk.CTkFrame(row, fg_color="transparent")
        mid.pack(side="left", fill="x", expand=True, padx=(2, 4), pady=4)
        ctk.CTkLabel(mid, text=s["name"], anchor="w",
                     text_color=(C("selected_text") if selected else C("text")),
                     font=(FONT, 12, "bold" if (selected or unread) else "normal"),
                     cursor="hand2").pack(anchor="w")
        if preview:
            ctk.CTkLabel(mid, text=preview, anchor="w", text_color=C("text_mute"),
                         font=(FONT, 10), cursor="hand2").pack(anchor="w")
        if unread:
            self._unread_badge(row, unread).pack(side="right", padx=(0, 10))
        for w in [row, dot] + list(mid.winfo_children()):
            w.bind("<Button-1>", lambda e, k=key: self._switch_to(k))
            w.bind("<Button-3>", lambda e, s=s: self._dm_context_menu(e, s))
        self._bind_row_hover(row, selected)

    def _add_member_item(self, cid, name):
        row = ctk.CTkFrame(self.session_frame, corner_radius=8, fg_color="transparent")
        row.pack(fill="x", pady=1)
        dot = ctk.CTkLabel(row, text="●", width=18, anchor="w", text_color=C("online"),
                           font=(FONT, 11, "bold"), cursor="hand2")
        dot.pack(side="left", padx=(10, 0), pady=7)
        lbl = ctk.CTkLabel(row, text=name, anchor="w", text_color=C("text"),
                           font=(FONT, 12), cursor="hand2")
        lbl.pack(side="left", fill="x", expand=True, pady=7)
        for w in (row, lbl, dot):
            w.bind("<Button-1>", lambda e, c=cid, nm=name: self._start_dm(c, nm))
        self._bind_row_hover(row, False)

    # --------------------------- 消息追加 / 持久化 ---------------------------

    def _flash_window(self):
        """新消息到达时闪烁任务栏图标（仅 Windows，窗口获得焦点后自动停止）。"""
        if os.name != "nt":
            return
        try:
            import ctypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint),
                            ("hwnd", ctypes.c_void_p),
                            ("dwFlags", ctypes.c_uint),
                            ("uCount", ctypes.c_uint),
                            ("dwTimeout", ctypes.c_uint)]

            info = FLASHWINFO()
            info.cbSize = ctypes.sizeof(FLASHWINFO)
            info.hwnd = self.root.winfo_id()
            info.dwFlags = 0x00000003  # FLASHW_ALL（任务栏 + 标题栏）
            info.uCount = 3
            info.dwTimeout = 0
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _maybe_notify(self, s, name, text, mine, system):
        """后台/非当前会话收到新消息时弹 Windows 通知；前台正在看时静默。"""
        if mine or system:
            return
        if s["key"] == self._current and self._window_focused:
            return
        try:
            title = str(s.get("name") or "新消息")
            who = str(name or "对方")
            preview = (text or "").strip().replace("\n", " ")[:80] or "（图片/文件）"
            _notify_windows(f"{title} · {who}", preview)
        except Exception:
            pass

    def _append_message(self, key, name, text, mine, img_path=None, file_path=None, system=False, mid=None, preview_tid=None):
        s = self._sessions.get(key)
        if s is None:
            return
        msg = {"name": name, "text": text, "mine": mine, "ts": time.time()}
        if mid:
            msg["mid"] = str(mid)
        if preview_tid:
            msg["preview_tid"] = str(preview_tid)
        if system:
            msg["system"] = True
        if img_path:
            msg["img_path"] = img_path
        if file_path:
            msg["file_path"] = file_path
        s["messages"].append(msg)
        if len(s["messages"]) > self.FEED_MAX:
            s["messages"] = s["messages"][-self.FEED_MAX:]
        self._save_session(s)
        self._maybe_notify(s, name, text, mine, system)
        if not mine and self.notify_sound and not system:
            _play_notify_sound()
        if key == self._current:
            self._stick_bottom = self._at_bottom()
            # 正在看的会话里来了对方的消息：立即发已读回执（不用等下次重渲染）
            if not mine and not system and mid and mid not in self._read_acked:
                self._read_acked.add(mid)
                if self.backend and self.backend.online:
                    is_dm = s.get("kind") == "dm"
                    target = s.get("cid") if is_dm else s.get("room")
                    self.backend.send_read(target, mid, is_dm)
            if system:
                self._render_system_line(text)
                self._maybe_scroll_bottom()
                self._trim_feed()
            else:
                show_head = self._should_show_head(s["messages"], len(s["messages"]) - 1)
                if img_path and os.path.isfile(img_path):
                    self._add_image_bubble(name, img_path, mine, msg.get("ts"), show_head)
                else:
                    self._add_bubble(name, text, mine, msg.get("ts"), show_head,
                                     file_path=file_path, mid=mid)
        else:
            s["unread"] = s.get("unread", 0) + 1
            self._schedule_session_list()
            self._update_window_title()
            if not mine:
                self._flash_window()

    def _save_session(self, s):
        if s["kind"] == "group":
            _save_group_history(s["room"], s["messages"])
        else:
            _save_dm_history(s["cid"], s["name"], s["messages"])

    # --------------------------- 输入框提示 ---------------------------

    def _reset_input_hint(self):
        self.input_box.delete("1.0", "end")
        self.input_box.insert("1.0", HINT)
        self.input_box.configure(text_color=C("text_mute"))
        self._hint_active = True

    def _on_input_focus_in(self, event):
        if self._hint_active:
            self.input_box.delete("1.0", "end")
            self.input_box.configure(text_color=C("text"))
            self._hint_active = False

    def _on_input_focus_out(self, event):
        if not self.input_box.get("1.0", "end").strip():
            self._reset_input_hint()

    # --------------------------- 房间 / 连接 ---------------------------

    def _add_room_from_input(self):
        room = self.room_var.get().strip() or "默认房间"
        self._add_room(room)

    def _refresh_room_combo(self):
        # 下拉历史：已加入的房间 + 历史文件里的房间
        items = list(self._rooms)
        for r in (_load_rooms() or []):
            if r and r not in items:
                items.append(r)
        try:
            self.room_combo.configure(values=(items or [""]))
        except Exception:
            pass

    def _add_room(self, room):
        room = (room or "").strip()
        if not room:
            return
        if room in self._rooms:
            self._switch_to(self._group_key(room))
            self.room_var.set("")
            return
        self._rooms.append(room)
        _save_rooms(self._rooms)
        self._ensure_group_session(room)
        if self.backend and self.backend.running:
            self.backend.add_room(room)
        self._apply_session_list()
        self._switch_to(self._group_key(room))
        self._refresh_room_combo()
        self.room_var.set("")

    def _remove_room(self, room):
        if room not in self._rooms:
            return
        self._rooms.remove(room)
        _save_rooms(self._rooms)
        if self.backend and self.backend.running:
            self.backend.remove_room(room)
        key = self._group_key(room)
        self._sessions.pop(key, None)
        if self._current == key:
            if self._rooms:
                self._switch_to(self._group_key(self._rooms[0]))
            else:
                self._current = None
                self._update_chat_title()
                self._render_feed()
        self._apply_session_list()

    def _clear_current_history(self):
        s = self._sessions.get(self._current)
        if s is None:
            messagebox.showinfo("提示", "当前没有选中的会话。")
            return
        if not messagebox.askyesno("清空记录",
                                   "确定清空当前会话的全部聊天记录吗？此操作不可撤销。"):
            return
        s["messages"] = []
        s["unread"] = 0
        if s["kind"] == "group":
            _delete_group_history(s["room"])
        else:
            _delete_dm_history(s["cid"])
        self._render_feed()
        self._apply_session_list()
        self._set_status("已清空当前会话记录", "ok")

    def _open_search(self, event=None):
        """打开会话内消息搜索栏并聚焦。"""
        try:
            self.search_frame.pack(fill="x", padx=8, pady=(0, 2))
            self.search_entry.delete(0, "end")
            self._search_query = ""
            self.search_entry.focus_set()
        except Exception:
            pass
        return "break"

    def _close_search(self, event=None):
        """关闭搜索并恢复完整消息列表。"""
        self._search_query = ""
        try:
            self.search_frame.pack_forget()
            self.search_entry.delete(0, "end")
        except Exception:
            pass
        self._render_feed()
        try:
            self.input_box.focus_set()
        except Exception:
            pass
        return "break"

    def _on_search_change(self, event=None):
        """搜索输入变化（防抖 200ms）。"""
        if self._msg_search_after is not None:
            try:
                self.root.after_cancel(self._msg_search_after)
            except Exception:
                pass
        self._msg_search_after = self.root.after(200, self._apply_search)

    def _apply_search(self):
        self._msg_search_after = None
        try:
            q = self.search_entry.get().strip()
        except Exception:
            q = ""
        if q == self._search_query:
            return
        self._search_query = q
        self._render_feed()

    def _clear_all_history(self):
        if not self._sessions:
            messagebox.showinfo("提示", "当前没有任何会话记录。")
            return
        if not messagebox.askyesno("清空所有记录",
                                   "确定清空全部会话的聊天记录吗？此操作不可撤销。"):
            return
        for s in self._sessions.values():
            s["messages"] = []
            s["unread"] = 0
            if s["kind"] == "group":
                _delete_group_history(s["room"])
            else:
                _delete_dm_history(s["cid"])
        self._render_feed()
        self._apply_session_list()
        self._set_status("已清空所有会话记录", "ok")

    def _delete_dm_session(self, key):
        s = self._sessions.get(key)
        if s is None or s["kind"] != "dm":
            return
        if not messagebox.askyesno("删除会话",
                                   f"确定删除与「{s['name']}」的私聊会话及全部聊天记录吗？"):
            return
        _delete_dm_history(s["cid"])
        self._sessions.pop(key, None)
        if self._current == key:
            if self._rooms:
                self._switch_to(self._group_key(self._rooms[0]))
            else:
                self._current = None
                self._update_chat_title()
                self._render_feed()
        self._apply_session_list()

    def _dm_context_menu(self, event, s):
        try:
            menu = tk.Menu(self.root, tearoff=0)
            menu.add_command(label="删除会话", command=lambda: self._delete_dm_session(s["key"]))
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _on_nick_changed(self):
        name = self.nick_var.get().strip() or "未命名"
        self._profile_name = name
        _save_profile(name, self._avatar)
        if not self._avatar:
            self._render_top_avatar()
        if self.backend and self.backend.running:
            self.backend.change_nick(name)

    def _toggle_connect(self):
        if self.backend and self.backend.running:
            self.backend.stop()
            self.backend = None
            self._peers = {}
            for s in self._sessions.values():
                if s["kind"] == "dm":
                    s["online"] = False
            self.connect_btn.configure(text="连接", fg_color=C("accent"),
                                       hover_color=C("accent_hover"))
            self._set_status("未连接", "mute")
            self._apply_session_list()
            self._update_chat_title()
            return

        name = self.nick_var.get().strip() or "未命名"
        # 兜底：没有房间时把当前输入框里的房间加进去
        if not self._rooms:
            self._add_room(self.room_var.get().strip() or "默认房间")
        # 连接前重新加载每个房间的历史（保证断开重连历史不丢）
        for room in self._rooms:
            s = self._ensure_group_session(room)
            s["messages"] = _load_group_history(room, self.FEED_MAX)

        self.backend = MqttBackend(
            name, self.cid, DEFAULT_BROKER, DEFAULT_PORT,
            on_text=self._cb_text,
            on_peers=self._cb_peers,
            on_file=self._cb_file,
            on_status=self._cb_status,
            on_dm=self._cb_dm,
            on_read=self._cb_read,
            on_recall=self._cb_recall,
            on_typing=self._cb_typing,
            on_delivered=self._cb_delivered,
            passphrase=self.encrypt_pass,
        )
        for room in self._rooms:
            self.backend.add_room(room)
        self.backend.start()
        self.connect_btn.configure(text="断开", fg_color=C("danger"),
                                   hover_color=C("err"))
        self._set_status("正在连接…", "mute")
        if self._current is None:
            self._switch_to(self._group_key(self._rooms[0]))
        else:
            self._render_feed()
        self._apply_session_list()

    # --------------------------- 发送 ---------------------------

    def _send_text(self):
        if self._hint_active:
            return
        text = self.input_box.get("1.0", "end").strip()
        if not text:
            return
        if not (self.backend and self.backend.online):
            self._show_system("尚未连接，无法发送。")
            return
        self.input_box.delete("1.0", "end")
        s = self._sessions.get(self._current)
        if s is None:
            return
        my = self.nick_var.get().strip() or "未命名"
        if s["kind"] == "dm":
            if self.backend.send_dm(s["cid"], text):
                self._append_message(s["key"], my, text, True)
            else:
                self.input_box.insert("1.0", text)
                self._show_system("发送失败，请检查连接。")
        else:
            if not self.backend.send_text(s["room"], text):
                self.input_box.insert("1.0", text)
                self._show_system("发送失败，请检查连接。")

    def _grab_clipboard_image(self):
        """读取剪贴板中的图片（无图片则返回 None）。"""
        try:
            if not _HAS_PIL:
                return None
            from PIL import Image, ImageGrab
            img = ImageGrab.grabclipboard()
            return img if isinstance(img, Image.Image) else None
        except Exception:
            return None

    def _on_paste(self, event):
        """Ctrl+V：若剪贴板是图片则作为图片发送，否则走默认文本粘贴。"""
        img = self._grab_clipboard_image()
        if img is None:
            return None
        if not (self.backend and self.backend.online):
            self._show_system("尚未连接，无法发送。")
            return "break"
        try:
            _ensure_data_dir()
            path = os.path.join(DATA_DIR, "paste_" + uuid.uuid4().hex[:10] + ".png")
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img.save(path, "PNG")
            self._do_send_file(path)
        except Exception:
            pass
        return "break"

    def _mention_names(self):
        """可 @ 的成员：当前房间在线成员 + 在线名单（去重）。"""
        names = []
        seen = set()
        s = self._sessions.get(self._current)
        room = s.get("room") if s else None
        for p in self._peers.values():
            n = str(p.get("name", "")).strip()
            if not n or n in seen:
                continue
            if room is None or room in (p.get("rooms") or []):
                names.append(n)
                seen.add(n)
        for p in self._peers.values():
            n = str(p.get("name", "")).strip()
            if n and n not in seen:
                names.append(n)
                seen.add(n)
        return names

    def _on_input_key(self, event):
        """检测 @ 输入并弹出成员提及面板；同时广播“正在输入”。"""
        if event.keysym in ("Up", "Down", "Return", "Escape", "Left", "Right", "BackSpace"):
            return
        self._send_typing()
        try:
            text = self.input_box.get("1.0", "insert")
            at = text.rfind("@")
            if at < 0:
                self._close_mention_panel()
                return
            partial = text[at + 1:]
            if " " in partial or "\n" in partial:
                self._close_mention_panel()
                return
            self._open_mention_panel(partial)
        except Exception:
            pass

    def _open_mention_panel(self, partial):
        names = self._mention_names()
        matches = [n for n in names if partial.lower() in n.lower()]
        if not matches:
            self._close_mention_panel()
            return
        if getattr(self, "_mention_win", None) is None or not self._mention_win.winfo_exists():
            self._mention_win = ctk.CTkToplevel(self.root)
            self._mention_win.overrideredirect(True)
            self._mention_win.configure(fg_color=C("panel"))
            self._mention_win.attributes("-topmost", True)
            self._mention_frame = ctk.CTkFrame(self._mention_win, fg_color="transparent")
            self._mention_frame.pack(padx=4, pady=4)
        for w in self._mention_frame.winfo_children():
            w.destroy()
        for n in matches[:8]:
            ctk.CTkButton(self._mention_frame, text="@" + n, height=26, corner_radius=6,
                          fg_color="transparent", hover_color=C("hover"), text_color=C("text"),
                          font=(FONT, 11), anchor="w",
                          command=lambda nm=n: self._insert_mention(nm)).pack(fill="x", pady=1)
        self._mention_win.update_idletasks()
        w = self._mention_win.winfo_reqwidth()
        h = self._mention_win.winfo_reqheight()
        x = self.input_box.winfo_rootx()
        y = self.input_box.winfo_rooty() - h - 6
        self._mention_win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _insert_mention(self, name):
        try:
            text = self.input_box.get("1.0", "insert")
            at = text.rfind("@")
            if at >= 0:
                self.input_box.delete(f"1.0 + {at} chars", "insert")
                self.input_box.insert("insert", "@" + name + " ")
        except Exception:
            pass
        self._close_mention_panel()
        self.input_box.focus_set()

    def _close_mention_panel(self):
        if getattr(self, "_mention_win", None) is not None:
            try:
                self._mention_win.destroy()
            except Exception:
                pass
            self._mention_win = None

    def _on_enter(self, event):
        if event.state & 0x0001:     # Shift+回车 = 换行
            return None
        self._send_text()
        return "break"

    def _pick_file(self):
        paths = filedialog.askopenfilenames(title="选择要发送的文件或图片（可多选）")
        for path in paths:
            if path and os.path.isfile(path):
                self._do_send_file(path)

    def _toggle_emoji_panel(self):
        if getattr(self, "_emoji_win", None) is not None:
            self._close_emoji_panel()
            return
        try:
            win = ctk.CTkToplevel(self.root)
            self._emoji_win = win
            win.overrideredirect(True)
            win.configure(fg_color=C("panel"))
            win.attributes("-topmost", True)
            cols = 10
            grid = ctk.CTkFrame(win, fg_color="transparent")
            grid.pack(padx=6, pady=6)
            for i, em in enumerate(EMOJIS):
                ctk.CTkButton(grid, text=em, width=34, height=34, corner_radius=8,
                              fg_color="transparent", hover_color=C("hover"),
                              text_color=C("text"), font=("Segoe UI Emoji", 15),
                              command=lambda e=em: self._insert_emoji(e)
                              ).grid(row=i // cols, column=i % cols, padx=1, pady=1)
            win.bind("<Escape>", lambda e: self._close_emoji_panel())
            win.update_idletasks()
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
            x = self.root.winfo_rootx() + self.root.winfo_width() - w - 24
            y = self.root.winfo_rooty() + self.root.winfo_height() - h - 130
            win.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            self._emoji_win = None

    def _close_emoji_panel(self):
        if getattr(self, "_emoji_win", None) is not None:
            try:
                self._emoji_win.destroy()
            except Exception:
                pass
            self._emoji_win = None

    def _insert_emoji(self, em):
        try:
            if self._hint_active:
                self.input_box.delete("1.0", "end")
                self.input_box.configure(text_color=C("text"))
                self._hint_active = False
            self.input_box.insert("insert", em)
            self.input_box.focus_set()
        except Exception:
            pass
        self._close_emoji_panel()

    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            return
        for p in paths:
            p = p.strip()
            if p and os.path.isfile(p):
                self._do_send_file(p)
        return "break"

    def _do_send_file(self, path):
        if not (self.backend and self.backend.online):
            self._show_system("尚未连接，无法发送。")
            return
        s = self._sessions.get(self._current)
        if s is None:
            return
        if s["kind"] == "group":
            self.backend.send_file(s["room"], path)
        else:
            self.backend.send_file_dm(s["cid"], path)

    # --------------------------- 回调（切回主线程） ---------------------------

    def _cb_text(self, room, name, text, mine, mid=None):
        self.root.after(0, lambda: self._append_message(self._group_key(room), name, text, mine, mid=mid))

    def _cb_peers(self, peers):
        self.root.after(0, lambda: self._refresh_peers(peers))

    def _cb_file(self, room, event, info):
        self.root.after(0, lambda: self._show_file_event(room, event, info))

    def _cb_status(self, online, msg):
        self.root.after(0, lambda: self._set_status(msg, "ok" if online else "err"))

    def _cb_dm(self, from_cid, name, text, mid=None):
        self.root.after(0, lambda: self._receive_dm(from_cid, name, text, mid))

    def _cb_read(self, room, mid, cid, name):
        self.root.after(0, lambda: self._receive_read(room, mid, cid, name))

    def _cb_recall(self, room, mid, who):
        self.root.after(0, lambda: self._receive_recall(room, mid, who))

    def _cb_typing(self, room, name, cid):
        self.root.after(0, lambda: self._receive_typing(room, name, cid))

    def _cb_delivered(self, room, mid, cid, name):
        self.root.after(0, lambda: self._receive_delivered(room, mid, cid, name))

    def _receive_typing(self, room, name, cid):
        """收到“正在输入”状态：当前会话标题临时显示，2.5 秒后恢复。"""
        if cid == self.cid:
            return
        if str(room).startswith("@"):
            key = self._dm_key(str(room)[1:])
        else:
            key = self._group_key(room)
        if key != self._current:
            return
        try:
            s = self._sessions.get(key)
            base = s["name"] if s else "聊天"
            prefix = "私聊 · " if (s and s["kind"] == "dm") else "群聊 · "
            self.chat_title.configure(text=f"{prefix}{base} · {name} 正在输入…")
            if self._typing_after is not None:
                try:
                    self.root.after_cancel(self._typing_after)
                except Exception:
                    pass
            self._typing_after = self.root.after(2500, self._clear_typing)
        except Exception:
            pass

    def _clear_typing(self):
        self._typing_after = None
        try:
            self._update_chat_title()
        except Exception:
            pass

    def _send_typing(self):
        """用户输入时广播“正在输入”（每 2 秒最多一次）。"""
        if self._hint_active:
            return
        if not (self.backend and self.backend.online):
            return
        now = time.time()
        if now - self._typing_last < 2.0:
            return
        s = self._sessions.get(self._current)
        if s is None:
            return
        self._typing_last = now
        is_dm = s.get("kind") == "dm"
        target = s.get("cid") if is_dm else s.get("room")
        try:
            self.backend.send_typing(target, is_dm)
        except Exception:
            pass

    def _receive_dm(self, from_cid, name, text, mid=None):
        s = self._ensure_dm_session(from_cid, name)
        s["online"] = True
        self._append_message(s["key"], name, text, False, mid=mid)
        self._schedule_session_list()

    def _receive_delivered(self, room, mid, cid, name):
        """收到“已送达”回执：给对应消息标记谁已送达（未读前显示已送达）。"""
        if not mid:
            return
        if str(room).startswith("@"):
            key = self._dm_key(str(room)[1:])
        else:
            key = self._group_key(room)
        s = self._sessions.get(key)
        if s is None:
            return
        for m in s["messages"]:
            if m.get("mid") == mid and m.get("mine"):
                names = m.setdefault("delivered_by", [])
                if name and name not in names:
                    names.append(name)
                self._save_session(s)
                if key == self._current:
                    self._render_feed()
                return

    def _receive_read(self, room, mid, cid, name):
        """收到已读回执：给对应消息标记谁已读。"""
        if not mid:
            return
        if str(room).startswith("@"):
            key = self._dm_key(str(room)[1:])
        else:
            key = self._group_key(room)
        s = self._sessions.get(key)
        if s is None:
            return
        for m in s["messages"]:
            if m.get("mid") == mid and m.get("mine"):
                names = m.setdefault("read_by", [])
                if name and name not in names:
                    names.append(name)
                self._save_session(s)
                if key == self._current:
                    self._render_feed()
                return

    def _receive_recall(self, room, mid, who):
        """收到撤回指令：把对应消息标记为已撤回。"""
        if not mid:
            return
        if str(room).startswith("@"):
            key = self._dm_key(str(room)[1:])
        else:
            key = self._group_key(room)
        s = self._sessions.get(key)
        if s is None:
            return
        for m in s["messages"]:
            if m.get("mid") == mid:
                if m.get("recalled"):
                    return
                m["recalled"] = True
                m["recalled_by"] = who or "对方"
                self._save_session(s)
                if key == self._current:
                    self._render_feed()
                return

    def _do_recall(self, mid):
        """右键撤回自己的消息。"""
        if not (self.backend and self.backend.online):
            self._set_status("未连接，无法撤回", "err")
            return
        s = self._sessions.get(self._current)
        if s is None:
            return
        is_dm = s.get("kind") == "dm"
        target = s.get("cid") if is_dm else s.get("room")
        if self.backend.send_recall(target, mid, is_dm):
            self._set_status("已撤回", "ok")
            for m in s.get("messages", []):
                if m.get("mid") == mid:
                    m["recalled"] = True
                    m["recalled_by"] = "我"
                    self._save_session(s)
                    break
            self._render_feed()
        else:
            self._set_status("撤回失败", "err")

    def _ack_reads(self, s):
        """对会话里已显示的他人消息发送已读回执（每条只发一次）。"""
        if not (self.backend and self.backend.online):
            return
        is_dm = s.get("kind") == "dm"
        target = s.get("cid") if is_dm else s.get("room")
        for m in s.get("messages", []):
            mid = m.get("mid")
            if not mid or m.get("mine") or mid in self._read_acked:
                continue
            self._read_acked.add(mid)
            self.backend.send_read(target, mid, is_dm)

    def _refresh_peers(self, peers):
        self._peers = peers or {}
        for s in self._sessions.values():
            if s["kind"] == "dm":
                s["online"] = s["cid"] in self._peers
        self._schedule_session_list()
        self._update_chat_title()
        if self.backend and self.backend.online:
            total = len(self._peers)
            self._set_status(f"已连接 · {len(self._rooms)} 个房间 · 共 {total} 人在线", "ok")

    # --------------------------- 界面更新 ---------------------------

    def _set_status(self, msg, color="mute"):
        # color 支持语义键（mute/ok/err/accent）或直接传十六进制色值
        if isinstance(color, str) and color in THEMES.get(_APPEARANCE, THEMES["dark"]):
            color = C(color)
        self.status_var.set(msg)
        self.status_label.configure(text_color=color)
        self._update_window_title()

    def _update_window_title(self):
        # 窗口标题实时反映连接状态 + 未读消息数（无变化时不重复设置，避免频繁重绘）
        try:
            if self.backend and self.backend.online:
                base = f"P2P 聊天 · 已连接 · {len(self._peers)} 人在线"
            else:
                base = "P2P 聊天 · 未连接"
            unread = sum(s.get("unread", 0) for s in self._sessions.values())
            if unread:
                base = f"● {base}  [{unread} 条未读]"
            if base != self._last_title:
                self._last_title = base
                self.root.title(base)
        except Exception:
            pass

    def _scroll_bottom_now(self):
        try:
            canvas = self.feed._parent_canvas
            canvas.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _scroll_top(self):
        """滚到顶部（搜索结果显示用）。"""
        def _do():
            try:
                canvas = self.feed._parent_canvas
                canvas.update_idletasks()
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.yview_moveto(0.0)
            except Exception:
                pass
        try:
            self.root.after(1, _do)
        except Exception:
            pass

    def _scroll_bottom(self):
        """延迟到下一帧再滚动，确保新内容布局完成、scrollregion 已更新。"""
        try:
            self.root.after(1, self._scroll_bottom_now)
        except Exception:
            pass

    def _at_bottom(self):
        """当前是否贴底（用于新消息到达前的贴底判定）。"""
        try:
            canvas = self.feed._parent_canvas
            _top, bottom = canvas.yview()
            return float(bottom) >= 0.98
        except Exception:
            return True

    def _maybe_scroll_bottom(self):
        """新内容到达时，仅当用户此前已贴底才自动滚动，避免打断向上翻阅历史。
        必须在内容 append 前调用 _at_bottom() 记录 _stick_bottom，否则新内容已把
        视口顶出底部，会误判成“用户在上翻”。"""
        if self._suppress_auto_scroll:
            return
        def _do():
            try:
                canvas = self.feed._parent_canvas
                canvas.update_idletasks()
                canvas.configure(scrollregion=canvas.bbox("all"))
                if self._stick_bottom:
                    canvas.yview_moveto(1.0)
            except Exception:
                pass
        try:
            self.root.after(1, _do)
        except Exception:
            pass

    def _add_file_offer_card(self, key, room, info):
        # 在聊天区渲染一条需手动确认的文件请求卡片（不弹窗）
        if key != self._current:
            # 非当前会话：先标记提醒，切过去再点
            s = self._sessions.get(key)
            if s is not None:
                s["unread"] = s.get("unread", 0) + 1
                self._apply_session_list()
            return
        self._stick_bottom = self._at_bottom()
        tid = info.get("tid")
        fname = info.get("name", "file")
        size = info.get("size", 0)
        sname = info.get("sname", "对方")
        card = ctk.CTkFrame(self.feed, corner_radius=14, fg_color=C("warn_bg"))
        card.pack(fill="x", padx=12, pady=3)
        head = ctk.CTkLabel(card, text=f"📥 {sname} 想发送文件", text_color=C("warn_text"),
                            font=(FONT, 10), anchor="w")
        head.pack(fill="x", padx=12, pady=(8, 0))
        body = ctk.CTkLabel(card, text=f"{fname}（{fmt_size(size)}）",
                            text_color=C("warn_text"), font=(FONT, 12, "bold"),
                            anchor="w", justify="left", wraplength=430)
        body.pack(fill="x", padx=12, pady=(2, 6))
        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 10))
        state_lbl = ctk.CTkLabel(btns, text="", text_color=C("text_mute"), font=(FONT, 10))
        state_lbl.pack(side="left")

        def _accept():
            if self.backend:
                self.backend.accept_file(tid)
            accept_btn.configure(state="disabled", text="已选择接收")
            reject_btn.configure(state="disabled")
            state_lbl.configure(text="✅ 已接收，正在传输…")

        def _reject():
            if self.backend:
                self.backend.reject_file(tid)
            accept_btn.configure(state="disabled")
            reject_btn.configure(state="disabled", text="已拒绝")
            state_lbl.configure(text="已拒绝该文件")

        reject_btn = ctk.CTkButton(btns, text="拒绝", width=64, height=28, corner_radius=8,
                                   fg_color=C("input_bg"), text_color=C("text_2"),
                                   hover_color=C("input_hover"), font=(FONT, 11), command=_reject)
        reject_btn.pack(side="right", padx=(0, 6))
        accept_btn = ctk.CTkButton(btns, text="接收", width=64, height=28, corner_radius=8,
                                   font=(FONT, 11, "bold"), command=_accept)
        accept_btn.pack(side="right")
        self._pending_offers[tid] = card
        self._maybe_scroll_bottom()
        self._trim_feed()

    @staticmethod
    def _should_show_head(msgs, idx):
        """Discord 式消息分组：同一发送者 5 分钟内的连续消息，仅第一条显示名字行。"""
        if idx <= 0:
            return True
        prev = msgs[idx - 1]
        cur = msgs[idx]
        if prev.get("name") != cur.get("name") or bool(prev.get("mine")) != bool(cur.get("mine")):
            return True
        return (cur.get("ts") or 0) - (prev.get("ts") or 0) >= ChatApp.GROUP_GAP

    def _avatar_label(self, parent, name, mine, size=34):
        """聊天消息头像：自己的用真实头像（圆形，缓存避免每帧重复解码），对方用彩色首字母。"""
        img = None
        if mine and self._avatar:
            img = self._my_avatar_ctk
            if img is None:
                img = _circular_ctk_image(self._avatar, size)
                self._my_avatar_ctk = img
        if img is not None:
            self._images.append(img)
            return ctk.CTkLabel(parent, image=img, text="", width=size, height=size,
                                corner_radius=size // 2, fg_color="transparent")
        n = (name or "?").strip() or "?"
        return ctk.CTkLabel(parent, text=n[:1].upper(), width=size, height=size,
                            corner_radius=size // 2, fg_color=_name_color(n),
                            text_color="#ffffff", font=(FONT, 15, "bold"))

    def _message_row(self, name, mine, show_head):
        """创建带头像的消息行，返回气泡控件；头像仅在消息组首条显示，其余缩进对齐。"""
        AV, GAP = 34, 8
        row = ctk.CTkFrame(self.feed, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(6 if show_head else 1))
        if mine:
            if show_head:
                self._avatar_label(row, name, True, AV).pack(side="right")
            else:
                ctk.CTkFrame(row, width=AV + GAP, height=1, fg_color="transparent").pack(side="right")
            bubble = ctk.CTkFrame(row, corner_radius=14, fg_color=C("mine_bubble"))
            bubble.pack(side="right", padx=(0, GAP if show_head else 0))
        else:
            if show_head:
                self._avatar_label(row, name, False, AV).pack(side="left")
            else:
                ctk.CTkFrame(row, width=AV + GAP, height=1, fg_color="transparent").pack(side="left")
            bubble = ctk.CTkFrame(row, corner_radius=14, fg_color=C("other_bubble"))
            bubble.pack(side="left", padx=(GAP if show_head else 0, 0))
        return bubble

    def _add_bubble(self, name, text, mine, ts=None, show_head=True, file_path=None,
                     read_by=None, delivered_by=None, mid=None, recalled=False,
                     recalled_by=None):
        if recalled:
            label = "（已撤回）" if mine else f"（{recalled_by or '对方'} 撤回了一条消息）"
            self._render_system_line(label)
            return
        tstr = _fmt_time(ts) if ts else ""
        bubble = self._message_row(name, mine, show_head)
        if show_head:
            head = ctk.CTkFrame(bubble, fg_color="transparent")
            head.pack(fill="x", padx=12, pady=(6, 0))
            ctk.CTkLabel(head, text=name, text_color=C("text_mute"),
                         font=(FONT, 10)).pack(side="left")
            if tstr:
                ctk.CTkLabel(head, text=tstr, text_color=C("text_mute"),
                             font=(FONT, 9)).pack(side="right")
        body = ctk.CTkLabel(bubble, text=text, wraplength=460, justify="left",
                            text_color=(C("mine_text") if mine else C("other_text")),
                            font=(FONT, 12))
        body.pack(anchor="w", padx=12, pady=((2 if show_head else 6), 8))
        body.bind("<Button-3>", lambda e, t=text, p=file_path: self._message_menu(e, t, p, mine=mine, mid=mid))
        if mine and read_by:
            names = "、".join(read_by[:5])
            if len(read_by) > 5:
                names += f" 等 {len(read_by)} 人"
            ctk.CTkLabel(bubble, text=f"已读 {names}", text_color=C("accent"),
                         font=(FONT, 9)).pack(anchor="e", padx=12, pady=(0, 4))
        elif mine and delivered_by:
            names = "、".join(delivered_by[:5])
            if len(delivered_by) > 5:
                names += f" 等 {len(delivered_by)} 人"
            ctk.CTkLabel(bubble, text=f"已送达 {names}", text_color=C("text_mute"),
                         font=(FONT, 9)).pack(anchor="e", padx=12, pady=(0, 4))
        self._maybe_scroll_bottom()
        self._trim_feed()

    def _add_image_bubble(self, name, path, mine, ts=None, show_head=True):
        tstr = _fmt_time(ts) if ts else ""
        if not (_HAS_PIL and path and os.path.isfile(path)):
            self._add_bubble(name, "🖼 一张图片", mine, ts, show_head)
            return
        try:
            # 缩略图缓存：按 路径+mtime 复用已解码的 CTkImage，避免每次切会话都重新解码整张图
            cache_key = (path, int(os.path.getmtime(path)))
            ctk_img = self._thumb_cache.get(cache_key)
            if ctk_img is None:
                from PIL import Image  # 惰性加载
                Image.MAX_IMAGE_PIXELS = None  # 允许超大原图也能渲染缩略图
                img = Image.open(path)
                try:
                    img.draft("RGB", (560, 560))  # JPEG 先降采样，加速解码
                except Exception:
                    pass
                img.load()  # 强制解码，损坏图片这里会失败而非渲染时崩溃
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                img = img.copy()
                img.thumbnail((280, 280))
                ctk_img = CTkImage(light_image=img, dark_image=img, size=(img.width, img.height))
                self._thumb_cache[cache_key] = ctk_img
                if len(self._thumb_cache) > 256:  # 缓存上限，防内存无限增长
                    self._thumb_cache.clear()
            self._images.append(ctk_img)
            bubble = self._message_row(name, mine, show_head)
            if show_head:
                head = ctk.CTkFrame(bubble, fg_color="transparent")
                head.pack(fill="x", padx=12, pady=(6, 2))
                ctk.CTkLabel(head, text=f"{name} · 图片", text_color=C("text_mute"),
                             font=(FONT, 10)).pack(side="left")
                if tstr:
                    ctk.CTkLabel(head, text=tstr, text_color=C("text_mute"),
                                 font=(FONT, 9)).pack(side="right")
            _img = ctk.CTkLabel(bubble, image=ctk_img, text="", cursor="hand2")
            _img.pack(padx=6, pady=4)
            _img.bind("<Button-1>", lambda e, p=path: self._open_image(p))
            _img.bind("<Button-3>", lambda e, p=path: self._message_menu(e, p, p))
            self._maybe_scroll_bottom()
            self._trim_feed()
        except Exception:
            self._add_bubble(name, "🖼 一张图片（无法预览）", mine, ts)

    def _copy_to_clipboard(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(text))
            self._set_status("已复制到剪贴板", "ok")
        except Exception:
            pass

    def _open_file_location(self, path):
        """在系统文件管理器中打开文件所在位置并选中文件。"""
        try:
            path = os.path.abspath(path or "")
            if not path:
                return
            if os.name == "nt":
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                d = os.path.dirname(path)
                if os.path.isdir(d):
                    import subprocess
                    subprocess.Popen(["xdg-open", d])
        except Exception:
            pass

    def _message_menu(self, event, text, file_path=None, mine=False, mid=None):
        """消息右键菜单：复制 / 撤回 / （文件消息）打开位置。"""
        try:
            menu = tk.Menu(self.root, tearoff=0)
            menu.add_command(label="复制", command=lambda: self._copy_to_clipboard(text))
            if mine and mid:
                menu.add_command(label="撤回", command=lambda: self._do_recall(mid))
            if file_path:
                menu.add_command(label="打开文件位置", command=lambda: self._open_file_location(file_path))
                menu.add_command(label="复制路径", command=lambda: self._copy_to_clipboard(file_path))
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _render_system_line(self, text):
        ctk.CTkLabel(self.feed, text=text, text_color=C("text_mute"), wraplength=560,
                     justify="center", font=(FONT, 10)).pack(pady=6)

    def _show_system(self, text, target_key=None):
        target_key = target_key or self._current
        if target_key is None or target_key != self._current:
            return
        self._stick_bottom = self._at_bottom()
        self._render_system_line(text)
        self._maybe_scroll_bottom()
        self._trim_feed()

    def _trim_feed(self):
        try:
            kids = self.feed.winfo_children()
            if len(kids) > ChatApp.FEED_MAX:
                for w in kids[:len(kids) - ChatApp.FEED_MAX]:
                    w.destroy()
        except Exception:
            pass

    def _expand_history(self):
        self._history_expanded = True
        self._render_feed()

    def _render_feed(self):
        for w in self.feed.winfo_children():
            w.destroy()
        self._images = []
        s = self._sessions.get(self._current)
        if s is None:
            self._update_chat_title()
            ctk.CTkLabel(self.feed, text="请在左侧选择或加入一个会话。",
                         text_color=C("text_mute"), font=(FONT, 11)).pack(pady=20)
            return
        msgs = s["messages"]
        if self._search_query:
            q = self._search_query.lower()
            msgs = [m for m in msgs if
                    q in str(m.get("text", "")).lower()
                    or q in str(m.get("name", "")).lower()
                    or (m.get("file_path") and q in os.path.basename(str(m["file_path"])).lower())]
            ctk.CTkLabel(self.feed, text=f"搜索「{self._search_query}」· 找到 {len(msgs)} 条（Esc 退出）",
                         text_color=C("accent"), font=(FONT, 10, "bold")).pack(pady=6)
        elif len(msgs) > self.RENDER_MAX and not self._history_expanded:
            remaining = len(msgs) - self.RENDER_MAX
            lbl = ctk.CTkLabel(self.feed, text=f"… 更早的 {remaining} 条消息 · 点击展开",
                               text_color=C("accent"), font=(FONT, 10, "bold"),
                               cursor="hand2", justify="center")
            lbl.pack(pady=4)
            lbl.bind("<Button-1>", lambda e: self._expand_history())
            msgs = msgs[-self.RENDER_MAX:]
        last_day = None
        self._suppress_auto_scroll = True  # 全量渲染：逐条 auto-scroll 交给末尾一次 _scroll_bottom
        for idx, m in enumerate(msgs):
            ts = m.get("ts")
            dlabel = _day_label(ts) if ts else ""
            day_break = bool(dlabel and dlabel != last_day)
            if day_break:
                ctk.CTkLabel(self.feed, text=f"── {dlabel} ──", text_color=C("text_mute"),
                             font=(FONT, 10)).pack(pady=(8, 2))
                last_day = dlabel
            show_head = day_break or self._should_show_head(msgs, idx)
            if m.get("system"):
                self._render_system_line(m.get("text", ""))
            elif m.get("img_path") and os.path.isfile(m["img_path"]):
                self._add_image_bubble(m["name"], m["img_path"], m["mine"], ts, show_head)
            else:
                self._add_bubble(m["name"], m["text"], m["mine"], ts, show_head,
                                 file_path=m.get("file_path"), read_by=m.get("read_by"),
                                 delivered_by=m.get("delivered_by"), mid=m.get("mid"),
                                 recalled=m.get("recalled"), recalled_by=m.get("recalled_by"))
        self._suppress_auto_scroll = False
        if self._search_query:
            self._scroll_top()
        else:
            self._scroll_bottom()
        self._ack_reads(s)

    def _show_image_preview(self, key, room, info):
        """图片 offer 到达：立即显示低画质缩略图预览，并自动接收原图（QQ 式内联）。"""
        import base64
        try:
            _ensure_data_dir()
            tp = os.path.join(DATA_DIR, "thumb_" + str(info.get("tid", "x")) + ".jpg")
            with open(tp, "wb") as f:
                f.write(base64.b64decode(info["thumb"]))
            sname = info.get("sname", "对方")
            self._append_message(key, sname, f"🖼 图片：{info.get('name', '')}", False,
                                 img_path=tp, preview_tid=info.get("tid"))
        except Exception:
            self._add_file_offer_card(key, room, info)
            return
        if self.backend:
            self.backend.accept_file(info.get("tid"))

    def _replace_preview(self, key, tid, full_path):
        """把之前的缩略图预览替换为原图；找到并替换返回 True。"""
        if not tid:
            return False
        s = self._sessions.get(key)
        if s is None:
            return False
        for m in s["messages"]:
            if m.get("preview_tid") == tid:
                m["img_path"] = full_path
                m.pop("preview_tid", None)
                self._save_session(s)
                if key == self._current:
                    self._render_feed()
                return True
        return False

    def _show_file_event(self, room, event, info):
        if str(room).startswith("@"):
            cid = str(room)[1:]
            if event == "offer":
                self._ensure_dm_session(cid, info.get("sname", "匿名"))
            key = self._dm_key(cid)
        else:
            key = self._group_key(room)
        name = info.get("name", "?")
        mime = info.get("mime", "")
        size = info.get("size", 0)
        my = self.nick_var.get().strip() or "未命名"
        if event == "waiting":
            self._show_system(f"📤 已发送请求，等待对方接收：{name}（{fmt_size(size)}）", key)
        elif event == "accepted":
            self._show_system(f"✅ 对方已接受，开始发送：{name}", key)
        elif event == "accepting":
            self._show_system(f"📥 已同意接收，正在等待数据：{name}", key)
        elif event == "progress":
            self._set_status(f"传输中 {info.get('percent', 0)}% · {name}", "accent")
        elif event == "sent":
            if is_image(mime) and info.get("path"):
                self._append_message(key, my, f"🖼 图片：{name}", True, img_path=info["path"])
            else:
                self._append_message(key, my, f"📎 已发送文件：{name}（{fmt_size(size)}）", True,
                                     file_path=info.get("path", ""))
        elif event == "offer":
            if is_image(mime) and info.get("thumb"):
                self._show_image_preview(key, room, info)
            else:
                self._add_file_offer_card(key, room, info)
        elif event == "rejected":
            self._append_message(key, "", f"⚠️ 对方拒绝接收：{name}", False, system=True)
        elif event == "done":
            sname = info.get("sname", "对方")
            path = info.get("path", "")
            if is_image(mime):
                if not self._replace_preview(key, info.get("tid"), path):
                    self._append_message(key, sname, f"🖼 图片：{name}", False, img_path=path)
            else:
                self._append_message(key, sname, f"📎 已收到文件：{name}（{fmt_size(size)}）", False,
                                     file_path=path)
            self._show_system(f"✅ 已保存到：{path}", key)
        elif event == "error":
            self._append_message(key, "", f"⚠️ {name}：{info.get('msg', '失败')}", False, system=True)

    def _on_close(self):
        try:
            _update_settings("window_geometry", self.root.geometry())
        except Exception:
            pass
        if self.backend:
            try:
                self.backend.stop()
            except Exception:
                pass
        try:
            _save_rooms(self._rooms)
        except Exception:
            pass
        self.root.destroy()


class ImagePreview:
    """内置图片预览窗口：双击聊天里的图片弹出，等比放大显示，Esc / 关闭按钮退出。"""

    def __init__(self, master, path):
        self._ctk_img = None
        if not (_HAS_PIL and path and os.path.isfile(path)):
            messagebox.showinfo("无法预览", "图片文件不存在或无法读取。")
            return
        try:
            from PIL import Image  # 惰性加载
            Image.MAX_IMAGE_PIXELS = None  # 允许超大原图也能放大预览
            img = Image.open(path)
            try:
                img.draft("RGB", (1640, 1200))
            except Exception:
                pass
            img.load()  # 强制解码，损坏图片这里会失败而非渲染时崩溃
            if img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")
            img.thumbnail((820, 600))
            ctk_img = CTkImage(light_image=img, dark_image=img, size=(img.width, img.height))
        except Exception:
            messagebox.showinfo("无法预览", "图片文件无法读取。")
            return

        self._ctk_img = ctk_img  # 持有引用，防止被 GC
        top = ctk.CTkToplevel(master)
        self.top = top
        top.title("图片预览")
        top.configure(fg_color=C("app_bg"))
        top.resizable(False, False)

        ctk.CTkLabel(top, text=os.path.basename(path), text_color=C("text_2"),
                     font=(FONT, 11)).pack(padx=20, pady=(12, 4))
        ctk.CTkLabel(top, image=ctk_img, text="").pack(padx=24, pady=(0, 4))
        ctk.CTkButton(top, text="关闭", width=90, height=30, corner_radius=8,
                      fg_color=C("input_bg"), hover_color=C("input_hover"),
                      text_color=C("text_2"), font=(FONT, 12), command=top.destroy).pack(pady=(4, 14))

        # 用图片自身（逻辑）尺寸设置窗口，避免高分屏下 winfo 物理像素被误当逻辑像素导致窗口放大
        scale = 1.0
        try:
            scale = float(top.tk.call("tk", "scaling")) / 1.3333333
        except Exception:
            scale = 1.0
        if scale <= 0:
            scale = 1.0
        w = img.width + 48
        h = img.height + 104
        # 居中于父窗口，并带 Esc 关闭
        try:
            mw = master.winfo_width() / scale
            mh = master.winfo_height() / scale
            mx = master.winfo_rootx() / scale
            my = master.winfo_rooty() / scale
            x = int(mx + (mw - w) // 2)
            y = int(my + max(0, (mh - h) // 3))
        except Exception:
            x, y = 100, 100
        top.geometry(f"{int(w)}x{int(h)}+{x}+{y}")
        top.bind("<Escape>", lambda e: top.destroy())
        try:
            top.focus_set()
        except Exception:
            pass


class AboutDialog:
    """环境检测 / 关于 对话框：查看依赖状态 + 一键安装缺失组件。"""

    def __init__(self, master):
        self.report = collect_env_report()
        top = ctk.CTkToplevel(master)
        self.top = top
        top.title("环境检测 / 关于")
        top.geometry("540x500")
        top.resizable(False, False)
        top.configure(fg_color=C("app_bg"))
        top.transient(master)
        top.bind("<Escape>", lambda e: top.destroy())
        try:
            top.grab_set()
        except Exception:
            pass

        ctk.CTkLabel(top, text="关于 P2P 聊天 · 运行环境", font=(FONT, 15, "bold"),
                     text_color=C("text")).pack(pady=(18, 4))

        info = ctk.CTkFrame(top, fg_color=C("panel_2"), corner_radius=14)
        info.pack(fill="x", padx=18, pady=(6, 10))
        ctk.CTkLabel(
            info,
            text=(
                f"版本     {APP_VERSION}\n"
                f"Python   {self.report['python']}\n"
                f"系统     {self.report['platform']}\n"
                f"服务器   {self.report['broker']}（免费公共 MQTT）\n"
                f"数据目录 {DATA_DIR}\n"
                f"收件夹   {DOWNLOADS_DIR}"
            ),
            justify="left", anchor="w", font=(FONT, 12), text_color=C("text_2"),
            wraplength=470,
        ).pack(padx=16, pady=12, anchor="w")

        ctk.CTkLabel(top, text="依赖组件", font=(FONT, 13, "bold"), text_color=C("text"),
                     anchor="w").pack(fill="x", padx=22, pady=(2, 2))
        self.item_frame = ctk.CTkFrame(top, fg_color="transparent")
        self.item_frame.pack(fill="x", padx=18)
        self._render_items()

        self.log_box = ctk.CTkTextbox(top, height=96, corner_radius=10, fg_color=C("panel_2"),
                                      text_color=C("text_2"), font=("Consolas", 11), wrap="word")
        self.log_box.pack(fill="x", padx=18, pady=(8, 0))
        self.log_box.insert("1.0", "已检测运行环境。缺组件可点下方按钮一键安装。\n")

        btnrow = ctk.CTkFrame(top, fg_color="transparent")
        btnrow.pack(fill="x", padx=18, pady=(10, 16))
        self.install_btn = ctk.CTkButton(
            btnrow, text="一键安装缺失组件", height=34, corner_radius=12,
            font=(FONT, 12, "bold"), command=self._install)
        self.install_btn.pack(side="left")
        ctk.CTkButton(
            btnrow, text="关闭", width=80, height=34, corner_radius=12,
            fg_color=C("input_bg"), text_color=C("text_2"), hover_color=C("input_hover"),
            font=(FONT, 12), command=top.destroy).pack(side="right")

        if not self.report["missing"]:
            self.install_btn.configure(state="disabled", text="环境已完整")

    def _render_items(self):
        for w in self.item_frame.winfo_children():
            w.destroy()
        for name, desc, ok in self.report["items"]:
            row = ctk.CTkFrame(self.item_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            color = C("ok") if ok else C("err")
            ctk.CTkLabel(row, text="✓" if ok else "✗", width=24,
                         font=(FONT, 15, "bold"), text_color=color).pack(side="left")
            ctk.CTkLabel(row, text=name if ok else name + "（缺）", width=130, anchor="w",
                         font=(FONT, 12, "bold"), text_color=C("text")).pack(side="left")
            ctk.CTkLabel(row, text=desc, anchor="w", font=(FONT, 12),
                         text_color=C("text_mute")).pack(side="left")

    def _append_log(self, text):
        self.top.after(0, lambda t=text: self._do_append(t))

    def _do_append(self, text):
        self.log_box.insert("end", text)
        self.log_box.see("end")

    def _install(self):
        import subprocess
        import threading
        missing = self.report["missing"]
        if not missing:
            return
        self.install_btn.configure(state="disabled", text="安装中…")
        self.log_box.delete("1.0", "end")
        self._append_log(f"开始安装：{', '.join(missing)}\n\n")
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing]

        def run():
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace")
                for line in proc.stdout:
                    self._append_log(line)
                proc.wait()
                self._append_log(f"\n退出码 {proc.returncode}\n")
            except Exception as e:
                self._append_log(f"安装出错：{e}\n")
            self.top.after(0, self._after_install)

        threading.Thread(target=run, daemon=True).start()

    def _after_install(self):
        self.report = collect_env_report()
        self._render_items()
        if self.report["missing"]:
            self.install_btn.configure(state="normal", text="重试安装")
            self._append_log("仍有组件缺失，请查看上方日志。\n")
        else:
            self.install_btn.configure(state="disabled", text="环境已完整")
            self._append_log("\n✅ 组件已安装。部分功能需重启程序后生效。\n")


class VersionManagerDialog:
    """版本管理对话框：列出所有版本，支持查看累积更新内容、下载任意版本（含回退）。"""

    def __init__(self, master, versions, current, download_cb):
        self.versions = versions            # [{tag, body, dl, html}]
        self.current = str(current).lstrip("vV").strip()
        self.download_cb = download_cb
        self.versions.sort(key=lambda v: _ver_parts(v["tag"]), reverse=True)
        self._selected = self.versions[0]["tag"] if self.versions else ""

        top = ctk.CTkToplevel(master)
        self.top = top
        top.title("版本管理 / 更新")
        top.geometry("480x540")
        top.resizable(False, False)
        top.transient(master)
        top.configure(fg_color=C("app_bg"))
        top.bind("<Escape>", lambda e: top.destroy())
        try:
            top.grab_set()
        except Exception:
            pass

        ctk.CTkLabel(top, text="版本管理", font=(FONT, 15, "bold"),
                     text_color=C("text")).pack(pady=(14, 2))
        ctk.CTkLabel(top, text=f"当前版本：v{self.current}", font=(FONT, 11),
                     text_color=C("text_mute")).pack()

        # 版本列表（紧凑、支持鼠标滚轮滚动）
        self.list_frame = ctk.CTkScrollableFrame(top, width=320, height=150, fg_color="transparent")
        self.list_frame.pack(fill="x", padx=18, pady=(8, 4))
        self._btn_by_tag = {}
        self._build_list()

        self.body_box = ctk.CTkTextbox(top, corner_radius=10, fg_color=C("panel_2"),
                                       text_color=C("text_2"), font=(FONT, 11), wrap="word")
        self.body_box.pack(fill="both", expand=True, padx=18, pady=8)
        self._render_body()

        btnrow = ctk.CTkFrame(top, fg_color="transparent")
        btnrow.pack(fill="x", padx=18, pady=(0, 16))
        self.dl_btn = ctk.CTkButton(btnrow, text="下载并安装", height=36, corner_radius=10,
                                    font=(FONT, 12, "bold"), command=self._do_download)
        self.dl_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(btnrow, text="关闭", width=90, height=36, corner_radius=10,
                      fg_color=C("input_bg"), text_color=C("text_2"), hover_color=C("input_hover"),
                      font=(FONT, 12), command=top.destroy).pack(side="right")

    def _build_list(self):
        for v in self.versions:
            tag = v["tag"]
            label = "v" + tag + ("（当前）" if tag == self.current else "")
            sel = (tag == self._selected)
            btn = ctk.CTkButton(self.list_frame, text=label, height=28, corner_radius=6,
                                fg_color=(C("selected_bg") if sel else "transparent"),
                                hover_color=C("hover"), text_color=C("text"),
                                font=(FONT, 11, "bold" if sel else "normal"),
                                anchor="w", command=lambda t=tag: self._select(t))
            btn.pack(fill="x", pady=1)
            self._btn_by_tag[tag] = btn

    def _select(self, tag):
        self._selected = tag
        for t, btn in self._btn_by_tag.items():
            sel = (t == tag)
            btn.configure(fg_color=(C("selected_bg") if sel else "transparent"),
                          font=(FONT, 11, "bold" if sel else "normal"))
        self._render_body()

    def _sel_obj(self):
        for v in self.versions:
            if v["tag"] == self._selected:
                return v
        return None

    def _render_body(self):
        self.body_box.configure(state="normal")
        self.body_box.delete("1.0", "end")
        if not self.versions:
            self.body_box.insert("1.0", "没有可用版本。")
            self.body_box.configure(state="disabled")
            return
        sel = self._selected
        cur, target = _ver_parts(self.current), _ver_parts(sel)
        if target == cur:
            text = f"你当前已经是 v{sel}。\n\n可以在上方选择其它版本下载（升级或回退）。"
        elif target > cur:
            items = [v for v in self.versions if cur < _ver_parts(v["tag"]) <= target]
            items.sort(key=lambda v: _ver_parts(v["tag"]))
            lines = [f"从 v{self.current} 升级到 v{sel} 的累积更新内容：\n"]
            for v in items:
                body = (v["body"] or "").strip()
                lines.append(f"━━━ v{v['tag']} ━━━")
                lines.append(body if body else "（无详细说明）")
                lines.append("")
            text = "\n".join(lines)
        else:
            v = self._sel_obj() or {}
            body = (v.get("body") or "").strip()
            text = (f"⚠️ 你将回退到旧版本 v{sel}（比当前 v{self.current} 更早）。\n\n"
                    f"该版本更新内容：\n{body if body else '（无详细说明）'}")
        self.body_box.insert("1.0", text)
        self.body_box.configure(state="disabled")

    def _do_download(self):
        v = self._sel_obj()
        if not v:
            return
        if not v.get("dl"):
            import webbrowser
            webbrowser.open(v.get("html") or
                            f"https://github.com/{UPDATE_OWNER}/{UPDATE_REPO}/releases")
            return
        self.top.destroy()
        if self.download_cb:
            self.download_cb(v["dl"])

class UpdateDialog:
    """发现新版本时的提示框：展示版本号 / 更新内容 / 跳转下载。"""

    def __init__(self, master, latest, body, notes, download_url="", download_cb=None):
        top = ctk.CTkToplevel(master)
        self.top = top
        top.title("发现新版本")
        top.geometry("480x480")
        top.resizable(False, False)
        top.transient(master)
        top.configure(fg_color=C("app_bg"))
        top.bind("<Escape>", lambda e: top.destroy())
        try:
            top.grab_set()
        except Exception:
            pass

        badge = ctk.CTkFrame(top, corner_radius=20, fg_color=C("panel"))
        badge.pack(fill="x", padx=24, pady=(24, 12))
        ctk.CTkLabel(badge, text="🎉", font=(FONT, 36)).pack(pady=(22, 4))
        ctk.CTkLabel(badge, text="发现新版本", font=(FONT, 18, "bold"),
                     text_color=C("text")).pack()
        ctk.CTkLabel(
            badge,
            text=f"当前 {APP_VERSION}  →  最新 {latest}",
            font=(FONT, 12, "bold"), text_color=C("accent"),
        ).pack(pady=(4, 2))

        body_frame = ctk.CTkFrame(top, corner_radius=16, fg_color=C("panel"))
        body_frame.pack(fill="both", expand=True, padx=24, pady=(0, 10))
        ctk.CTkLabel(body_frame, text="更新内容", font=(FONT, 13, "bold"),
                     text_color=C("text"), anchor="w").pack(anchor="w", padx=16, pady=(14, 4))
        text = body.strip() if body else "（本次更新未附详细说明）"
        body_box = ctk.CTkTextbox(body_frame, corner_radius=10, fg_color=C("panel_2"),
                                  text_color=C("text_2"), font=(FONT, 12), wrap="word",
                                  border_width=0)
        body_box.pack(fill="both", expand=True, padx=16, pady=(0, 6))
        body_box.insert("1.0", text)
        body_box.configure(state="disabled")

        btnrow = ctk.CTkFrame(top, fg_color="transparent")
        btnrow.pack(fill="x", padx=24, pady=(0, 24))
        web_url = notes or f"https://github.com/{UPDATE_OWNER}/{UPDATE_REPO}/releases"

        def _do_download():
            self.top.destroy()
            if download_cb:
                download_cb()

        if download_cb:
            ctk.CTkButton(btnrow, text="下载并安装", height=40, corner_radius=12,
                          font=(FONT, 13, "bold"), command=_do_download
                          ).pack(side="left", fill="x", expand=True, padx=(0, 8))
            ctk.CTkButton(btnrow, text="前往网页", width=90, height=40, corner_radius=12,
                          fg_color=C("input_bg"), text_color=C("text_2"), hover_color=C("input_hover"),
                          font=(FONT, 12), command=lambda: self._open(web_url)
                          ).pack(side="right")
        else:
            ctk.CTkButton(btnrow, text="前往下载", height=40, corner_radius=12,
                          font=(FONT, 13, "bold"), command=lambda: self._open(web_url)
                          ).pack(side="left", fill="x", expand=True, padx=(0, 8))
            ctk.CTkButton(btnrow, text="稍后", width=90, height=40, corner_radius=12,
                          fg_color=C("input_bg"), text_color=C("text_2"), hover_color=C("input_hover"),
                          font=(FONT, 12), command=top.destroy).pack(side="right")

    def _open(self, url):
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            pass
        self.top.destroy()


def _ensure_deps():
    missing = []
    if not _MQTT_OK:
        missing.append("paho-mqtt（文字 / 文件传输）")
    if not _HAS_PIL:
        missing.append("Pillow（图片显示）")
    if not _HAS_CTK:
        missing.append("customtkinter（圆角界面）")
    if not _HAS_DND:
        missing.append("tkinterdnd2（拖拽发文件）")
    if not missing:
        return True

    install_cmd = "python -m pip install paho-mqtt pillow customtkinter tkinterdnd2"
    import tkinter as tk
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "缺少运行环境",
            "检测到缺少以下组件：\n\n" + "\n".join("  • " + m for m in missing)
            + "\n\n双击运行「安装环境.bat」即可一键补齐，\n或执行：\n\n" + install_cmd,
        )
        root.destroy()
    except Exception:
        print("[环境检查] 缺少组件：", "、".join(missing))
        print("请执行：", install_cmd)
    return False


def _crash_log_path():
    """崩溃日志写到用户可写的本地目录（Program Files 可能只读）。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or _base_dir()
    d = os.path.join(base, "P2PChat")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = _base_dir()
    return os.path.join(d, "crash.log")


def _write_crash(etype, value, tb):
    try:
        import traceback
        p = _crash_log_path()
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            f.write("".join(traceback.format_exception(etype, value, tb)))
            f.write("\n")
        return p
    except Exception:
        return ""


def _notify_crash(path):
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(
            "程序出错",
            "程序遇到错误，详细信息已写入：\n%s\n\n请把这份日志反馈给开发者以便修复。" % (path or "（无法写入日志）"))
        r.destroy()
    except Exception:
        pass


def _patch_focus_guards():
    """从源头消除「对已销毁控件 focus_set」的迟到回调错误。

    customtkinter 在 Windows 标题栏换色 / 最小化恢复时，会先保存当前焦点控件，再
    after(1, widget.focus) 延迟还原焦点（见 ctk_tk.py 内部逻辑）。若这 1ms 期间界面
    已被重建（会话列表刷新、对话框关闭等），旧控件已销毁，focus_set 就会抛
    'bad window path name'。这里给 tkinter 的 focus_set / focus_force 加一层
    winfo_exists 检查，控件不存在则静默跳过，彻底消除这类无害错误，不再落日志。
    """
    try:
        _orig_focus_set = tk.Misc.focus_set
        _orig_focus_force = tk.Misc.focus_force

        def _safe_focus_set(self):
            try:
                if self.winfo_exists():
                    _orig_focus_set(self)
            except Exception:
                pass

        def _safe_focus_force(self):
            try:
                if self.winfo_exists():
                    _orig_focus_force(self)
            except Exception:
                pass

        tk.Misc.focus_set = _safe_focus_set
        tk.Misc.focus_force = _safe_focus_force
        # 注意：tkinter 里 focus 是 focus_set 的别名（focus = focus_set），而
        # customtkinter 用的是 after(1, widget.focus)，需同步替换，否则仍走旧实现。
        tk.Misc.focus = _safe_focus_set
    except Exception:
        pass


def _install_excepthook():
    """把未捕获异常写到日志并弹窗，避免 --windowed 打包后静默闪退、无从排查。"""

    def hook(etype, value, tb):
        p = _write_crash(etype, value, tb)
        _notify_crash(p)
        try:
            sys.__excepthook__(etype, value, tb)
        except Exception:
            pass

    sys.excepthook = hook
    try:
        def thook(args):
            tb = getattr(args, "exc_traceback", None)
            p = _write_crash(args.exc_type, args.exc_value, tb)
            _notify_crash(p)
        threading.excepthook = thook
    except Exception:
        pass


def _is_benign_tk_error(value):
    """判断是否为无害的 Tk 错误：控件被销毁后的迟到回调（customtkinter 按钮点击动画、
    标题栏焦点恢复等）会抛 'bad window path name' / 'invalid command name'。这类错误只落
    日志、不弹窗打断用户，避免「程序出错」误报。"""
    try:
        if isinstance(value, tk.TclError):
            msg = str(value)
            return ("bad window path name" in msg) or ("invalid command name" in msg)
    except Exception:
        pass
    return False


def _report_callback_exception(etype, value, tb):
    # 替换 tkinter 的默认回调异常处理，落日志而不是静默闪退。
    # 注意：tkinter 以 report_callback_exception(etype, value, tb) 三个参数调用（这是
    # 普通函数而非绑定方法），签名必须恰好三个参数。若误写成 (self, etype, value, tb)，
    # 处理函数自身会抛 TypeError，把可恢复的回调错误升级成整个程序崩溃。
    if _is_benign_tk_error(value):
        _write_crash(etype, value, tb)
        return
    p = _write_crash(etype, value, tb)
    _notify_crash(p)


def main():
    global _DND_READY
    _patch_focus_guards()
    _install_excepthook()
    if not _ensure_deps():
        return
    # 读取上次保存的主题（默认暗色），与 C() 颜色体系保持一致
    _appearance = str(_load_settings().get("appearance", "dark")).strip()
    set_appearance(_appearance)
    ctk.set_default_color_theme("blue")
    try:
        root = ctk.CTk()
        if _HAS_DND:
            try:
                TkinterDnD.require(root)
                _DND_READY = True
            except Exception:
                _DND_READY = False
        root.report_callback_exception = _report_callback_exception
        # 单窗口：直接进入主界面，头像/昵称/ID 都在主界面内设置，不再有独立登录层
        profile = _load_profile()
        app = ChatApp(root, profile=profile, name=profile["name"], avatar=profile["avatar"])
        app.check_for_update()          # 后台静默检查更新
        root.mainloop()
    except Exception:
        p = _write_crash(*sys.exc_info())
        _notify_crash(p)
        raise


if __name__ == "__main__":
    main()