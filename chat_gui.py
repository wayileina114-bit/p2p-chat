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
- 提速：512KB 大分片 + 并发传输 + 局域网直连加速（同网段自动 TCP 直连，失败回退 MQTT）
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
import socket
import struct
import sys
import threading
import time
import uuid

# 图片缩略图解码并发上限：图片多时防止线程爆炸（每图一线程）
_THUMB_SEM = threading.Semaphore(4)
# 语音时长缓存：path -> 秒（避免重复读 wav 文件头）
_VOICE_DUR_CACHE = {}

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

APP_VERSION = "3.8.1"            # 程序版本（每次更新时 +1）
UPDATE_OWNER = "wayileina114-bit"  # GitHub 仓库所有者（自动检查更新用）
UPDATE_REPO = "p2p-chat"           # GitHub 仓库名（自动检查更新用）

DEFAULT_BROKER = "broker.emqx.io"
DEFAULT_PORT = 1883
# 辅助公共 MQTT broker：与主 broker 并行收发文本/控制消息（多通道加速，到达重复由 mid 去重）
AUX_BROKERS = [
    ("broker.hivemq.com", 1883),
    ("test.mosquitto.org", 1883),
    ("broker.emqx.io", 1883),
]
CHUNK_SIZE = 512 * 1024          # 每个分片 512KB（二进制直传，更大分片减少往返次数，提速）
LAN_PORT = 47654                  # 局域网直连监听端口（同网段文件加速，失败回退 MQTT）
LAN_DISCOVER_PORT = 47655        # 局域网成员自动发现端口（UDP 广播）
LAN_MSG_PORT = 47656              # 常驻消息直连端口（双栈：IPv4 局域网 + IPv6 公网直连，后台自动走直连）
MAX_FILE = 200 * 1024 * 1024     # 单文件上限 200MB
OFFER_TIMEOUT = 60.0             # 送文件请求 60 秒无人应答则取消
RECV_TIMEOUT = 120.0             # 接收方收不齐数据的超时（秒），超时清理残留分片
PRESENCE_TTL = 120.0             # 在线名单过期时间（秒），超时视为下线（兜底 will 丢失）
MAX_TEXT = 10000                 # 单条文字消息长度上限（防异常/恶意超长消息撑爆界面）
RECALL_WINDOW = 120             # 撤回时间窗（秒）：发送后 2 分钟内可撤回（QQ 同规则）
EDIT_WINDOW = 300               # 编辑时间窗（秒）：发送后 5 分钟内可编辑

FONT = "Microsoft YaHei UI"
HINT = "输入文字，回车发送；也可直接把图片 / 文件拖到这里"

EMOJI_GROUPS = [
    {"label": "表情 1", "items": [
        "😀","😁","😂","🤣","😊","😇","🙂","😉","😍","🥰",
        "😘","😗","😙","😚","😋","😛","😝","😜","🤪","🤨",
        "🧐","🤓","😎","🥸","🤩","🥳","😏","😒","😞","😔",
        "😟","😕","🙁","☹️","😣","😖","😫","😩","🥺","😢",
        "😭","😤","😠","😡","🤬","🤯","😳","🥵","🥶","😱",
        "😨","😰","😥","😓","🤗","🤔","🫡","🤭","🤫","🤥",
        "😶","😐","😑","😬","🙄","😯","😦","😧","😮","😲",
        "🥱","😴","🤤","😪","😵","🤐","🥴","🤢","🤮","🤧",
        "😷","🤒","🤕","🤑","🤠","😈","👿","👹","👺","🤡",
        "💩","👻","💀","👽","👾","🤖","🎃","😺","😸","😹",
    ]},
    {"label": "手势 / 人", "items": [
        "😻","😼","😽","🙀","😿","😾","🙈","🙉","🙊","👋",
        "🤚","🖐️","✋","🖖","👌","🤌","🤏","✌️","🤞","🫰",
        "🤟","🤘","🤙","👈","👉","👆","🖕","👇","☝️","👍",
        "👎","✊","👊","🤛","🤜","👏","🙌","👐","🤲","🤝",
        "🙏","✍️","💅","🤳","💪","🦾","🦵","🦶","👂","🦻",
        "👃","🧠","🦷","👀","👁️","👅","👄","👶","🧒","👦",
    ]},
    {"label": "心形 / 符号", "items": [
        "❤️","🧡","💛","💚","💙","💜","🖤","🤍","🤎","💔",
        "❣️","💕","💞","💓","💗","💖","💘","💝","💟","♥️",
        "💯","💢","💥","💫","💦","💨","💣","💬","💭","💤",
        "🔥","✨","⭐","🌟","⚡","🌈","☀️","☁️","⛅","🌤️",
        "🌥️","🌦️","🌧️","⛈️","🌩️","🌨️","❄️","☃️","⛄","💧",
        "🌊","🌋","🌍","🌎","🌏","🌱","🌿","🍀","🍁","🍂",
    ]},
    {"label": "植物 / 食物", "items": [
        "🍃","🌾","🌺","🌻","🌼","🌸","🌷","🌹","🥀","🌵",
        "🌲","🌳","🌴","🪴","🍄","🍏","🍎","🍐","🍊","🍋",
        "🍌","🍉","🍇","🍓","🫐","🍈","🍒","🍑","🥭","🍍",
        "🥥","🥝","🍅","🍆","🥑","🥦","🥬","🥒","🌶️","🫑",
        "🌽","🥕","🫒","🧄","🧅","🥔","🍠","🥐","🥯","🍞",
        "🥖","🥨","🧀","🥚","🍳","🥞","🧇","🥓","🥩","🍗",
    ]},
    {"label": "主食 / 甜品 / 饮料", "items": [
        "🍖","🦴","🌭","🍔","🍟","🍕","🫓","🥪","🥙","🧆",
        "🌮","🌯","🫔","🥗","🥘","🫕","🥣","🍛","🍜","🍝",
        "🍢","🍣","🍤","🍥","🥮","🍡","🥟","🦪","🍦","🍧",
        "🍨","🍩","🍪","🎂","🍰","🧁","🥧","🍫","🍬","🍭",
        "🍮","🍯","🍼","🥛","☕","🫖","🍵","🍶","🍾","🍷",
        "🍸","🍹","🍺","🍻","🥂","🥃","🫗","🧊","🎉","🎊",
    ]},
    {"label": "活动 / 运动 / 物品", "items": [
        "🎁","🏆","🥇","🥈","🥉","🏅","🎖️","🎗️","🎫","🎟️",
        "🎪","🤹","🎭","🎨","🎬","🎤","🎧","🎼","🎹","🥁",
        "🎷","🎺","🎸","🪕","🎻","🎞️","📷","📸","📹","📼",
        "🔍","🔎","💡","🔦","🏮","🪔","📔","📕","📖","📗",
        "📘","📙","📚","📓","📒","📃","📜","📄","📰","🗞️",
        "📑","🔖","🏷️","💰","💴","💵","💶","💷","💸","💳",
        "🧾","✉️","📩","📨","📧","💌","📥","📤","📦","📌",
        "📍","📎","🖇️","📏","📐","📊","📈","📉","🗑️","🔒",
        "🔑","🔨","🪓","⛏️","⚒️","🛠️","🗡️","⚔️","🔫","🏹",
        "🛡️","🚗","🚕","🚙","🚌","🚎","🏎️","🚓","🚑","🚒",
        "🚐","🛻","🚚","🚛","🚜","🛵","🏍️","🚲","🛴","🛹",
        "⛵","🚤","🛳️","✈️","🛩️","🚁","🚀","🛸","⌛","⏳",
    ]},
]

EMOJIS = [em for g in EMOJI_GROUPS for em in g["items"]]


# ---------------------------------------------------------------------------
# 主题（暗色参考 Discord，亮色参考 QQ；可切换 + 持久化）
# ---------------------------------------------------------------------------

THEMES = {
    "dark": {
        "radius_scale": 1.0,
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
        "search_hl": "#f0b429",
    },
    "light": {
        "radius_scale": 1.0,
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
        "search_hl": "#f0b429",
    },
    # 夜樱（二次元）：深紫夜空底 + 樱粉点缀 + 更圆润的圆角
    "anime": {
        "radius_scale": 1.4,
        "app_bg": "#221826", "panel": "#2c1f33", "panel_2": "#362844",
        "input_bg": "#43334f", "input_hover": "#4f3d5e",
        "accent": "#ff7eb6", "accent_hover": "#ff66a8",
        "mine_bubble": "#ff7eb6", "mine_text": "#3a1030",
        "other_bubble": "#2c1f33", "other_text": "#f3e8f5",
        "text": "#f3e8f5", "text_2": "#c9aed4", "text_mute": "#9d84ab",
        "hover": "#3f2f4d", "selected_bg": "#4f3d5e", "selected_text": "#ffd6ea",
        "online": "#7ce8a8", "danger": "#ff5c7a",
        "warn_bg": "#4a3350", "warn_text": "#ffd166", "section": "#c9aed4",
        "mute": "#9d84ab", "ok": "#7ce8a8", "err": "#ff5c7a",
        "search_hl": "#ffb347",
    },
}

_APPEARANCE = "dark"
_ACCENT_OVERRIDE = None   # 用户自定义强调色（hex）；None = 用主题默认色


def _shade_hex(hex_color, factor):
    """把 hex 颜色加深（factor<1）/提亮（factor>1），用于派生 hover 色。"""
    try:
        c = str(hex_color or "").strip().lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        if len(c) != 6:
            return hex_color
        rgb = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
        rgb = tuple(max(0, min(255, int(round(v * factor)))) for v in rgb)
        return "#%02x%02x%02x" % rgb
    except Exception:
        return hex_color


def R(n):
    """按当前主题的圆角缩放系数取圆角值（二次元主题更圆润）。"""
    try:
        scale = float(THEMES.get(_APPEARANCE, THEMES["dark"]).get("radius_scale", 1.0))
        return int(round(n * scale))
    except Exception:
        return n


def _win11_round_corners(hwnd):
    """Win11：给窗口加系统圆角（DWM 圆角偏好），美化观感；失败静默。"""
    try:
        import ctypes
        if os.name != "nt" or not hwnd:
            return
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(hwnd)), DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


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
            # ctk 只有 dark/light；anime 属暗色系
            ctk.set_appearance_mode("dark" if mode in ("dark", "anime") else "light")
        except Exception:
            pass


def _detect_system_theme():
    """检测 Windows 系统深浅色（注册表 Personalize 下 AppsUseLightTheme）；失败默认暗色。"""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
                return "light" if int(v) else "dark"
        except Exception:
            pass
    return "dark"


def C(key):
    """取当前主题色；未知 key 回退主文字色，避免界面崩溃。
    自定义强调色（_ACCENT_OVERRIDE）覆盖 accent / accent_hover。"""
    pal = THEMES.get(_APPEARANCE, THEMES["dark"])
    if _ACCENT_OVERRIDE and key in ("accent", "accent_hover"):
        return _ACCENT_OVERRIDE if key == "accent" else _shade_hex(_ACCENT_OVERRIDE, 0.82)
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
        "bio": str(d.get("bio", "")).strip(),
    }


def _save_profile(name, avatar, bio=""):
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "profile.json")
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"cid": _load_identity(), "name": (name or "").strip(),
                       "avatar": avatar or "", "bio": (bio or "").strip()}, f, ensure_ascii=False)
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


def _get_lan_ip():
    """探测本机局域网 IP（用于同网段直连加速）；失败返回空串。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return ""


def _is_global_v6(addr):
    """IPv6 地址是否为全局单播（2000::/3）——排除链路本地 fe80::/10、ULA fc00::/7、
    环回 ::1、IPv4 映射 ::ffff: 与临时区段 % 后缀。"""
    if not addr or "%" in addr or "::ffff:" in addr.lower():
        return False
    try:
        import ipaddress
        ip = ipaddress.ip_address(addr)
        return ip.version == 6 and ip.is_global and not ip.is_private and not ip.is_link_local
    except Exception:
        return False


def _get_global_v6():
    """探测本机 IPv6 全局地址（家庭宽带的公网 IPv6 直连用）；无公网 IPv6 返回空串。

    通过向公共 IPv6 地址建一个不发数据的 UDP socket 获取本机出口地址，
    不实际发包，纯本地探测；仅保留全局单播地址。
    """
    try:
        if not socket.has_ipv6:
            return ""
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            s.connect(("2400:3200::1", 80))  # 公共 IPv6 DNS，仅取本机出口地址
            addr = s.getsockname()[0]
            if _is_global_v6(addr):
                return addr
        finally:
            s.close()
    except Exception:
        pass
    # 兜底：枚举本机所有 IPv6 地址找全局单播
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            addr = info[4][0]
            if _is_global_v6(addr):
                return addr
    except Exception:
        pass
    return ""


def _is_voice_name(name):
    """判断文件名是否是支持的语音格式。"""
    return str(name or "").lower().endswith((".wav", ".mp3", ".ogg", ".m4a", ".aac", ".flac"))


_VOICE_SPEED = [1.0, 1.5, 2.0]   # 语音播放倍速档位


def _play_voice(path, speed=1.0):
    """播放语音：WAV 用 winsound（可变速用 sounddevice），其它格式用系统播放器。

    speed=1.0 用 winsound（低开销）；speed>1 用 sounddevice+numpy 重采样变速。
    """
    if not path or not os.path.isfile(path):
        return
    try:
        if str(path).lower().endswith(".wav"):
            if speed and speed > 1.01:
                import threading as _th
                _th.Thread(target=_play_wav_speed, args=(path, speed), daemon=True).start()
                return
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        elif os.name == "nt":
            os.startfile(path)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def _play_wav_speed(path, speed):
    """用 sounddevice 以指定倍速播放 WAV（numpy 线性插值重采样）。"""
    try:
        import sounddevice as sd
        import numpy as np
        import wave
        with wave.open(path, "rb") as wf:
            rate = wf.getframerate()
            n = wf.getnframes()
            raw = wf.readframes(n)
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        # 线性插值变速
        if speed and abs(speed - 1.0) > 0.01:
            n_out = int(len(data) / speed)
            idx = np.linspace(0, len(data) - 1, n_out)
            data = np.interp(idx, np.arange(len(data)), data)
        sd.play(data, rate)
        sd.wait()
    except Exception:
        pass


def _record_voice_to(path, stop_evt, rate=16000):
    """后台线程录音到 WAV（sounddevice），直到 stop_evt 被设置。"""
    try:
        import sounddevice as sd
        import numpy as np
        import wave
        chunks = []

        def _cb(indata, frames, time_info, status):
            chunks.append(indata.copy())

        with sd.InputStream(samplerate=rate, channels=1, dtype="int16", callback=_cb):
            while not stop_evt.is_set():
                time.sleep(0.05)
        data = np.concatenate(chunks) if chunks else np.zeros((0, 1), dtype="int16")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(data.tobytes())
        return True
    except Exception:
        return False


def _make_qr_png(data, path):
    """生成二维码 PNG（名片用）；失败返回空串。"""
    try:
        import qrcode
        img = qrcode.make(str(data))
        img.save(path)
        return path
    except Exception:
        return ""


def _read_qr_text(path):
    """识别二维码图片，返回解码文本；失败返回空串。"""
    try:
        import zxingcpp
        from PIL import Image
        img = Image.open(path).convert("RGB")
        for r in zxingcpp.read_barcodes(img):
            return str(r.text)
    except Exception:
        return ""


_NAME_COLOR_CACHE = {}


def _name_color(name):
    """根据昵称生成稳定的头像底色（Discord 风格彩色首字母，结果缓存）。"""
    key = str(name or "?")
    if key in _NAME_COLOR_CACHE:
        return _NAME_COLOR_CACHE[key]
    palette = ["#5865f2", "#3ba55d", "#faa61a", "#ed4245", "#eb459e",
               "#00a8fc", "#9146ff", "#f47fff"]
    h = 0
    for ch in key:
        h = (h * 31 + ord(ch)) & 0xffffffff
    c = palette[h % len(palette)]
    if len(_NAME_COLOR_CACHE) > 512:
        _NAME_COLOR_CACHE.clear()
    _NAME_COLOR_CACHE[key] = c
    return c


_URL_RE = None


def _extract_search_snippet(text, query, width=28):
    """提取搜索命中的上下文片段（关键词前后各截取干字符），未命中返回空。"""
    try:
        q = str(query or "").strip().lower()
        t = str(text or "")
        if not q or q not in t.lower():
            return ""
        idx = t.lower().index(q)
        start = max(0, idx - width)
        end = min(len(t), idx + len(q) + width)
        pre = "…" if start > 0 else ""
        post = "…" if end < len(t) else ""
        return pre + t[start:end] + post
    except Exception:
        return ""


def _extract_urls(text):
    """从文本中提取 http(s):// 开头的链接（去重、去掉尾部标点）。"""
    global _URL_RE
    if _URL_RE is None:
        import re
        _URL_RE = re.compile(r"https?://[^\s　、。，一-鿿]+", re.IGNORECASE)
    try:
        out = []
        for m in _URL_RE.findall(str(text or "")):
            u = m.rstrip(".,;:!?)]}>，。；：！？）》、】")
            if u and u not in out:
                out.append(u)
        return out[:6]
    except Exception:
        return []


def _extract_mentions(text, names):
    """从消息文本中提取 @提及的昵称（匹配当前会话可提及成员）。"""
    out = []
    try:
        for n in names or []:
            n = str(n).strip()
            if not n:
                continue
            tag = "@" + n
            if tag in str(text or "") and n not in out:
                out.append(n)
    except Exception:
        pass
    return out[:8]


def _open_url(url):
    """用系统默认浏览器打开链接。"""
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass

IMAGE_COMPRESS_THRESHOLD = 3 * 1024 * 1024   # 大于 3MB 的图片自动压缩
IMAGE_MAX_EDGE = 1920                         # 最长边超过 1920px 时缩小
IMAGE_JPEG_QUALITY = 82                       # JPEG 质量


def _is_image_path(path):
    try:
        return is_image(guess_mime(str(path)))
    except Exception:
        return False


def _auto_compress_image(path):
    """大图压缩：大于 3MB 或最长边超 1920px 时，生成压缩副本发送（原图不动）。
    压缩前先检查是否已有缓存，避免重复耗时；失败时返回原路径。"""
    try:
        if not (_HAS_PIL and path and os.path.isfile(path)):
            return path
        size = os.path.getsize(path)
        if size <= IMAGE_COMPRESS_THRESHOLD:
            return path
        from PIL import Image
        img = Image.open(path)
        w, h = img.size
        if max(w, h) <= IMAGE_MAX_EDGE:
            return path
        ratio = IMAGE_MAX_EDGE / float(max(w, h))
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
        cache_dir = os.path.join(DATA_DIR, "thumb")
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except Exception:
            cache_dir = DATA_DIR
        cache_key = "%s_%sx%s.jpg" % (os.path.basename(path).rsplit(".", 1)[0], img.size[0], img.size[1])
        out = os.path.join(cache_dir, cache_key)
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
        if img.mode not in ("RGB", "RGBA"):
            try:
                img = img.convert("RGB")
            except Exception:
                pass
        img.save(out, "JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
        img.close()
        return out if os.path.isfile(out) else path
    except Exception:
        return path


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


# ---------------------------------------------------------------------------
# 任务栏未读角标（Windows 7+ 通过 ITaskbarList3.SetOverlayIcon 显示红色数字徽标）
# ---------------------------------------------------------------------------
_TASKBAR3 = None  # 惰性初始化：None=未尝试 / False=失败 / dict=成功（含 COM 指针）


def _taskbar3_init():
    """初始化 ITaskbarList3 COM 接口（惰性，失败静默返回 False）。"""
    global _TASKBAR3
    if _TASKBAR3 is not None:
        return _TASKBAR3
    if os.name != "nt":
        _TASKBAR3 = False
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID2(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        # CLSID_TaskbarList / IID_ITaskbarList3
        CLSID_TaskbarList = _GUID2(0x56FDF344, 0xFD6D, 0x11D0,
                                   (0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90))
        IID_ITaskbarList3 = _GUID2(0xEA1AFB91, 0x9E28, 0x4B86,
                                   (0x90, 0xE9, 0x9E, 0x9F, 0x8A, 0x5E, 0xEF, 0xAF))
        ppv = ctypes.c_void_p()
        hr = ctypes.windll.ole32.CoCreateInstance(
            ctypes.byref(CLSID_TaskbarList), None, 1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(IID_ITaskbarList3), ctypes.byref(ppv))
        if hr != 0 or not ppv.value:
            _TASKBAR3 = False
            return False
        _TASKBAR3 = {
            "ppv": ppv,
            "vtbl": ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p)).contents,
        }
        return _TASKBAR3
    except Exception:
        _TASKBAR3 = False
        return False


def _taskbar3_set_overlay(hwnd, hicon, desc="未读消息"):
    """给任务栏按钮设置角标图标（hicon 为 0 时清除角标）。"""
    tb = _taskbar3_init()
    if not tb:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        vtbl = tb["vtbl"]
        # ITaskbarList::HrInit（vtable 第 4 项）
        hrinit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtbl[3])
        hrinit(tb["ppv"])
        # ITaskbarList3::SetOverlayIcon（vtable 第 19 项）
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND,
                                ctypes.c_void_p, wintypes.LPCWSTR)(vtbl[18])
        hr = fn(tb["ppv"], hwnd, ctypes.c_void_p(int(hicon) if hicon else 0), desc)
        return hr == 0
    except Exception:
        return False


def _taskbar3_release():
    """释放 ITaskbarList3 COM 接口（程序退出时调用）。"""
    global _TASKBAR3
    tb = _TASKBAR3
    if not tb:
        return
    try:
        import ctypes
        release = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(tb["vtbl"][2])
        release(tb["ppv"])
    except Exception:
        pass
    _TASKBAR3 = None


def _build_badge_icon(n):
    """用 PIL 生成红色圆形未读数字图标，返回 HICON（失败返回 0）。"""
    try:
        import ctypes
        from PIL import Image, ImageDraw, ImageFont
        size = 32
        txt = str(n) if n < 100 else "99+"
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([1, 1, size - 2, size - 2], fill=(255, 59, 48, 255))
        try:
            font = ImageFont.truetype("segoeuib.ttf", 20 if len(txt) <= 2 else 15)
        except Exception:
            font = ImageFont.load_default()
        bbox = d.textbbox((0, 0), txt, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
               txt, font=font, fill=(255, 255, 255, 255))
        ico = os.path.join(DATA_DIR, "taskbar_badge.ico")
        img.save(ico, format="ICO", sizes=[(size, size)])
        # IMAGE_ICON=1, LR_LOADFROMFILE=0x10
        hicon = ctypes.windll.user32.LoadImageW(None, ico, 1, size, size, 0x00000010)
        return int(hicon) if hicon else 0
    except Exception:
        return 0


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


def _load_contacts():
    """加载收藏的联系人列表 [{cid, name}]。"""
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "contacts.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            arr = json.load(f)
            if isinstance(arr, list):
                return [{"cid": str(x.get("cid", "")), "name": str(x.get("name", "？"))}
                        for x in arr if isinstance(x, dict) and x.get("cid")]
    except Exception:
        pass
    return []


def _save_contacts(contacts):
    _ensure_data_dir()
    try:
        with open(os.path.join(DATA_DIR, "contacts.json"), "w", encoding="utf-8") as f:
            json.dump(list(contacts), f, ensure_ascii=False)
    except Exception:
        pass


_SETTINGS_CACHE = None  # 设置缓存：避免每次读取都走磁盘


def _load_settings():
    """读取设置（带内存缓存；写入后自动失效）。"""
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is not None:
        return dict(_SETTINGS_CACHE)
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "settings.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
            _SETTINGS_CACHE = dict(d)
            return d
    except Exception:
        return {}


def _save_settings(d):
    global _SETTINGS_CACHE
    _ensure_data_dir()
    p = os.path.join(DATA_DIR, "settings.json")
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        _SETTINGS_CACHE = dict(d)
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


def _fmt_full_time(ts):
    """把时间戳格式化成完整日期时间，无效则返回空串。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
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
    if m.get("edited"):
        msg["edited"] = True
    if m.get("pinned"):
        msg["pinned"] = True
    if m.get("voice"):
        msg["voice"] = True
    if m.get("reply"):
        msg["reply"] = dict(m["reply"])
    if m.get("reactions"):
        msg["reactions"] = {k: dict(v) for k, v in m["reactions"].items()}
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
                 on_recall=None, on_read=None, on_typing=None, on_delivered=None, on_edit=None, on_reaction=None, on_lan_peers=None, passphrase=""):
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
        self.on_edit = on_edit
        self.on_reaction = on_reaction
        self.on_lan_peers = on_lan_peers
        self._lan_peers = {}          # 同网段自动发现的成员：cid -> {name, lan_ip, rooms, last_seen}
        self._lan_stop_evt = threading.Event()
        self._lan_conns = {}          # cid -> 常驻 TCP 连接（IPv4 局域网 + IPv6 公网直连通道，全自动维护）
        self._v6_addr = _get_global_v6()  # 本机 IPv6 全局地址（家宽公网直连加速，无则空串）
        self._seen_mids = set()       # 近期已处理的消息 mid（直连 + MQTT 双通道去重）
        self._ctrl_seen = set()       # 控制类（撤回/已读/编辑/回应）去重键：kind:mid（与文本 mid 共用会导致撤回被吞）
        self._lan_msg_sock = None     # 常驻消息连接监听 socket
        self._lan_msg_stop = threading.Event()
        self._delivered_acked = set()  # 已自动回过“已送达”的消息 mid

        self.online = False
        self.running = False
        self._connected_once = False
        self._client = None
        self._aux_clients = []       # 辅助公共 broker 客户端（多通道文本/控制并行，加速）
        self._aux_online = set()     # 当前在线的辅助客户端（主通道断开时自动接棒，保持在线）
        self._outbox = []            # 发送失败待补发的消息：[(topic, payload, qos)]，通道恢复后自动重发
        self.rooms = {}              # 房间名 -> roomid
        self._room_by_id = {}        # roomid -> 房间名
        self.presence = {}           # cid -> {"name":.., "rooms":[..]}（全局在线名单）
        self._subscribed = set()
        self._aux_subscribed = set() # 辅助客户端已订阅的文本话题

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
            c.max_inflight_messages_set(256)
        except Exception:
            pass
        try:
            c.max_queued_messages_set(0)
        except Exception:
            pass
        c.will_set(self._topic_presence(), b"", qos=1, retain=True)
        return c

    def _build_aux(self, broker, port):
        """构建辅助公共 broker 客户端（多通道文本/控制并行加速，文件仍走主通道）。"""
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                            client_id=f"{self.cid}-a{len(self._aux_clients)}")
        except (AttributeError, TypeError):
            c = mqtt.Client(client_id=f"{self.cid}-a{len(self._aux_clients)}")
        c.on_connect = self._on_aux_connect
        c.on_message = self._on_message
        c.on_disconnect = self._on_aux_disconnect
        c.reconnect_delay_set(1, 60)
        try:
            c.max_inflight_messages_set(256)
        except Exception:
            pass
        return c

    def _try_firewall_rule(self):
        """尝试为直连端口添加 Windows 防火墙放行规则（IPv6 公网直连需要入站放行）。

        非阻塞后台执行：有管理员权限则自动放行 47654/47656 端口（TCP），
        无权限静默跳过（可手动在「帮助 → 开放直连端口」执行）。
        """
        try:
            import subprocess
            _flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            # text=False（bytes 输出）避免子进程输出含非 GBK 字节时解码崩溃
            _kw = dict(capture_output=True, timeout=6, creationflags=_flags, text=False)
            for port in (LAN_PORT, LAN_MSG_PORT):
                rule = f"P2PChat Direct {port}"
                subprocess.run(
                    ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule}"],
                    **_kw)
                subprocess.run(
                    ["netsh", "advfirewall", "firewall", "add", "rule",
                     f"name={rule}", "dir=in", "action=allow", "protocol=TCP",
                     f"localport={port}", "profile=any"],
                    **_kw)
        except Exception:
            pass

    def _on_aux_connect(self, client, userdata, flags, rc, properties=None):
        ok = (not rc.is_failure) if hasattr(rc, "is_failure") else (rc == 0)
        if not ok:
            return
        try:
            self._aux_online.add(id(client))
        except Exception:
            pass
        # 主通道未在线但辅助通道已就绪：辅助接棒，保持整体在线（断线智能切换）
        if not self.online and self.running:
            self.online = True
            self._fire_status(True, "主通道未就绪，已切换辅助通道保持在线")
        self._flush_outbox()  # 任一通道恢复即补发失败消息
        self._aux_subscribed = set()
        client.subscribe(f"{self.NS}/presence/+", qos=1)
        self._aux_subscribed.add(f"{self.NS}/presence/+")
        client.subscribe(self._topic_dm(), qos=1)
        self._aux_subscribed.add(self._topic_dm())
        for room in list(self.rooms.keys()):
            self._aux_subscribe_room(client, room)

    def _on_aux_disconnect(self, client, userdata, *args):
        try:
            self._aux_online.discard(id(client))
        except Exception:
            pass
        # 主通道断开且最后一个辅助也断开：整体掉线（等待任一通道重连）
        if self.online and not self._aux_online and self._client is None:
            self.online = False
            if self.running:
                self._fire_status(False, "全部通道断开，正在重连…")

    def _aux_subscribe_room(self, client, room):
        topic = f"{self._topic_room(room)}/msg"
        if topic in self._aux_subscribed:
            return
        client.subscribe(topic, qos=1)
        self._aux_subscribed.add(topic)

    def _publish_all(self, topic, payload, qos=1, retain=False, from_outbox=False):
        """多通道发布：主 broker + 所有在线辅助 broker（文本/控制类消息）。

        返回是否至少有一个通道成功投递（paho 为异步投递，此处以“已入队到在线
        客户端”为成功；任一通道在线即视为可发送，主通道断开时由辅助通道接棒）。
        发送失败时自动加入补发队列（_outbox），任一通道恢复后自动重发。
        """
        sent = False
        try:
            if self._client is not None:
                self._client.publish(topic, payload, qos=qos, retain=retain)
                sent = True
        except Exception:
            pass
        for c in list(self._aux_clients):
            try:
                if c is not None and id(c) in self._aux_online:
                    c.publish(topic, payload, qos=qos)
                    sent = True
            except Exception:
                pass
        if not sent and not from_outbox:
            try:
                self._outbox.append((topic, payload, qos))
                if len(self._outbox) > 200:
                    self._outbox.pop(0)
            except Exception:
                pass
        return sent

    def _flush_outbox(self):
        """通道恢复后自动补发之前失败的消息（按序重发，成功即移除）。"""
        if not self._outbox or not self._any_online():
            return
        kept = []
        for topic, payload, qos in self._outbox:
            ok = self._publish_all(topic, payload, qos=qos, from_outbox=True)
            if not ok:
                kept.append((topic, payload, qos))
        self._outbox = kept

    def _any_online(self):
        """是否有任一通道在线（主或辅助）。"""
        if self._client is not None and self.online:
            return True
        if self._aux_online:
            return True
        return self.online

    def _any_online(self):
        """是否有任一通道在线（主或辅助）。"""
        if self._client is not None and self.online:
            return True
        if self._aux_online:
            return True
        return self.online

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
            self._flush_outbox()  # 通道恢复，自动补发之前失败的消息
            msg = "已重新连接" if self._connected_once else "已连接"
            self._connected_once = True
            self._fire_status(True, msg)
        else:
            self.online = False
            self._fire_status(False, f"连接失败（{rc}）")

    def _on_disconnect(self, client, userdata, *args):
        # 主通道断开：若仍有辅助通道在线，整体保持在线（智能切换，不打断聊天）
        if self._aux_online and self.running:
            self._fire_status(False, "主通道断开，已自动切换辅助通道")
            return
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
        if data.get("cid") == self.cid:
            return
        _mid = str(data.get("mid", ""))
        if _mid and not self._check_seen(_mid):
            return
        kind = data.get("kind")
        if kind in ("recall", "read", "delivered", "edit", "reaction"):
            if not self._check_ctrl_seen(kind, _mid):
                return
        if kind == "recall":
            self._fire_recall(room, _mid, str(data.get("cid", "")))
            return
        if kind == "read":
            self._fire_read(room, _mid, str(data.get("cid", "")), str(data.get("name", "匿名")))
            return
        if kind == "typing":
            self._fire_typing(room, str(data.get("name", "匿名"))[:60], str(data.get("cid", "")))
            return
        if kind == "delivered":
            self._fire_delivered(room, _mid, str(data.get("cid", "")), str(data.get("name", "匿名")))
            return
        if kind == "edit":
            self._fire_edit(room, _mid, str(data.get("cid", "")), str(data.get("text", "")))
            return
        if kind == "reaction":
            self._fire_reaction(room, _mid, str(data.get("emoji", "")), str(data.get("cid", "")), str(data.get("name", "匿名")))
            return
        if kind not in (None, "text"):
            return
        self._auto_delivered(room, _mid, False)
        self._fire_text(room, str(data.get("name", "匿名"))[:60],
                        str(data.get("text", ""))[:MAX_TEXT], False, _mid,
                        data.get("reply"))

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
            v6 = str(data.get("v6") or "").strip()
            self.presence[cid] = {"name": name, "rooms": [str(r)[:60] for r in rooms], "ts": ts, "v6": v6}
            # IPv6 公网直连加速：对方有全局 IPv6 时后台自动建立常驻直连（全自动，界面不显示）
            if v6 and self.fernet is None:
                self._maybe_connect_v6(cid, v6)
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
        _retry_wait = 30
        while self.running:
            time.sleep(_retry_wait)
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
            try:
                had = bool(self._outbox)
                self._flush_outbox()
                # 指数退避：有补发任务时加速重试（30s），全空则放慢（60→120s 封顶），减少空转
                if had:
                    _retry_wait = 30
                else:
                    _retry_wait = min(120, _retry_wait * 2 if _retry_wait >= 60 else 60)
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
        dmid = str(data.get("mid", ""))
        kind = data.get("kind")
        if kind in ("recall", "read", "delivered", "edit", "reaction"):
            if not self._check_ctrl_seen(kind, dmid):
                return
        if kind == "recall":
            self._fire_recall(self.DM_FILE_PREFIX + str(sender), dmid, str(sender))
            return
        if kind == "read":
            self._fire_read(self.DM_FILE_PREFIX + str(sender), dmid, str(sender), str(data.get("name", "匿名")))
            return
        if kind == "typing":
            self._fire_typing(self.DM_FILE_PREFIX + str(sender), str(data.get("name", "匿名"))[:60], str(sender))
            return
        if kind == "delivered":
            self._fire_delivered(self.DM_FILE_PREFIX + str(sender), dmid, str(sender), str(data.get("name", "匿名")))
            return
        if kind == "edit":
            self._fire_edit(self.DM_FILE_PREFIX + str(sender), dmid, str(sender), str(data.get("text", "")))
            return
        if kind == "reaction":
            self._fire_reaction(self.DM_FILE_PREFIX + str(sender), dmid, str(data.get("emoji", "")), str(sender), str(data.get("name", "匿名")))
            return
        if kind not in (None, "text"):
            return
        if not sender or sender == self.cid:
            return
        _mid = str(data.get("mid", ""))
        if _mid and not self._check_seen(_mid):
            return
        self._auto_delivered(self.DM_FILE_PREFIX + str(sender), _mid, True)
        if self.on_dm:
            self.on_dm(sender, str(data.get("name", "匿名"))[:60],
                       str(data.get("text", ""))[:MAX_TEXT], _mid,
                       data.get("reply"))

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
        elif kind == "req":
            self._on_req(data)

    def _on_offer(self, room, data):
        if data.get("from") == self.cid:
            return
        tid = data.get("id")
        if not tid or not self._check_ctrl_seen("offer", tid):
            return  # 多通道下同一 offer 会重复到达，去重只处理一次
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
        self._auto_delivered(room, tid, str(room).startswith(self.DM_FILE_PREFIX))
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

    def _on_req(self, data):
        """接收方请求补发缺失分片：只补自己缺的 idx，多通道下自然去重。"""
        tid = data.get("id")
        p = self._pending.get(tid)
        if p is None or data.get("to") != self.cid or not p.get("accepted"):
            return
        idx_list = data.get("idx") or []
        idx_list = [int(x) for x in idx_list if str(x).isdigit()]
        if not idx_list:
            return
        threading.Thread(target=self._resend_chunks, args=(tid, idx_list), daemon=True).start()

    def _resend_chunks(self, tid, idx_list):
        """重新发布指定分片（补发请求；仅发送缺失部分，不整包重传）。"""
        p = self._pending.get(tid)
        if p is None:
            return
        room = p["room"]
        data_topic = self._file_topic_base(room) + "/file/data"
        try:
            fh = open(p["path"], "rb")
        except Exception:
            return
        try:
            for i in idx_list:
                if not self.online or self._client is None:
                    break
                if i >= p["total"]:
                    continue
                fh.seek(i * CHUNK_SIZE)
                piece = fh.read(CHUNK_SIZE)
                if not piece:
                    continue
                if p.get("enc") and self.fernet is not None:
                    piece = self.fernet.encrypt(piece)
                frame = b"C" + tid.encode("ascii") + struct.pack(">I", i) + piece
                try:
                    self._publish_all(data_topic, frame, qos=1, from_outbox=True)
                except Exception:
                    break
        finally:
            try:
                fh.close()
            except Exception:
                pass

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
        # 接收进度节流上报（约每 5% 一次），状态栏能看到接收百分比
        try:
            if r["got"] % max(1, r["total"] // 20) == 0 or r["got"] >= r["total"]:
                pct = int(r["got"] * 100 / max(1, r["total"]))
                self._fire_file(r["room"], "progress", {"name": r["name"], "percent": pct})
        except Exception:
            pass
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
        if not r.get("lan") and r["got"] < r["total"]:
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
        """接收方看门狗：每 6 秒向发送方请求缺失分片（应对公共 broker 丢包），
        超过 RECV_TIMEOUT 仍未收齐则清理残留，避免内存/磁盘泄漏。"""
        r = self._receivers.get(tid)
        if r is None:
            return
        start = time.time()
        next_req = time.time() + 6
        while self.running:
            r = self._receivers.get(tid)
            if r is None:
                return  # 已收齐（_finish 清理）或已被取消
            if r.get("got", 0) >= r.get("total", 0):
                return
            now = time.time()
            if now >= next_req:
                next_req = now + 6
                try:
                    missing = [i for i in range(r["total"]) if i not in r["received"]]
                    if missing and r.get("from"):
                        self._publish_ctrl(r["room"], {"kind": "req", "id": tid,
                                                       "to": r["from"], "idx": missing})
                except Exception:
                    pass
            if now - start >= RECV_TIMEOUT:
                self._receivers.pop(tid, None)
                try:
                    r["fh"].close()
                except Exception:
                    pass
                self._cleanup_part(r.get("tmp"))
                self._fire_file(r["room"], "error", {"name": r["name"], "msg": "接收超时，传输中断"})
                return
            time.sleep(0.5)

    # --------------------------- 对外方法 ---------------------------

    def start(self):
        if not _MQTT_OK:
            self._fire_status(False, "缺少 paho-mqtt 库（pip install paho-mqtt）")
            return
        self.running = True
        self._client = self._build()
        self._client.loop_start()
        # 多通道加速：同时连接辅助公共 broker（文本/控制并行，文件仍主通道）
        self._aux_clients = []
        for ab, ap in AUX_BROKERS:
            if (ab, ap) == (self.broker, self.port):
                continue
            try:
                c = self._build_aux(ab, ap)
                c.loop_start()
                c.connect_async(ab, ap, keepalive=30)
                self._aux_clients.append(c)
            except Exception:
                pass
        threading.Thread(target=self._prune_loop, daemon=True).start()
        threading.Thread(target=self._try_firewall_rule, daemon=True).start()
        # 后台局域网成员自动发现 + 常驻直连（同网段一切通讯自动走直连）
        self._lan_stop_evt = threading.Event()
        self._lan_msg_stop = threading.Event()
        threading.Thread(target=self._lan_discover_loop, daemon=True).start()
        if self.fernet is None:  # 明文模式才启用直连；加密时全走 MQTT 保证安全
            threading.Thread(target=self._lan_msg_listener, daemon=True).start()
        try:
            self._client.connect_async(self.broker, self.port, keepalive=30)
        except Exception as e:
            self._fire_status(False, f"连接失败：{e}")
            self.running = False

    def stop(self):
        self.running = False
        try:
            self._lan_stop_evt.set()
        except Exception:
            pass
        try:
            self._lan_msg_stop.set()
        except Exception:
            pass
        for cid, conn in list(getattr(self, "_lan_conns", {}).items()):
            try:
                conn.close()
            except Exception:
                pass
        self._lan_conns = {}
        try:
            if self._lan_msg_sock is not None:
                self._lan_msg_sock.close()
        except Exception:
            pass
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
        aux = list(self._aux_clients)
        self._aux_clients = []
        client = self._client
        self._client = None
        self.online = False
        def _teardown_aux(c):
            try:
                c.disconnect()
            except Exception:
                pass
            try:
                c.loop_stop(True)
            except TypeError:
                try:
                    c.loop_stop()
                except Exception:
                    pass
            except Exception:
                pass
        if aux:
            threading.Thread(target=lambda: [_teardown_aux(c) for c in aux], daemon=True).start()
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
        for c in list(self._aux_clients):
            try:
                if c is not None and id(c) in self._aux_online:
                    self._aux_subscribe_room(c, room)
            except Exception:
                pass
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
        for c in list(self._aux_clients):
            try:
                if c is not None:
                    t = f"{self._topic_room(room)}/msg"
                    try:
                        c.unsubscribe(t)
                    except Exception:
                        pass
                    self._aux_subscribed.discard(t)
            except Exception:
                pass
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
        if getattr(self, "hidden", False):
            # 隐身模式：广播空 presence（retain 置空），其他客户端会把我从在线名单移除
            self._publish_all(self._topic_presence(), b"", qos=1, retain=True)
            return
        payload = json.dumps({
            "name": self.nickname,
            "rooms": list(self.rooms.keys()),
            "v6": getattr(self, "_v6_addr", "") or _get_global_v6(),
            "ts": int(time.time()),
        }, ensure_ascii=False)
        self._publish_all(self._topic_presence(), payload, qos=1, retain=True)

    def _publish_ctrl(self, room, obj):
        """文件控制消息（offer/accept/reject）走多通道发布，单通道丢失时由其他通道送达。"""
        if self._client is None:
            return
        topic = self._file_topic_base(room) + "/file/ctrl"
        payload = json.dumps(obj, ensure_ascii=False)
        self._publish_all(topic, payload, qos=1, from_outbox=False)

    def change_nick(self, new):
        new = (new or "").strip() or "匿名"
        self.nickname = new
        self._publish_presence()

    # --------------------------- 发送 ---------------------------

    def send_text(self, room, text, reply=None):
        text = (text or "").strip()
        if not text or room not in self.rooms or not self._any_online():
            return False
        mid = uuid.uuid4().hex[:12]
        obj = {"name": self.nickname, "text": text, "cid": self.cid, "mid": mid}
        if reply:
            obj["reply"] = reply
        payload = json.dumps(obj, ensure_ascii=False)
        if self.fernet is not None:
            payload = json.dumps({"enc": self.fernet.encrypt(payload.encode("utf-8")).decode("ascii")})
        self._publish_all(self._topic_room(room) + "/msg", payload, qos=1)
        # 局域网直连加速：同房间的同网段成员也后台直发一份（MQTT 仍兜底，双通道去重）
        if self.fernet is None:
            for cid, p in list(self._lan_peers.items()):
                if room in (p.get("rooms") or []):
                    self._send_lan(cid, {"kind": "text", "room": room, "mid": mid,
                                         "name": self.nickname, "cid": self.cid,
                                         "text": text, "reply": reply or {}})
        self._fire_text(room, self.nickname, text, True, mid, reply)
        return True

    def send_dm(self, target_cid, text, reply=None):
        text = (text or "").strip()
        if not text or not self._any_online() or not target_cid:
            return False
        mid = uuid.uuid4().hex[:12]
        obj = {"name": self.nickname, "text": text, "cid": self.cid, "mid": mid}
        if reply:
            obj["reply"] = reply
        payload = json.dumps(obj, ensure_ascii=False)
        if self.fernet is not None:
            payload = json.dumps({"enc": self.fernet.encrypt(payload.encode("utf-8")).decode("ascii")})
        self._publish_all(f"{self.NS}/dms/{target_cid}", payload, qos=1)
        # 局域网直连加速：目标同网段时后台直发一份（MQTT 兜底，双通道去重）
        if self.fernet is None:
            self._send_lan(target_cid, {"kind": "dm", "to": target_cid, "mid": mid,
                                        "name": self.nickname, "cid": self.cid,
                                        "text": text, "reply": reply or {}})
        return True

    def send_recall(self, target, mid, is_dm=False):
        """撤回一条消息：向房间/私聊广播撤回指令。"""
        if not mid or not self._any_online():
            return False
        payload = json.dumps({"kind": "recall", "mid": mid, "cid": self.cid}, ensure_ascii=False)
        if is_dm:
            self._publish_all(f"{self.NS}/dms/{target}", payload, qos=1)
        else:
            self._publish_all(self._topic_room(target) + "/msg", payload, qos=1)
        return True

    def send_read(self, target, mid, is_dm=False):
        """发送已读回执：告知对方我已看到该消息。"""
        if not mid or not self._any_online():
            return False
        payload = json.dumps({"kind": "read", "mid": mid, "cid": self.cid, "name": self.nickname}, ensure_ascii=False)
        if is_dm:
            self._publish_all(f"{self.NS}/dms/{target}", payload, qos=1)
        else:
            self._publish_all(self._topic_room(target) + "/msg", payload, qos=1)
        return True

    def send_typing(self, target, is_dm=False):
        """广播“正在输入”状态（qos=0，瞬时、不持久化）。"""
        if not self._any_online() or not target:
            return False
        payload = json.dumps({"kind": "typing", "cid": self.cid, "name": self.nickname}, ensure_ascii=False)
        if is_dm:
            self._publish_all(f"{self.NS}/dms/{target}", payload, qos=0)
        else:
            self._publish_all(self._topic_room(target) + "/msg", payload, qos=0)
        return True

    def send_delivered(self, target, mid, is_dm=False):
        """发送“已送达”回执：我已收到该消息（尚未打开也算送达）。"""
        if not mid or not self._any_online():
            return False
        payload = json.dumps({"kind": "delivered", "mid": mid, "cid": self.cid, "name": self.nickname}, ensure_ascii=False)
        if is_dm:
            self._publish_all(f"{self.NS}/dms/{target}", payload, qos=1)
        else:
            self._publish_all(self._topic_room(target) + "/msg", payload, qos=1)
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

    def send_edit(self, target, mid, new_text, is_dm=False):
        """编辑一条已发送的消息：向房间/私聊广播修改内容。"""
        if not mid or not new_text or not self._any_online():
            return False
        payload = json.dumps({"kind": "edit", "mid": mid, "cid": self.cid,
                              "text": str(new_text)[:MAX_TEXT]}, ensure_ascii=False)
        if is_dm:
            self._publish_all(f"{self.NS}/dms/{target}", payload, qos=1)
        else:
            self._publish_all(self._topic_room(target) + "/msg", payload, qos=1)
        return True

    def send_reaction(self, target, mid, emoji, is_dm=False):
        """对一条消息做表情回应（再次发送同一表情则取消）。"""
        if not mid or not emoji or not self._any_online():
            return False
        payload = json.dumps({"kind": "reaction", "mid": mid, "emoji": str(emoji),
                              "cid": self.cid, "name": self.nickname}, ensure_ascii=False)
        if is_dm:
            self._publish_all(f"{self.NS}/dms/{target}", payload, qos=1)
        else:
            self._publish_all(self._topic_room(target) + "/msg", payload, qos=1)
        return True

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
            "accepted": False, "sent_via_lan": False,
            "evt": threading.Event(), "enc": self.fernet is not None,
        }
        offer = {"kind": "offer", "id": tid, "from": self.cid, "sname": self.nickname,
                 "name": name, "size": size, "mime": mime, "total": total, "md5": md5}
        if self.fernet is not None:
            offer["enc"] = True
        # 直连加速（局域网 IPv4 + 公网 IPv6）：明文文件时开启监听，对方可直连（失败自动回退 MQTT）
        if self.fernet is None:
            try:
                lan_ip = _get_lan_ip()
                v6 = getattr(self, "_v6_addr", "") or _get_global_v6()
                sock = None
                if socket.has_ipv6:
                    try:  # 双栈监听：IPv6 公网 + IPv4 局域网都能直连
                        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        try:
                            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                        except Exception:
                            pass
                        sock.bind(("::", LAN_PORT))
                        sock.listen(1)
                        self._pending[tid]["lan_sock"] = sock
                        threading.Thread(target=self._lan_listener, args=(tid,), daemon=True).start()
                    except Exception:
                        try:
                            if sock is not None:
                                sock.close()
                        except Exception:
                            pass
                        sock = None
                if sock is None:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("0.0.0.0", LAN_PORT))
                    sock.listen(1)
                    self._pending[tid]["lan_sock"] = sock
                    threading.Thread(target=self._lan_listener, args=(tid,), daemon=True).start()
                if lan_ip and not lan_ip.startswith("127."):
                    offer["lan_ip"] = lan_ip
                    offer["lan_port"] = LAN_PORT
                if v6 and ":" in v6:
                    offer["v6"] = v6
                    offer["v6_port"] = LAN_PORT
            except Exception:
                pass
        if is_image(mime):
            thumb = _make_thumb_base64(path)
            if thumb:
                offer["thumb"] = thumb
        self._pending[tid]["offer"] = offer  # 副本：10 秒未应答时重发
        self._publish_ctrl(room, offer)
        threading.Thread(target=self._watch_send, args=(tid,), daemon=True).start()
        self._fire_file(room, "waiting", {"name": name, "size": size, "tid": tid, "path": path, "mime": mime})
        return True

    def accept_file(self, tid):
        data = self._offers.pop(tid, None)
        if data is None:
            return
        room = data.get("room")
        if data.get("enc") and self.fernet is None:
            self._fire_file(room, "error", {"name": data["name"], "msg": "对方发送了加密文件，但你未设置加密口令"})
            return
        # 尝试直连（IPv6 公网优先，其次局域网 IPv4；失败自动回退 MQTT）
        if not data.get("enc"):
            v6 = data.get("v6")
            try:
                v6_port = int(data.get("v6_port", 0) or 0)
            except Exception:
                v6_port = 0
            if v6 and ":" in v6 and v6_port and self._try_lan_receive(tid, data, v6, v6_port, room, is_v6=True):
                return
            lan_ip = data.get("lan_ip")
            try:
                lan_port = int(data.get("lan_port", 0) or 0)
            except Exception:
                lan_port = 0
            if lan_ip and lan_port and self._try_lan_receive(tid, data, lan_ip, lan_port, room):
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
            "from": data.get("from", ""),  # 发送者 cid：缺失分片时向其请求补发
        }
        threading.Thread(target=self._watch_recv, args=(tid,), daemon=True).start()
        self._publish_ctrl(room, {"kind": "accept", "id": tid, "from": self.cid, "to": data["from"]})
        self._fire_file(room, "accepting", {"name": data["name"], "size": data["size"]})

    def _try_lan_receive(self, tid, data, lan_ip, lan_port, room, is_v6=False):
        """尝试通过直连（局域网 IPv4 / 公网 IPv6）TCP 接收；成功建立连接并启动接收线程返回 True。"""
        sock = None
        try:
            fam = socket.AF_INET6 if is_v6 else socket.AF_INET
            sock = socket.socket(fam, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect((lan_ip, lan_port))
            sock.settimeout(120)
        except Exception:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            return False
        d = DOWNLOADS_DIR
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, ".p2pchat-part-" + tid)
        try:
            fh = open(tmp, "wb")
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            return False
        self._receivers[tid] = {
            "name": data["name"], "size": data["size"], "mime": data["mime"],
            "total": data["total"], "md5": data["md5"],
            "received": set(), "got": 0, "fh": fh, "tmp": tmp,
            "sname": data.get("sname", "对方"), "room": room, "enc": False, "lan": True,
        }
        threading.Thread(target=self._lan_recv, args=(tid, sock), daemon=True).start()
        self._fire_file(room, "accepting", {"name": data["name"], "size": data["size"]})
        return True

    def _lan_recv(self, tid, sock):
        """局域网直连接收线程：流式落盘并校验。"""
        r = self._receivers.get(tid)
        if r is None:
            try:
                sock.close()
            except Exception:
                pass
            return
        try:
            last_pct = -1
            while True:
                data = sock.recv(262144)
                if not data:
                    break
                r["fh"].write(data)
                r["got"] += len(data)
                if r["size"] > 0:
                    pct = int(r["got"] * 100 / r["size"])
                    if pct >= last_pct + 20:
                        last_pct = pct
                        self._fire_file(r["room"], "progress", {"name": r["name"], "percent": pct})
            sock.close()
            self._finish(tid)
        except Exception:
            self._fire_file(r["room"], "error", {"name": r["name"], "msg": "局域网接收失败"})

    def reject_file(self, tid):
        data = self._offers.pop(tid, None)
        if data is None:
            return
        self._publish_ctrl(data.get("room"),
                           {"kind": "reject", "id": tid, "from": self.cid, "to": data["from"], "reason": "rejected"})

    # --------------------------- 局域网成员自动发现 ---------------------------

    def _lan_broadcast_payload(self):
        try:
            return json.dumps({"cid": self.cid, "name": self.nickname,
                               "lan_ip": _get_lan_ip(), "v6": getattr(self, "_v6_addr", "") or _get_global_v6(),
                               "rooms": list(self.rooms), "ts": time.time()}).encode("utf-8")
        except Exception:
            return b""

    def _lan_discover_loop(self):
        """后台线程：周期性 UDP 广播自己的存在，并监听同网段成员的广播。"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", LAN_DISCOVER_PORT))
            except Exception:
                sock.bind(("0.0.0.0", 0))
            sock.settimeout(0.5)
        except Exception:
            return
        last_bcast = 0.0
        changed = False
        while not self._lan_stop_evt.is_set():
            try:
                now = time.time()
                if now - last_bcast >= 5:
                    last_bcast = now
                    try:
                        sock.sendto(self._lan_broadcast_payload(), ("255.255.255.255", LAN_DISCOVER_PORT))
                    except Exception:
                        pass
                try:
                    data, addr = sock.recvfrom(65535)
                    try:
                        info = json.loads(data.decode("utf-8"))
                        cid = str(info.get("cid", ""))
                        if cid and cid != self.cid and addr[0]:
                            lan_ip = str(info.get("lan_ip") or addr[0])
                            p = self._lan_peers.get(cid)
                            if p is None or p.get("lan_ip") != lan_ip or p.get("name") != info.get("name"):
                                changed = True
                            was_new = cid not in self._lan_peers
                            self._lan_peers[cid] = {
                                "name": str(info.get("name", "匿名"))[:60],
                                "lan_ip": lan_ip,
                                "rooms": list(info.get("rooms") or []),
                                "last_seen": now,
                            }
                            if was_new:
                                self._maybe_connect_lan(cid, lan_ip)
                    except Exception:
                        pass
                except socket.timeout:
                    pass
                # 清理超时成员
                stale = [c for c, p in self._lan_peers.items() if now - p["last_seen"] > 20]
                if stale:
                    for c in stale:
                        self._lan_peers.pop(c, None)
                    changed = True
                if changed:
                    changed = False
                    self._fire_lan_peers()
            except Exception:
                pass
            time.sleep(0.2)
        try:
            sock.close()
        except Exception:
            pass

    def _fire_lan_peers(self):
        if self.on_lan_peers:
            try:
                self.on_lan_peers({cid: {"name": p["name"], "lan_ip": p["lan_ip"], "rooms": p["rooms"]}
                                   for cid, p in self._lan_peers.items()})
            except Exception:
                pass

    # --------------------------- 局域网常驻直连（全自动） ---------------------------

    @staticmethod
    def _lan_read_line(sock):
        """从 socket 读一行（到 \n），返回 str 或 None（超时/断开）。"""
        buf = b""
        try:
            sock.settimeout(30)
            while True:
                ch = sock.recv(1)
                if not ch:
                    return None
                if ch == b"\n":
                    return buf.decode("utf-8", "ignore")
                buf += ch
                if len(buf) > 65536:
                    return None
        except Exception:
            return None

    def _maybe_connect_lan(self, cid, ip):
        """与同网段成员建立常驻直连（后台线程，不阻塞发现循环）。"""
        if not cid or cid == self.cid or self.fernet is not None:
            return
        if cid in self._lan_conns:
            return
        if self.cid >= cid:  # 约定：cid 小者主动连，避免双向重复建连
            return
        threading.Thread(target=self._lan_connect_worker, args=(cid, ip, False), daemon=True).start()

    def _maybe_connect_v6(self, cid, v6):
        """与公网 IPv6 成员建立常驻直连（家宽 IPv6 公网直连加速，全自动后台）。"""
        if not cid or cid == self.cid or self.fernet is not None:
            return
        if not v6 or ":" not in v6:
            return
        if cid in self._lan_conns:
            return
        if self.cid >= cid:  # 约定：cid 小者主动连，避免双向重复建连
            return
        threading.Thread(target=self._lan_connect_worker, args=(cid, v6, True), daemon=True).start()

    def _lan_connect_worker(self, cid, ip, is_v6=False):
        conn = None
        try:
            conn = socket.create_connection((ip, LAN_MSG_PORT), timeout=3)
            conn.settimeout(30)
            conn.sendall((json.dumps({"hello": 1, "cid": self.cid, "v6": is_v6}) + "\n").encode("utf-8"))
            threading.Thread(target=self._conn_reader, args=(conn,), daemon=True).start()
        except Exception:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def _lan_msg_listener(self):
        """常驻直连监听线程：双栈（IPv4 局域网 + IPv6 公网）接受成员的主动连接。"""
        sock = None
        try:
            if socket.has_ipv6:
                try:
                    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                    except Exception:
                        pass
                    sock.bind(("::", LAN_MSG_PORT))
                    sock.listen(8)
                    sock.settimeout(1.0)
                    self._lan_msg_sock = sock
                except Exception:
                    try:
                        if sock is not None:
                            sock.close()
                    except Exception:
                        pass
                    sock = None
            if sock is None:  # 无 IPv6 或双栈失败：回退 IPv4 监听
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", LAN_MSG_PORT))
                sock.listen(8)
                sock.settimeout(1.0)
                self._lan_msg_sock = sock
        except Exception:
            return
        while not self._lan_msg_stop.is_set():
            try:
                conn, _addr = sock.accept()
                conn.settimeout(30)
                conn.sendall((json.dumps({"hello": 1, "cid": self.cid}) + "\n").encode("utf-8"))
                threading.Thread(target=self._conn_reader, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                try:
                    time.sleep(0.2)
                except Exception:
                    pass
        try:
            sock.close()
        except Exception:
            pass

    def _conn_reader(self, conn):
        """连接读线程：先收 hello 确认身份，再逐帧处理。"""
        peer_cid = ""
        try:
            line = self._lan_read_line(conn)
            if not line:
                return
            try:
                h = json.loads(line)
                peer_cid = str(h.get("cid", ""))
            except Exception:
                return
            if not peer_cid or peer_cid == self.cid:
                return
            # 去重：若已有同 cid 连接则关闭新连接
            old = self._lan_conns.get(peer_cid)
            if old is not None and old is not conn:
                try:
                    conn.close()
                except Exception:
                    pass
                return
            self._lan_conns[peer_cid] = conn
            # 循环读帧
            while not self._lan_msg_stop.is_set():
                frame = self._lan_read_line(conn)
                if frame is None:
                    break
                try:
                    self._handle_lan_frame(peer_cid, frame)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if self._lan_conns.get(peer_cid) is conn:
                try:
                    self._lan_conns.pop(peer_cid, None)
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

    def _send_lan(self, peer_cid, obj):
        """向同网段成员常驻连接发送一帧（失败即断开连接，消息仍由 MQTT 兜底）。"""
        conn = self._lan_conns.get(peer_cid)
        if conn is None:
            return
        try:
            conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            try:
                self._lan_conns.pop(peer_cid, None)
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _handle_lan_frame(self, peer_cid, line):
        """处理局域网直连帧（文字/私聊）。"""
        try:
            data = json.loads(line)
        except Exception:
            return
        kind = data.get("kind")
        mid = str(data.get("mid", ""))
        if kind == "text" and mid:
            if not self._check_seen(mid):
                return
            room = str(data.get("room", ""))
            if room in self.rooms:
                self._fire_text(room, str(data.get("name", "匿名"))[:60],
                                str(data.get("text", ""))[:MAX_TEXT], False, mid,
                                data.get("reply"))
        elif kind == "dm" and mid:
            if not self._check_seen(mid):
                return
            sender = str(data.get("cid", ""))
            if sender and sender != self.cid and self.on_dm:
                self.on_dm(sender, str(data.get("name", "匿名"))[:60],
                           str(data.get("text", ""))[:MAX_TEXT], mid, data.get("reply"))

    def _check_seen(self, mid):
        """消息去重：已处理过返回 False，否则记录并返回 True。"""
        if mid in self._seen_mids:
            return False
        if len(self._seen_mids) > 4000:
            self._seen_mids.clear()
        self._seen_mids.add(mid)
        return True

    def _check_ctrl_seen(self, kind, mid):
        """控制类消息去重（多通道下同一控制指令会重复到达）。"""
        if not mid:
            return True
        key = f"{kind}:{mid}"
        if key in self._ctrl_seen:
            return False
        if len(self._ctrl_seen) > 4000:
            self._ctrl_seen.clear()
        self._ctrl_seen.add(key)
        return True

    # --------------------------- 文件发送流程 ---------------------------

    def _lan_listener(self, tid):
        """局域网直连监听线程：接受一次连接并把文件经 TCP 发给对方。"""
        p = self._pending.get(tid)
        if p is None:
            return
        sock = p.get("lan_sock")
        if sock is None:
            return
        try:
            sock.settimeout(12)
            conn, _addr = sock.accept()
            conn.settimeout(120)
            with open(p["path"], "rb") as fh:
                while True:
                    data = fh.read(262144)
                    if not data:
                        break
                    conn.sendall(data)
            try:
                conn.close()
            except Exception:
                pass
            if self._pending.get(tid) is p:
                p["sent_via_lan"] = True
                p["evt"].set()
        except Exception:
            pass  # 局域网直连失败/超时，回退 MQTT
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _watch_send(self, tid):
        p = self._pending.get(tid)
        if p is None:
            return
        # 10 秒未收到应答：重发一次 offer（防止单通道把请求弄丢，公共 broker 常见）
        if not p["evt"].wait(10):
            p = self._pending.get(tid)
            if p is not None and not p.get("accepted") and not p.get("sent_via_lan"):
                try:
                    if p.get("offer"):
                        self._publish_ctrl(p["room"], p["offer"])
                except Exception:
                    pass
        p = self._pending.get(tid)
        if p is None:
            return
        p["evt"].wait(max(0, OFFER_TIMEOUT - 10))
        p = self._pending.get(tid)
        if p is None:
            return
        if p.get("sent_via_lan"):
            self._pending.pop(tid, None)
            self._fire_file(p["room"], "sent", {"name": p["name"], "size": p["size"], "mime": p["mime"], "path": p["path"]})
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
                    # 多通道发布：主 + 辅助 broker 都发，接收端按 idx 去重。
                    # 分片是海量二进制数据，绝不进文字补发队列（from_outbox=True），
                    # 否则断网时会用 512KB 分片塞满 outbox 拖垮所有文字消息甚至内存崩溃。
                    # 丢失的分片由接收端周期「请求补发」（见 _watch_recv / _on_req）兜底。
                    self._publish_all(data_topic, frame, qos=1, from_outbox=True)
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

    def _fire_text(self, room, name, text, mine=False, mid=None, reply=None):
        if self.on_text:
            self.on_text(room, name, text, mine, mid, reply)

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

    def _fire_edit(self, room, mid, who, text):
        if self.on_edit:
            self.on_edit(room, mid, who, text)

    def _fire_reaction(self, room, mid, emoji, cid, name):
        if self.on_reaction:
            self.on_reaction(room, mid, emoji, cid, name)

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

    def __init__(self, root, profile=None, name="", avatar="", bio=""):
        self.root = root
        self.cid = (profile or {}).get("cid") or _load_identity()
        self._profile_name = name
        self._avatar = avatar
        self._bio = bio
        self.appearance = _APPEARANCE
        global _ACCENT_OVERRIDE
        _ACCENT_OVERRIDE = str(_load_settings().get("accent_override", "") or "").strip() or None
        self._appearance_mode = str(_load_settings().get("appearance_mode", "system") or "system").strip()
        if self._appearance_mode == "system":
            self.appearance = _detect_system_theme()
        elif self._appearance_mode in THEMES:
            self.appearance = self._appearance_mode
        self.backend = None
        self._peers = {}            # cid -> {"name":.., "rooms":[..]}
        self._rooms = []            # 已加入房间（有序）
        self._sessions = {}         # key -> 会话（群聊 + 私聊）
        self._mid_index = {}         # key -> {mid: msg} 消息索引（右键/跳转/回执 O(1) 反查，免线性扫描）
        self._current = None
        self._images = []
        self._pending_offers = {}
        self._hint_active = True
        self._thumb_cache = {}      # 图片缩略图缓存：path -> CTkImage
        self._my_avatar_ctk = None  # 自己的圆形头像缓存
        self._last_title = ""       # 窗口标题缓存（避免频繁重设）
        self._unread_total = 0      # 未读总数缓存（增量维护，避免每次标题/角标全量 sum）
        self._read_acked = set()     # 已发送过已读回执的消息 mid
        self._stick_bottom = True   # 新消息到达前用户是否贴底（自动滚动判断）
        self._new_msg_floating = None  # “↓ 新消息”浮标（用户上翻时新消息到达提示）
        self._search_query = ""      # 会话内消息搜索关键词（空 = 未搜索）
        self._expanded_msgs = set()  # 已展开全文的长消息 mid（不再折叠）
        self._feed_filter = ""       # 消息筛选："" 全部 / "img" 只看图片 / "file" 只看文件
        self._msg_search_after = None  # 消息搜索防抖 timer id
        self._suppress_auto_scroll = False  # 全量渲染时抑制逐条自动滚动，避免布局抖动/残影
        self._window_focused = True    # 窗口是否聚焦（后台/最小化时不聚焦，用于弹通知）
        self._typing_after = None      # “正在输入”提示的延时恢复 timer id
        self._typing_last = 0.0        # 上次发送“正在输入”广播的时间戳（节流用）
        self._members_visible = False  # 成员列表面板是否展开
        self._reply_to = None          # 正在引用的消息 {"name":..,"text":..}
        self._dnd = bool(_load_settings().get("dnd", False))       # 免打扰（静音通知+提示音，持久化）
        self._ghost = bool(_load_settings().get("ghost", False))   # 隐身模式（不广播在线状态，仍可收发消息，持久化）
        self._pin_var = tk.BooleanVar(value=bool(_load_settings().get("pin_window", False)))  # 窗口置顶
        self._muted = set(_load_settings().get("muted_sessions", []) or [])  # 静音会话 key 集合
        self._lan_peers = {}          # 同网段自动发现的成员：cid -> {name, lan_ip, rooms}
        self._pinned_sessions = set(_load_settings().get("pinned_sessions", []) or [])  # 置顶会话 key 集合
        self._last_list_fp = None          # 会话列表指纹（无实质变化时跳过重建，减卡顿）
        self._mention_cache = None        # 可 @ 成员名缓存（当前会话名列表；渲染时避免重复构建）
        self._bubble_frames = {}       # mid -> 气泡容器（用于局部刷新回应，避免整页重渲染）
        self._playing_voice = None    # 当前播放中的语音文件路径
        self._voice_start_ts = 0.0    # 当前播放开始时间戳
        self._voice_btns = {}         # 语音路径 -> 播放按钮（播放时更新文本）
        self._voice_bars = {}         # 语音路径 -> 进度条
        self._voice_durs = {}         # 语音路径 -> 时长（秒）
        self._voice_speeds = {}       # 语音路径 -> 倍速档位索引
        self._voice_spd_btns = {}     # 语音路径 -> 倍速按钮
        self._voice_tick_job = None   # 进度刷新的 after 任务 id
        self._playing_btn = None      # 当前播放语音的按钮（同路径多条消息时不串控件）
        self._playing_bar = None      # 当前播放语音的进度条
        self._reaction_rows = {}       # mid -> 回应 badge 行控件
        self._feed_after = None        # 已读/送达/编辑/撤回回执的合并重渲染 timer id
        self._body_labels = {}         # mid -> 正文 label（局部更新编辑）
        self._footer_labels = {}       # mid -> 已读/送达/已编辑 标签（局部更新回执）
        self._hover_bar = None         # 当前悬停快捷操作浮层（同时只存在一个）
        self._hover_mid = None         # 浮层对应的消息 mid
        self._hover_after = None       # 浮层显示/隐藏的延时 timer id
        self._last_badge_n = None      # 上次任务栏角标的未读数（无变化不重绘）
        self._hover_inside = False      # 鼠标是否仍在悬停目标上
        self._emoji_focus_after = None  # 表情面板失焦延迟关闭的定时器 id（可取消）
        self._emoji_opened_at = 0.0     # 表情面板最近一次打开时间（刚打开瞬间的焦点抖动不关面板）
        self._overlay_hicon = 0        # 当前任务栏角标 HICON（替换前销毁旧句柄）
        self._search_after = None   # 搜索防抖 timer id
        self._list_after = None     # 会话列表防抖 timer id
        self.auto_connect = bool(_load_settings().get("auto_connect", True))
        self.enter_sends = bool(_load_settings().get("enter_sends", True))
        # 聊天字号（设置中心可调：小 12 / 中 13 / 大 15，持久化）
        try:
            self._chat_font_size = max(10, min(20, int(_load_settings().get("chat_font_size", 13) or 13)))
        except Exception:
            self._chat_font_size = 13
        # 聊天背景："" 默认 / "deep" 深邃 / "sakura" 樱花（持久化）
        try:
            self._chat_bg = str(_load_settings().get("chat_bg", "") or "")
        except Exception:
            self._chat_bg = ""
        self._history_expanded = False  # 是否已展开“更早消息”
        self.notify_sound = bool(_load_settings().get("notify_sound", True))
        self.notify_popup = bool(_load_settings().get("notify_popup", True))
        self.encrypt_pass = str(_load_settings().get("encrypt_pass", "") or "")
        self.broker = str(_load_settings().get("broker", "") or "").strip() or DEFAULT_BROKER
        try:
            self.port = int(_load_settings().get("port", DEFAULT_PORT))
        except Exception:
            self.port = DEFAULT_PORT

        self.root.title("P2P 聊天")
        saved_geo = str(_load_settings().get("window_geometry", "") or "").strip()
        if saved_geo and re.fullmatch(r"\d{3,5}x\d{3,5}([+-]\d+[+-]\d+)?", saved_geo):
            self.root.geometry(saved_geo)
        else:
            self.root.geometry("1000x680")
        self.root.minsize(820, 560)
        self.root.configure(fg_color=C("app_bg"))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Web 端风格：隐藏系统标题栏，改用自绘标题栏（见 _build_titlebar）
        self._custom_titlebar = True
        self._maximized = False
        self._restore_geo = None
        try:
            self.root.overrideredirect(False)
            self.root.attributes("-toolwindow", True)  # 去系统标题栏，保留任务栏/缩放
        except Exception:
            self._custom_titlebar = False
        self.root.after(60, self._fix_taskbar_style)  # 窗口映射后修正任务栏样式
        self.root.after(400, self._fix_taskbar_style)  # 样式可能被系统延迟重置，多刷一次
        self.root.after(80, self._round_main_window)  # Win11 系统圆角

        self._build_ui()
        self._build_menu()

        # 恢复房间与会话
        self._rooms = _load_rooms() or _scan_group_rooms()
        self._contacts = _load_contacts()
        if not self._rooms:
            default = "默认房间"
            self._rooms = [default]
            _save_rooms(self._rooms)
        for room in self._rooms:
            self._ensure_group_session(room)
        for cid, name in _scan_dm_sessions():
            self._ensure_dm_session(cid, name)
        self._current = self._group_key(self._rooms[0])
        self._unread_total = sum(s.get("unread", 0) for s in self._sessions.values())

        self._apply_session_list()
        self.root.after(1, self._render_feed)  # 延迟渲染 feed，窗口先显示，启动更快
        self._start_auto_backup()
        self._set_status("未连接", "mute")
        self._show_system("🌸 欢迎使用 P2P 聊天！顶部输入房间名点「＋ 加入」即自动上线常驻；或点「💌 私聊」输入对方 ID 直接开聊。文字 / 图片 / 语音 / 文件都能发，同网段自动走局域网直连加速哦～")
        # 房间即群组：启动时存在历史房间则自动连接常驻上线
        if self._rooms or self.auto_connect:
            self.root.after(400, self._auto_connect_on_startup)

    # --------------------------- UI 构建 ---------------------------

    def _round_main_window(self):
        """Win11：主窗口系统圆角。"""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            _win11_round_corners(hwnd)
        except Exception:
            pass

    def _round_toplevel(self, win):
        """给浮窗（设置/表情/详情等）加 Win11 系统圆角。"""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
            _win11_round_corners(hwnd)
        except Exception:
            pass

    def _fix_taskbar_style(self):
        """无边框窗口任务栏修正：去 TOOLWINDOW 加 APPWINDOW，让任务栏图标正常显示。

        同时剥离系统标题栏样式（WS_CAPTION / WS_SYSMENU）：-toolwindow 样式一旦被
        移除（为恢复任务栏图标），系统自带的最小化/最大化/关闭按钮会重新出现，
        与自绘标题栏按钮叠加成两套。这里显式去掉系统标题栏按钮，只保留可缩放的
        粗边框（WS_THICKFRAME），缩放/最大化行为不受影响。"""
        if not getattr(self, "_custom_titlebar", False) or os.name != "nt":
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if not hwnd:
                hwnd = self.root.winfo_id()
            GWL_STYLE = -16
            GWL_EXSTYLE = -20
            # 去系统标题栏 + 系统菜单（消除与自绘按钮并存的系统按钮）
            st = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
            st = (st & ~0x00C00000)  # ~WS_CAPTION（含 WS_BORDER+WS_DLGFRAME）
            st = (st & ~0x00080000)  # ~WS_SYSMENU
            ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_STYLE, st)
            # 任务栏：去 TOOLWINDOW 加 APPWINDOW
            style = ctypes.windll.user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            style = (style & ~0x80) | 0x40000  # ~WS_EX_TOOLWINDOW | WS_EX_APPWINDOW
            ctypes.windll.user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def _build_titlebar(self, parent):
        """自绘标题栏（Web 端风格）：logo + 标题 + 弹性空间 + 最小化/最大化/关闭。"""
        tb = ctk.CTkFrame(parent, corner_radius=0, height=40, fg_color=C("panel"))
        tb.pack(fill="x")
        tb.pack_propagate(False)
        self._titlebar = tb
        ctk.CTkLabel(tb, text="🌸", font=(FONT, 15)).pack(side="left", padx=(12, 4))
        ctk.CTkLabel(tb, text="P2P 聊天", font=(FONT, 12, "bold"),
                     text_color=C("text")).pack(side="left")
        self._tb_title = tb.winfo_children()[-1]
        # 弹性空间（拖拽区）
        drag = ctk.CTkFrame(tb, fg_color="transparent")
        drag.pack(side="left", fill="both", expand=True)
        for w in (tb, drag):
            w.bind("<Button-1>", self._tb_press)
            w.bind("<B1-Motion>", self._tb_drag_move)
            w.bind("<Double-Button-1>", lambda e: self._tb_toggle_max())
        # 窗口控制按钮（Web 风格）
        def _ctl(text, cmd, hover):
            b = ctk.CTkButton(tb, text=text, width=42, height=40, corner_radius=0,
                              fg_color="transparent", hover_color=hover,
                              text_color=C("text_2"), font=(FONT, 13), command=cmd,
                              border_width=0, border_spacing=0)
            b.pack(side="right")
            return b
        self._btn_close = _ctl("✕", self._on_close, C("danger"))
        self._btn_max = _ctl("▢", self._tb_toggle_max, C("hover"))
        self._btn_min = _ctl("─", lambda: self.root.iconify(), C("hover"))
        for b in (self._btn_close, self._btn_max, self._btn_min):
            b.bind("<Button-1>", lambda e: "break")
        return tb

    def _tb_press(self, event):
        """标题栏按下：记录拖拽起点（最大化时禁止拖拽）。"""
        if self._maximized:
            self._tb_drag = None
            return
        self._tb_drag = (event.x_root - self.root.winfo_x(),
                         event.y_root - self.root.winfo_y())
        return "break"

    def _tb_drag_move(self, event):
        """标题栏拖拽移动窗口。"""
        if not getattr(self, "_tb_drag", None):
            return
        try:
            nx = event.x_root - self._tb_drag[0]
            ny = event.y_root - self._tb_drag[1]
            self.root.geometry(f"+{int(nx)}+{int(ny)}")
            # 窗口移动时关闭浮层（表情/提及面板留在原屏幕位置会很怪）
            try:
                if not getattr(self, "_emoji_locked", False):
                    self._close_emoji_panel()
            except Exception:
                pass
            try:
                self._close_mention_panel()
            except Exception:
                pass
        except Exception:
            pass
        return "break"

    def _tb_toggle_max(self):
        """最大化 / 还原（手动 geometry，兼容无边框窗口）。"""
        try:
            if self._maximized:
                self._maximized = False
                if self._restore_geo:
                    self.root.geometry(self._restore_geo)
                try:
                    self._btn_max.configure(text="▢")
                except Exception:
                    pass
            else:
                self._restore_geo = self.root.geometry()
                self._maximized = True
                sw = self.root.winfo_screenwidth()
                sh = self.root.winfo_screenheight()
                self.root.geometry(f"{sw}x{sh - 1}+0+0")
                try:
                    self._btn_max.configure(text="❐")
                except Exception:
                    pass
        except Exception:
            pass

    def _build_nav_rail(self, parent):
        """Discord 式图标导航栏：用户头像 + 房间圆形图标 + 添加/设置。"""
        rail = ctk.CTkFrame(parent, corner_radius=0, width=54, fg_color=C("panel"))
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        self._nav_rail = rail
        # 用户头像（点击个人资料卡）
        self.nav_avatar = ctk.CTkLabel(rail, text="", width=36, height=36,
                                       corner_radius=18, fg_color=C("input_bg"),
                                       cursor="hand2")
        self.nav_avatar.pack(pady=(10, 4))
        try:
            if self._avatar:
                from PIL import Image as _PILImage
                img = _PILImage.open(self._avatar)
                img.thumbnail((36, 36))
                from customtkinter import CTkImage as _CI
                ctk_img = _CI(light_image=img, dark_image=img, size=(36, 36))
                self._nav_avatar_img = ctk_img
                self.nav_avatar.configure(image=ctk_img, text="")
        except Exception:
            pass
        self.nav_avatar.bind("<Button-1>", lambda e: self._open_profile_card())
        # 头像延迟加载（启动不卡）：稍后再解码图片
        try:
            if self._avatar and os.path.isfile(self._avatar):
                self.root.after(400, self._render_nav_avatar)
        except Exception:
            pass
        ctk.CTkFrame(rail, width=30, height=2, corner_radius=1,
                     fg_color=C("hover")).pack(pady=(2, 6))
        # 房间图标列表（滚动）
        self.nav_rooms_frame = ctk.CTkScrollableFrame(
            rail, width=54, fg_color="transparent", corner_radius=0)
        self.nav_rooms_frame.pack(fill="both", expand=True)
        # 底部：➕ 加入房间 / ⚙ 设置
        ctk.CTkButton(rail, text="➕", width=36, height=36, corner_radius=18,
                      fg_color=C("input_bg"), hover_color=C("input_hover"),
                      text_color=C("text_2"), font=(FONT, 14),
                      command=self._add_room_from_input).pack(pady=(4, 2))
        ctk.CTkButton(rail, text="⚙", width=36, height=36, corner_radius=18,
                      fg_color=C("input_bg"), hover_color=C("input_hover"),
                      text_color=C("text_2"), font=(FONT, 14),
                      command=self._open_settings).pack(pady=(2, 10))
        self._refresh_nav_rail()
        return rail

    def _refresh_nav_rail(self):
        """刷新导航栏房间图标（选中态高亮胶囊）。

        指纹：当前房间 / 各房间未读数 / 置顶集变化时才重建，presence 连串
        更新或列表重绘时不重复销毁重建（导航栏是高频触发的轻量控件）。"""
        try:
            rail = getattr(self, "nav_rooms_frame", None)
            if rail is None:
                return
            try:
                fp = (self._current,
                      tuple((r, (self._sessions.get(self._group_key(r)) or {}).get("unread", 0),
                             self._is_pinned_session(self._group_key(r)))
                            for r in getattr(self, "_rooms", []) or []))
                if fp == getattr(self, "_nav_rail_fp", None):
                    return
                self._nav_rail_fp = fp
            except Exception:
                pass
            for w in rail.winfo_children():
                w.destroy()
            cur_room = None
            s = self._sessions.get(self._current)
            if s and s.get("kind") == "group":
                cur_room = s.get("room")
            for room in getattr(self, "_rooms", []) or []:
                ch = (str(room)[:1].upper() or "#")
                sel = (room == cur_room)
                key = self._group_key(room)
                s = self._sessions.get(key)
                unread = (s.get("unread") or 0) if s else 0
                pinned = self._is_pinned_session(key)
                irow = ctk.CTkFrame(rail, fg_color="transparent", height=44)
                irow.pack(fill="x", pady=1)
                # Discord 式选中指示条（左侧竖条）
                ctk.CTkFrame(irow, width=3, height=28, corner_radius=2,
                             fg_color=(C("text") if sel else "transparent")).pack(
                    side="left", padx=(2, 1), fill="y")
                # 置顶会话图标带 📌 角标（Discord 式徽标）
                icon = ("📌" if pinned else ch)
                b = ctk.CTkButton(
                    irow, text=icon, width=40, height=40, corner_radius=20,
                    fg_color=(C("accent") if sel else C("input_bg")),
                    hover_color=C("accent_hover"),
                    text_color=("#ffffff" if sel else C("text")),
                    font=(FONT, 13, "bold"),
                    command=lambda r=room: self._switch_to(self._group_key(r)))
                b.pack(side="left", padx=(2, 0))
                # 未读红点（Discord 式右上角小圆点）
                if unread:
                    dot = ctk.CTkLabel(irow, text="", width=8, height=8, corner_radius=4,
                                       fg_color=C("danger"))
                    dot.place(relx=1.0, x=-8, y=4, anchor="ne")
                # 悬停 tooltip：房间名（含未读数/置顶提示）
                tip = room + (f" · {unread} 未读" if unread else "")
                if pinned:
                    tip += " · 置顶"
                b.bind("<Enter>", lambda e, t=tip: self._set_status(t, "mute"))
                b.bind("<Leave>", lambda e: self._restore_status())
        except Exception:
            pass

    def _build_ui(self):
        # Web 端风格：自绘标题栏（无边框窗口）最顶
        if getattr(self, "_custom_titlebar", False):
            self._build_titlebar(self.root)
        # 顶部工具条（Web 式精简）：左侧连接状态，右侧主题/免打扰/隐身/更新
        # （昵称/ID/头像收进左上角个人资料卡；加入房间收进导航栏 ➕；私聊收进会话列表）
        top = ctk.CTkFrame(self.root, corner_radius=0, fg_color=C("panel"))
        top.pack(fill="x")

        self.status_var = ctk.StringVar(value="未连接")
        self.conn_lbl = ctk.CTkLabel(top, text="○ 未连接",
                                     anchor="w", font=(FONT, 11),
                                     text_color=C("text_mute"))
        self.conn_lbl.pack(side="left", padx=(14, 0), pady=10)

        self.update_btn = ctk.CTkButton(top, text="🔄", width=38, height=30, corner_radius=8,
                                          fg_color=C("input_bg"), hover_color=C("input_hover"),
                                          text_color=C("text_2"), font=(FONT, 13),
                                          command=self._manual_check_update)
        self.update_btn.pack(side="right", padx=(0, 10), pady=10)
        _tbtn = {"dark": "🌙", "light": "☀️", "anime": "🌸"}.get(self.appearance, "🌙")
        self.theme_btn = ctk.CTkButton(top, text=_tbtn,
                                        width=38, height=30, corner_radius=8,
                                        fg_color=C("input_bg"), hover_color=C("input_hover"),
                                        text_color=C("text_2"), font=(FONT, 13),
                                        command=self._toggle_theme)
        self.theme_btn.pack(side="right", padx=(0, 6), pady=10)
        self.pin_btn = ctk.CTkButton(top, text="📌", width=38, height=30, corner_radius=8,
                                       fg_color=(C("accent") if self._pin_var.get() else C("input_bg")),
                                       hover_color=C("input_hover"),
                                       text_color=C("text_2"), font=(FONT, 13),
                                       command=self._toggle_pin_window)
        self.pin_btn.pack(side="right", padx=(0, 6), pady=10)
        try:
            if self._pin_var.get():
                self.root.attributes("-topmost", True)
        except Exception:
            pass
        self.dnd_btn = ctk.CTkButton(top, text=("🔕" if self._dnd else "🔔"),
                                       width=38, height=30, corner_radius=8,
                                       fg_color=C("input_bg"), hover_color=C("input_hover"),
                                       text_color=C("text_2"), font=(FONT, 13),
                                       command=self._toggle_dnd)
        self.dnd_btn.pack(side="right", padx=(0, 6), pady=10)
        self.ghost_btn = ctk.CTkButton(top, text="🙂", width=38, height=30, corner_radius=8,
                                       fg_color=C("input_bg"), hover_color=C("input_hover"),
                                       text_color=C("text_2"), font=(FONT, 13),
                                       command=self._toggle_ghost)
        self.ghost_btn.pack(side="right", padx=(0, 6), pady=10)

        # 昵称变量保留（个人资料卡编辑后经此同步）
        self.nick_var = ctk.StringVar(value=self._profile_name)
        self.nick_var.trace_add("write", lambda *_: self._on_nick_changed())

        # 顶栏底部的品牌色条（二次元主题下是樱粉色装饰线）
        ctk.CTkFrame(self.root, corner_radius=0, height=3,
                     fg_color=C("accent")).pack(fill="x")

        # 状态栏（status_var 已在顶栏创建，两处共享同一文本）
        self.status_label = ctk.CTkLabel(self.root, textvariable=self.status_var,
                                         anchor="w", font=(FONT, 11),
                                         text_color=C("text_mute"), fg_color=C("app_bg"))
        self.status_label.pack(fill="x", padx=18, pady=(6, 4))

        # 主体
        body = ctk.CTkFrame(self.root, fg_color=C("app_bg"))
        body.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        # Web 端风格：最左图标导航栏（房间圆形图标）
        if getattr(self, "_custom_titlebar", False):
            self._build_nav_rail(body)

        # 左：会话列表
        left = ctk.CTkFrame(body, corner_radius=R(12), fg_color=C("panel"), width=248)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._schedule_session_refresh())
        srow = ctk.CTkFrame(left, fg_color="transparent")
        srow.pack(fill="x", padx=10, pady=(12, 6))
        ctk.CTkButton(srow, text="💌", width=34, height=32, corner_radius=R(8),
                      fg_color=C("input_bg"), hover_color=C("accent_hover"),
                      text_color=C("text"), font=(FONT, 13),
                      command=self._open_dm_dialog).pack(side="left", padx=(0, 6))
        self.search_entry = ctk.CTkEntry(srow, textvariable=self.search_var, height=32,
                                       corner_radius=R(8), border_width=0, fg_color=C("input_bg"),
                                       text_color=C("text"), placeholder_text_color=C("text_mute"),
                                       placeholder_text="搜索会话 / 成员",
                                       font=(FONT, 12))
        self.search_entry.pack(side="left", fill="x", expand=True)
        # 一键清空搜索（有内容时显示 ✕，Web 式快捷交互）
        self.search_clear_btn = ctk.CTkButton(srow, text="✕", width=24, height=24, corner_radius=12,
                                              fg_color=C("input_bg"), hover_color=C("input_hover"),
                                              text_color=C("text_2"), font=(FONT, 10),
                                              command=self._clear_session_search)
        self.search_clear_btn.pack(side="left", padx=(4, 0))
        self.session_frame = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.session_frame.pack(fill="both", expand=True, padx=6, pady=(0, 8))

        # 右：当前会话聊天区
        right = ctk.CTkFrame(body, corner_radius=12, fg_color=C("panel_2"))
        right.pack(side="left", fill="both", expand=True)

        self.title_row = ctk.CTkFrame(right, fg_color="transparent")
        self.title_row.pack(fill="x", padx=16, pady=(12, 2))
        # 两层标题排版（Web 式）：主标题 15px 粗体 + 副标题 10px 灰
        tstack = ctk.CTkFrame(self.title_row, fg_color="transparent")
        tstack.pack(side="left", fill="x", expand=True)
        self.chat_title = ctk.CTkLabel(tstack, text="群聊", font=(FONT, 15, "bold"),
                                       text_color=C("text"), anchor="w", cursor="hand2")
        self.chat_title.pack(fill="x")
        self.chat_title.bind("<Double-Button-1>", lambda e: self._copy_current_chat_id())
        self.chat_sub = ctk.CTkLabel(tstack, text="", font=(FONT, 10),
                                     text_color=C("text_mute"), anchor="w")
        self.chat_sub.pack(fill="x")
        self.members_btn = ctk.CTkButton(self.title_row, text="👥", width=32, height=26,
                                         corner_radius=R(8), font=(FONT, 12),
                                         fg_color=C("input_bg"), text_color=C("text_2"),
                                         hover_color=C("input_hover"), command=self._toggle_members)
        self.members_btn.pack(side="right")
        # ⋯ 更多菜单：全部已读 / 消息筛选 / @我汇总（收纳低频操作，标题行更清爽）
        self.next_unread_btn = ctk.CTkButton(self.title_row, text="↧ 未读", width=56, height=26,
                                              corner_radius=R(8), font=(FONT, 10, "bold"),
                                              fg_color=C("input_bg"), text_color=C("accent"),
                                              hover_color=C("input_hover"),
                                              command=self._goto_next_unread)
        self.next_unread_btn.pack(side="right", padx=(0, 6))
        self.more_btn = ctk.CTkButton(self.title_row, text="⋯", width=32, height=26,
                                      corner_radius=R(8), font=(FONT, 13, "bold"),
                                      fg_color=C("input_bg"), text_color=C("text_2"),
                                      hover_color=C("input_hover"),
                                      command=lambda: self._title_more_menu())
        self.more_btn.pack(side="right", padx=(0, 6))
        self.mention_btn = ctk.CTkButton(self.title_row, text="📢 @我", width=76, height=26,
                                         corner_radius=R(8), font=(FONT, 11),
                                         fg_color=C("input_bg"), text_color=C("text_2"),
                                         hover_color=C("input_hover"), command=self._open_mentions)
        self.mention_btn.pack(side="right", padx=(0, 6))
        self._refresh_mention_btn()

        # 成员列表面板（默认隐藏，点「👥 成员」开关）
        self.members_frame = ctk.CTkFrame(right, corner_radius=8, fg_color=C("panel"))

        # 会话内消息搜索栏（默认隐藏，Ctrl+F 打开）
        self.search_frame = ctk.CTkFrame(right, fg_color="transparent")
        self.search_entry = ctk.CTkEntry(self.search_frame, placeholder_text="搜索消息…",
                                         height=30, corner_radius=8, font=(FONT, 12),
                                         fg_color=C("input_bg"), text_color=C("text"),
                                         border_width=0)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(16, 6))
        self.search_entry.bind("<KeyRelease>", self._on_search_change)
        self.search_entry.bind("<Escape>", lambda e: self._close_search())
        self.search_entry.bind("<Return>", lambda e: self._jump_search_hit(1))
        self.search_entry.bind("<Shift-Return>", lambda e: self._jump_search_hit(-1))
        # 命中计数标签（_apply_search 更新）
        self.search_count_lbl = ctk.CTkLabel(self.search_frame, text="", font=(FONT, 9),
                                             text_color=C("accent"))
        self.search_count_lbl.pack(side="left", padx=(0, 6))
        ctk.CTkButton(self.search_frame, text="▲", width=28, height=28, corner_radius=8,
                      fg_color=C("input_bg"), text_color=C("text_2"),
                      hover_color=C("input_hover"), font=(FONT, 10),
                      command=lambda: self._jump_search_hit(-1)).pack(side="left", padx=(0, 4))
        ctk.CTkButton(self.search_frame, text="▼", width=28, height=28, corner_radius=8,
                      fg_color=C("input_bg"), text_color=C("text_2"),
                      hover_color=C("input_hover"), font=(FONT, 10),
                      command=lambda: self._jump_search_hit(1)).pack(side="left", padx=(0, 4))
        ctk.CTkButton(self.search_frame, text="✕", width=28, height=28, corner_radius=8,
                      fg_color=C("input_bg"), text_color=C("text_2"),
                      hover_color=C("input_hover"), font=(FONT, 12),
                      command=self._close_search).pack(side="left", padx=(0, 16))

        # 消息流背景与聊天卡片同色（CTkScrollableFrame 的 transparent 不会透出底色）
        self.feed = ctk.CTkScrollableFrame(right, fg_color=self._chat_bg_color(), corner_radius=0)
        self.feed.pack(fill="both", expand=True, padx=6, pady=2)
        # 滚到顶部自动加载更早历史（聊天软件标配交互）
        try:
            canvas = self.feed._parent_canvas
            canvas.bind("<MouseWheel>", self._on_feed_wheel)
            canvas.bind("<Button-4>", self._on_feed_wheel)
            canvas.bind("<Button-5>", self._on_feed_wheel)
        except Exception:
            pass
        if _DND_READY:
            try:
                self.feed.drop_target_register(DND_FILES)
                self.feed.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        # 引用回复提示栏（默认隐藏）
        self.reply_bar = ctk.CTkFrame(right, corner_radius=8, fg_color=C("warn_bg"))

        # 底部输入区
        # 输入区（Web 式重排）：左侧圆形工具钮 + 输入框 + 右侧圆形发送钮
        self._ibar = ctk.CTkFrame(right, corner_radius=R(14), fg_color=C("panel"))
        self._ibar.pack(fill="x", padx=8, pady=(4, 10))
        ibar = self._ibar

        # 左侧工具列：表情 / 文件 / 语音（圆形图标钮）
        tools = ctk.CTkFrame(ibar, fg_color="transparent")
        tools.pack(side="left", padx=(10, 4), pady=10)
        self.emoji_btn = ctk.CTkButton(tools, text="😊", width=38, height=38, corner_radius=19,
                                       fg_color=C("input_bg"), hover_color=C("input_hover"),
                                       text_color=C("text"), font=(FONT, 15),
                                       command=self._toggle_emoji_panel)
        self.emoji_btn.pack(pady=1)
        self.file_btn = ctk.CTkButton(tools, text="📎", width=38, height=38, corner_radius=19,
                                      fg_color=C("input_bg"), hover_color=C("input_hover"),
                                      text_color=C("text"), font=(FONT, 14),
                                      command=self._pick_file)
        self.file_btn.pack(pady=1)
        self.voice_btn = ctk.CTkButton(tools, text="🎤", width=38, height=38, corner_radius=19,
                                       fg_color=C("input_bg"), hover_color=C("input_hover"),
                                       text_color=C("text"), font=(FONT, 14))
        self.voice_btn.pack(pady=1)
        self.voice_btn.bind("<ButtonPress-1>", lambda e: self._start_voice())
        self.voice_btn.bind("<ButtonRelease-1>", lambda e: self._stop_voice())
        self.voice_btn.bind("<Button-3>", lambda e: self._cancel_voice_recording())
        self.voice_btn.bind("<Enter>", lambda e: self._set_status("按住 🎤 说话，松开发送（过短自动取消）", "mute"))
        self.voice_btn.bind("<Leave>", lambda e: self._restore_status())

        # 输入框（聚焦时显示主题色发光边框，QQ/Discord 式反馈）
        self.input_box = ctk.CTkTextbox(ibar, height=72, corner_radius=R(10), border_width=1,
                                        border_color=C("input_bg"),
                                        fg_color=C("input_bg"), text_color=C("text_mute"),
                                        font=(FONT, max(11, self._chat_font_size - 1)), wrap="word")
        self._input_focused = False
        self.input_box.pack(side="left", fill="both", expand=True, padx=(6, 10), pady=10)
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

        # 发送钮（圆形 accent）
        self.send_btn = ctk.CTkButton(ibar, text="➤", width=46, height=46, corner_radius=23,
                                      font=(FONT, 17, "bold"), fg_color=C("accent"),
                                      hover_color=C("accent_hover"), text_color="#ffffff",
                                      command=self._send_text)
        self.send_btn.pack(side="right", padx=(0, 12), pady=10)

        # Ctrl+F 打开会话内消息搜索；F3 / Shift+F3 在命中间跳转（QQ 式）
        self.root.bind("<Control-f>", self._open_search)
        self.root.bind("<Control-F>", self._open_search)
        self.root.bind("<F3>", lambda e: self._jump_search_hit(1))
        self.root.bind("<Shift-F3>", lambda e: self._jump_search_hit(-1))
        # 全局快捷键：Ctrl+Shift+F 全局搜索 / Ctrl+N 发起私聊 / Ctrl+R 重连 / Alt+↑↓ 切换会话
        self.root.bind("<Control-Shift-F>", self._open_global_search)
        self.root.bind("<Control-Shift-f>", self._open_global_search)
        self.root.bind("<Control-n>", self._shortcut_new_dm)
        self.root.bind("<Control-N>", self._shortcut_new_dm)
        self.root.bind("<Control-r>", self._shortcut_reconnect)
        self.root.bind("<Control-R>", self._shortcut_reconnect)
        self.root.bind("<Alt-Up>", lambda e: self._switch_session_dir(-1))
        self.root.bind("<Alt-Down>", lambda e: self._switch_session_dir(1))
        # 焦点跟踪：后台/最小化时收到新消息弹 Windows 通知
        self.root.bind("<FocusIn>", lambda e: setattr(self, "_window_focused", True))
        self.root.bind("<FocusOut>", lambda e: setattr(self, "_window_focused", False))

    # --------------------------- 菜单 / 环境检测 ---------------------------

    def _build_menu(self):
        try:
            if getattr(self, "_custom_titlebar", False):
                # Web 端风格无边框窗口：原生菜单栏不显示，功能入口整合进设置中心
                return
            menubar = tk.Menu(self.root)
            view_menu = tk.Menu(menubar, tearoff=0)
            view_menu.add_command(label="深色主题", command=lambda: self._set_theme("dark"))
            view_menu.add_command(label="浅色主题", command=lambda: self._set_theme("light"))
            view_menu.add_command(label="二次元主题（夜樱）", command=lambda: self._set_theme("anime"))
            self._pin_var = tk.BooleanVar(value=False)
            view_menu.add_checkbutton(label="窗口置顶", variable=self._pin_var,
                                      command=self._toggle_pin_window)
            menubar.add_cascade(label="视图", menu=view_menu)
            settings_menu = tk.Menu(menubar, tearoff=0)
            settings_menu.add_command(label="设置中心…", command=self._open_settings)
            settings_menu.add_separator()
            self._auto_conn_var = tk.BooleanVar(value=self.auto_connect)
            settings_menu.add_checkbutton(label="启动时自动连接", variable=self._auto_conn_var,
                                          command=self._toggle_auto_connect)
            self._sound_var = tk.BooleanVar(value=self.notify_sound)
            settings_menu.add_checkbutton(label="新消息提示音", variable=self._sound_var,
                                          command=self._toggle_notify_sound)
            self._popup_var = tk.BooleanVar(value=self.notify_popup)
            settings_menu.add_checkbutton(label="Windows 通知弹窗", variable=self._popup_var,
                                          command=self._toggle_notify_popup)
            settings_menu.add_separator()
            settings_menu.add_command(label="加密口令…", command=self._set_encrypt_pass)
            menubar.add_cascade(label="设置", menu=settings_menu)
            conn_menu = tk.Menu(menubar, tearoff=0)
            conn_menu.add_command(label="断开连接（离线）", command=self._disconnect)
            conn_menu.add_command(label="重新连接（上线）", command=self._ensure_connected)
            menubar.add_cascade(label="连接", menu=conn_menu)
            help_menu = tk.Menu(menubar, tearoff=0)
            help_menu.add_command(label="检查更新", command=self._manual_check_update)
            help_menu.add_command(label="环境检测 / 关于", command=self._show_about)
            help_menu.add_separator()
            help_menu.add_command(label="导出当前会话记录（TXT）", command=self._export_current_history)
            help_menu.add_command(label="导出当前会话记录（网页 HTML）", command=self._export_current_history_html)
            help_menu.add_command(label="我的名片…", command=self._show_my_card)
            help_menu.add_command(label="扫名片…", command=self._scan_card)
            help_menu.add_command(label="网络测速…", command=self._measure_network)
            help_menu.add_command(label="开放直连端口（需管理员）", command=self._open_firewall_ports)
            help_menu.add_command(label="备份全部数据…", command=self._backup_data)
            help_menu.add_command(label="从备份恢复…", command=self._restore_data)
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

    def _toggle_notify_popup(self):
        self.notify_popup = bool(self._popup_var.get())
        _update_settings("notify_popup", self.notify_popup)

    def _apply_setting(self, key, var, attr):
        val = bool(var.get())
        setattr(self, attr, val)
        _update_settings(key, val)

    def _apply_setting_dnd(self, var):
        self._dnd = bool(var.get())
        try:
            self.dnd_btn.configure(text="🔕" if self._dnd else "🔔")
        except Exception:
            pass

    def _chat_bg_color(self):
        """聊天背景色："" 默认（跟随主题）/ "deep" 深邃 / "sakura" 樱花。"""
        try:
            bg = getattr(self, "_chat_bg", "")
            if bg == "deep":
                return "#14161a" if _APPEARANCE in ("dark", "anime") else "#dde3ec"
            if bg == "sakura":
                return "#2a1d33" if _APPEARANCE in ("dark", "anime") else "#fdeef4"
        except Exception:
            pass
        return C("panel_2")

    def _apply_chat_bg(self, mode):
        """应用聊天背景：持久化 + 更新 feed 底色并重渲染。"""
        try:
            self._chat_bg = str(mode or "")
            _update_settings("chat_bg", self._chat_bg)
            try:
                self.feed.configure(fg_color=self._chat_bg_color())
            except Exception:
                pass
            self._render_feed()
            names = {"": "默认", "deep": "深邃", "sakura": "樱花"}
            self._set_status(f"聊天背景：{names.get(self._chat_bg, '默认')}", "ok")
        except Exception:
            pass

    def _apply_chat_font_size(self, size):
        """应用消息字号：持久化 + 更新输入框字体 + 重渲染当前会话。"""
        try:
            self._chat_font_size = max(10, min(20, int(size)))
            _update_settings("chat_font_size", self._chat_font_size)
            try:
                self.input_box.configure(font=(FONT, max(11, self._chat_font_size - 1)))
            except Exception:
                pass
            self._apply_session_list()
            self._render_feed()
            self._set_status(f"消息字号已设为 {self._chat_font_size}px", "ok")
        except Exception:
            pass

    def _pick_accent_color(self):
        """自定义强调色：颜色选择器选色 → 持久化 → 重建界面。"""
        global _ACCENT_OVERRIDE
        try:
            from tkinter import colorchooser
            cur = _ACCENT_OVERRIDE or C("accent")
            rgb, hexv = colorchooser.askcolor(color=cur, title="选择主题强调色")
            if not hexv:
                return
            _ACCENT_OVERRIDE = str(hexv).strip()
            _update_settings("accent_override", _ACCENT_OVERRIDE)
            self._rebuild_ui()
            self._set_status(f"主题色已设为 {_ACCENT_OVERRIDE}", "ok")
        except Exception:
            pass

    def _reset_accent_color(self):
        """恢复主题默认强调色。"""
        try:
            global _ACCENT_OVERRIDE
            _ACCENT_OVERRIDE = None
            _update_settings("accent_override", "")
            self._rebuild_ui()
            self._set_status("已恢复默认主题色", "ok")
        except Exception:
            pass

    def _apply_broker_setting(self, broker_var, port_var):
        """应用自定义服务器设置（自建局域网 broker 可大幅提速）。"""
        b = broker_var.get().strip()
        if b:
            self.broker = b
        try:
            self.port = int(port_var.get().strip())
        except Exception:
            self.port = DEFAULT_PORT
        _update_settings("broker", self.broker)
        _update_settings("port", self.port)
        self._set_status(f"服务器已设为 {self.broker}:{self.port}（重新连接后生效）", "ok")

    def _open_settings(self):
        """设置中心对话框：集中管理所有开关与选项。"""
        try:
            win = ctk.CTkToplevel(self.root)
            win.title("设置")
            win.geometry("440x720")
            win.resizable(True, True)
            win.minsize(430, 480)
            try:
                self._round_toplevel(win)
            except Exception:
                pass
            win.attributes("-topmost", True)
            ctk.CTkLabel(win, text="设置中心", font=(FONT, 15, "bold"),
                         text_color=C("text")).pack(pady=(12, 6))
            scroll = ctk.CTkScrollableFrame(win, fg_color="transparent", corner_radius=0)
            scroll.pack(fill="both", expand=True, padx=0, pady=(0, 0))

            ctk.CTkLabel(scroll, text="外观", font=(FONT, 11),
                         text_color=C("text_mute")).pack(anchor="w", padx=26, pady=(8, 2))
            _am_lbl = {"system": "跟随系统", "dark": "深色", "light": "浅色",
                       "anime": "二次元"}.get(self._appearance_mode, "跟随系统")
            _mode_var = ctk.StringVar(value=_am_lbl)
            ctk.CTkSegmentedButton(
                scroll, values=["跟随系统", "深色", "浅色", "二次元"], variable=_mode_var,
                font=(FONT, 11), height=30,
                selected_color=C("accent"), selected_hover_color=C("accent_hover"),
                unselected_color=C("input_bg"), unselected_hover_color=C("input_hover"),
                command=self._apply_appearance_mode).pack(fill="x", padx=26, pady=(2, 4))

            # 自定义强调色（QQ/Discord 式主题色定制，持久化）
            accent_row = ctk.CTkFrame(scroll, fg_color="transparent")
            accent_row.pack(fill="x", padx=26, pady=(6, 0))
            ctk.CTkButton(accent_row, text="🎨 自定义主题色…", width=130, height=26, corner_radius=8,
                          fg_color=(_ACCENT_OVERRIDE or C("accent")),
                          text_color=("#ffffff" if _ACCENT_OVERRIDE else "#ffffff"),
                          hover_color=C("accent_hover"), font=(FONT, 10),
                          command=self._pick_accent_color).pack(side="left")
            ctk.CTkButton(accent_row, text="恢复默认", width=76, height=26, corner_radius=8,
                          fg_color=C("input_bg"), text_color=C("text_2"),
                          hover_color=C("input_hover"), font=(FONT, 10),
                          command=self._reset_accent_color).pack(side="left", padx=(6, 0))

            # 消息字号（小/中/大，持久化；改动立即重渲染当前会话）
            ctk.CTkLabel(scroll, text="消息字号", font=(FONT, 10),
                         text_color=C("text_mute")).pack(anchor="w", padx=26, pady=(6, 0))
            _fs_var = ctk.StringVar(value={12: "小", 13: "中", 15: "大"}.get(self._chat_font_size, "中"))
            ctk.CTkSegmentedButton(
                scroll, values=["小", "中", "大"], variable=_fs_var,
                font=(FONT, 11), height=28,
                selected_color=C("accent"), selected_hover_color=C("accent_hover"),
                unselected_color=C("input_bg"), unselected_hover_color=C("input_hover"),
                command=lambda v: self._apply_chat_font_size(
                    {"小": 12, "中": 13, "大": 15}.get(v, 13))).pack(fill="x", padx=26, pady=(2, 4))

            # 聊天背景（默认/深邃/樱花，持久化）
            ctk.CTkLabel(scroll, text="聊天背景", font=(FONT, 10),
                         text_color=C("text_mute")).pack(anchor="w", padx=26, pady=(6, 0))
            _bg_var = ctk.StringVar(value={"": "默认", "deep": "深邃", "sakura": "樱花"}.get(self._chat_bg, "默认"))
            ctk.CTkSegmentedButton(
                scroll, values=["默认", "深邃", "樱花"], variable=_bg_var,
                font=(FONT, 11), height=28,
                selected_color=C("accent"), selected_hover_color=C("accent_hover"),
                unselected_color=C("input_bg"), unselected_hover_color=C("input_hover"),
                command=lambda v: self._apply_chat_bg(
                    {"默认": "", "深邃": "deep", "樱花": "sakura"}.get(v, ""))).pack(fill="x", padx=26, pady=(2, 4))

            # 开机自启动（Windows 注册表 Run 键）
            autostart_var = tk.BooleanVar(value=self._is_autostart())
            ctk.CTkCheckBox(scroll, text="开机自动启动", variable=autostart_var,
                            command=lambda: self._set_autostart(autostart_var.get()),
                            font=(FONT, 12), text_color=C("text")).pack(anchor="w", padx=26, pady=5)

            popup_var = tk.BooleanVar(value=self.notify_popup)
            ctk.CTkCheckBox(scroll, text="Windows 通知弹窗", variable=popup_var,
                            command=lambda: self._apply_setting("notify_popup", popup_var, "notify_popup"),
                            font=(FONT, 12), text_color=C("text")).pack(anchor="w", padx=26, pady=5)
            sound_var = tk.BooleanVar(value=self.notify_sound)
            ctk.CTkCheckBox(scroll, text="新消息提示音", variable=sound_var,
                            command=lambda: self._apply_setting("notify_sound", sound_var, "notify_sound"),
                            font=(FONT, 12), text_color=C("text")).pack(anchor="w", padx=26, pady=5)
            auto_var = tk.BooleanVar(value=self.auto_connect)
            ctk.CTkCheckBox(scroll, text="启动时自动连接", variable=auto_var,
                            command=lambda: self._apply_setting("auto_connect", auto_var, "auto_connect"),
                            font=(FONT, 12), text_color=C("text")).pack(anchor="w", padx=26, pady=5)
            enter_var = tk.BooleanVar(value=self.enter_sends)
            ctk.CTkCheckBox(scroll, text="回车键发送（关闭后为 QQ 风格 Ctrl+回车发送、回车换行）", variable=enter_var,
                            command=lambda: self._apply_setting("enter_sends", enter_var, "enter_sends"),
                            font=(FONT, 12), text_color=C("text")).pack(anchor="w", padx=26, pady=5)
            dnd_var = tk.BooleanVar(value=self._dnd)
            ctk.CTkCheckBox(scroll, text="免打扰（静音通知+提示音）", variable=dnd_var,
                            command=lambda: self._apply_setting_dnd(dnd_var),
                            font=(FONT, 12), text_color=C("text")).pack(anchor="w", padx=26, pady=5)

            ctk.CTkLabel(scroll, text="服务器（同一局域网可自建 broker 提速）",
                         font=(FONT, 10), text_color=C("text_mute")).pack(anchor="w", padx=26, pady=(10, 0))
            broker_var = ctk.StringVar(value=self.broker)
            ctk.CTkEntry(scroll, textvariable=broker_var, width=240, height=30, corner_radius=8,
                         border_width=0, fg_color=C("input_bg"), text_color=C("text"),
                         placeholder_text="服务器地址（如 192.168.1.10）", font=(FONT, 11)).pack(pady=4)
            port_var = ctk.StringVar(value=str(self.port))
            ctk.CTkEntry(scroll, textvariable=port_var, width=120, height=30, corner_radius=8,
                         border_width=0, fg_color=C("input_bg"), text_color=C("text"),
                         placeholder_text="端口", font=(FONT, 11)).pack(pady=2)
            ctk.CTkButton(scroll, text="应用服务器设置", width=140, height=28, corner_radius=8,
                          fg_color=C("input_bg"), text_color=C("text_2"), hover_color=C("input_hover"),
                          font=(FONT, 11),
                          command=lambda: self._apply_broker_setting(broker_var, port_var)).pack(pady=4)

            _enc_txt = ("🔒 修改加密口令…" if self.encrypt_pass else "🔓 设置加密口令…")
            ctk.CTkButton(scroll, text=_enc_txt, height=32, corner_radius=8,
                          fg_color=C("input_bg"), text_color=C("text"), hover_color=C("input_hover"),
                          font=(FONT, 12), command=self._set_encrypt_pass).pack(fill="x", padx=26, pady=6)

            # 更多功能（原菜单栏入口，Web 端风格整合到设置中心）
            ctk.CTkLabel(scroll, text="更多功能", font=(FONT, 11),
                         text_color=C("text_mute")).pack(anchor="w", padx=26, pady=(12, 2))
            for _txt, _cmd in (
                ("📤 导出当前会话记录（TXT）", self._export_current_history),
                ("🌐 导出当前会话记录（网页 HTML）", self._export_current_history_html),
                ("💾 备份全部数据…", self._backup_data),
                ("📥 从备份恢复…", self._restore_data),
                ("🛡 开放直连端口（需管理员）", self._open_firewall_ports),
                ("📶 网络测速…", self._measure_network),
                ("🔄 检查更新", self._manual_check_update),
            ):
                ctk.CTkButton(scroll, text=_txt, height=28, corner_radius=8,
                              fg_color=C("input_bg"), text_color=C("text_2"),
                              hover_color=C("input_hover"), font=(FONT, 11),
                              command=_cmd).pack(fill="x", padx=26, pady=1)

            ctk.CTkLabel(scroll, text=f"P2P 聊天 · v{APP_VERSION}", font=(FONT, 10),
                         text_color=C("text_mute")).pack(pady=(10, 14))
            win.bind("<Escape>", lambda e: win.destroy())
        except Exception:
            pass

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

    def _is_autostart(self):
        """是否已注册开机自启动（Windows Run 键）。"""
        if os.name != "nt":
            return False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                try:
                    winreg.QueryValueEx(k, "P2PChat")
                    return True
                except FileNotFoundError:
                    return False
        except Exception:
            return False

    def _set_autostart(self, on):
        """设置/取消开机自启动（Windows 注册表 Run 键，指向当前程序）。"""
        try:
            if os.name != "nt":
                self._set_status("仅 Windows 支持开机自启动", "err")
                return
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE)
            exe = sys.executable
            if getattr(sys, "frozen", False):
                target = f'"{exe}"'
            else:
                target = f'"{exe}" "{os.path.abspath(__file__)}"'
            if on:
                winreg.SetValueEx(key, "P2PChat", 0, winreg.REG_SZ, target)
                self._set_status("已开启开机自启动", "ok")
            else:
                try:
                    winreg.DeleteValue(key, "P2PChat")
                except FileNotFoundError:
                    pass
                self._set_status("已关闭开机自启动", "mute")
            winreg.CloseKey(key)
        except Exception:
            self._set_status("设置开机自启动失败", "err")

    def _auto_connect_on_startup(self):
        if not (self.backend and self.backend.running):
            try:
                self._ensure_connected()
            except Exception:
                pass

    # --------------------------- 主题切换 ---------------------------

    def _toggle_theme(self):
        order = ["dark", "light", "anime"]
        try:
            nxt = order[(order.index(self.appearance) + 1) % len(order)]
        except Exception:
            nxt = "dark"
        self._set_theme(nxt)

    def _toggle_pin_window(self):
        """窗口置顶开关：置顶后窗口始终在最前（多任务/看视频时方便）。"""
        try:
            on = bool(self._pin_var.get())
            self.root.attributes("-topmost", on)
            _update_settings("pin_window", on)
            try:
                self.pin_btn.configure(fg_color=(C("accent") if on else C("input_bg")))
            except Exception:
                pass
            self._set_status("已置顶窗口（始终在最前）" if on else "已取消窗口置顶", "ok")
        except Exception:
            pass

    def _toggle_dnd(self):
        """免打扰开关：一键静音通知 + 提示音（状态持久化）。"""
        self._dnd = not self._dnd
        _update_settings("dnd", self._dnd)
        try:
            self.dnd_btn.configure(text="🔕" if self._dnd else "🔔",
                                   fg_color=(C("accent") if self._dnd else C("input_bg")))
        except Exception:
            pass
        self._set_status("已开启免打扰" if self._dnd else "已关闭免打扰", "ok")

    def _toggle_ghost(self):
        """隐身模式开关：不广播在线状态（对方看到你离线），但仍可收发消息（状态持久化）。"""
        self._ghost = not self._ghost
        _update_settings("ghost", self._ghost)
        try:
            if self.backend:
                self.backend.hidden = self._ghost
                self.backend._publish_presence()
        except Exception:
            pass
        try:
            self.ghost_btn.configure(
                text=("🙈" if self._ghost else "🙂"),
                fg_color=(C("accent") if self._ghost else C("input_bg")))
        except Exception:
            pass
        self._set_status(
            "已开启隐身：对方看不到你在线（仍可收发消息）" if self._ghost else "已关闭隐身：恢复在线状态广播",
            "accent" if self._ghost else "ok")

    def _apply_appearance_mode(self, label):
        """设置中心「外观」三选：跟随系统 / 深色 / 浅色。"""
        try:
            mapping = {"跟随系统": "system", "深色": "dark", "浅色": "light", "二次元": "anime"}
            mode = mapping.get(str(label), str(label))
            if mode == "system":
                self._appearance_mode = "system"
                _update_settings("appearance_mode", "system")
                self._set_theme(_detect_system_theme())
                self._appearance_mode = "system"  # 保持跟随状态（_set_theme 会改掉）
            else:
                self._appearance_mode = mode
                _update_settings("appearance_mode", mode)
                self._set_theme(mode)
        except Exception:
            pass

    def _set_theme(self, mode):
        if mode not in THEMES or mode == self.appearance:
            return
        self.appearance = mode
        self._appearance_mode = mode
        _update_settings("appearance", mode)
        _update_settings("appearance_mode", mode)
        try:
            self._set_status("正在切换主题…", "mute")
        except Exception:
            pass
        # 先更新配色并重建界面，最后再切换 ctk 外观模式。ctk.set_appearance_mode 会触发
        # Windows 标题栏重绘（withdraw/deiconify）并 after(1) 恢复焦点；若在重建前调用，
        # 焦点恢复会指向已销毁的旧控件而崩溃。
        set_appearance(mode, apply_ctk=False)
        self._rebuild_ui()
        # ctk 的 set_appearance_mode 会遍历全部控件重配外观（约 1s）——
        # 控件刚按新主题重建过，纯属重复；异步执行不阻塞切换首屏
        try:
            self.root.after(
                30, lambda: ctk.set_appearance_mode(
                    "dark" if mode in ("dark", "anime") else "light"))
        except Exception:
            pass
        # ctk 外观切换会 withdraw/deiconify 触发标题栏重绘，可能重置窗口样式
        # （系统标题栏按钮重新出现）；延迟重新剥离一次，保持自绘标题栏唯一
        try:
            self.root.after(120, self._fix_taskbar_style)
            self.root.after(600, self._fix_taskbar_style)
        except Exception:
            pass

    def _rebuild_ui(self):
        # 主题切换：销毁并重建全部控件（会话/历史状态保存在 self 里，不丢）
        # 先关闭所有浮窗（表情面板 / @面板 / 设置 / 个人资料等独立 Toplevel），避免主题重建后残留“闪现空窗”。
        try:
            for w in list(self.root.winfo_children()):
                try:
                    if w.winfo_class() == "Toplevel" and w is not self.root:
                        w.destroy()
                except Exception:
                    pass
        except Exception:
            pass
        self._close_emoji_panel()
        self._close_mention_panel()
        try:
            self._destroy_hover_bar()
        except Exception:
            pass
        for attr in ("_emoji_win", "_mention_win", "_emoji_cv", "_emoji_items",
                     "_emoji_tab_btns", "_emoji_root_bind"):
            try:
                setattr(self, attr, None)
            except Exception:
                pass
        nick = ""
        try:
            nick = self.nick_var.get().strip()
        except Exception:
            nick = self._profile_name
        # 主题切换不再整窗消失重载：盖一层新主题底色的遮罩，销毁重建在遮罩后进行，
        # 完成后掀开——窗口保持可见、任务栏不闪、位置尺寸不丢，观感是"瞬间换装"。
        overlay = None
        try:
            overlay = tk.Frame(self.root, bg=C("app_bg"), highlightthickness=0)
            overlay.place(x=0, y=0, relwidth=1, relheight=1)
            overlay.lift()
            tk.Label(overlay, text="🌸 正在换装…", bg=C("app_bg"), fg=C("text_mute"),
                     font=(FONT, 12)).place(relx=0.5, rely=0.5, anchor="center")
        except Exception:
            overlay = None
        # 先释放图片引用（CTkImage 持有 Tk 图片对象，先清引用再销毁控件能显著减少释放时间）
        self._images = []
        self._thumb_cache = {}
        self._my_avatar_ctk = None
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        for w in list(self.root.winfo_children()):
            if overlay is not None and w is overlay:
                continue  # 遮罩保留到最后
            try:
                w.destroy()
            except Exception:
                pass
        self._last_list_fp = None
        self.root.configure(fg_color=C("app_bg"))
        self._build_ui()
        self._build_menu()
        self.nick_var.set(nick or self._profile_name or "未命名")
        self._apply_session_list()
        # 主题切换期间限制首屏渲染量（历史保留，切完后自动补全），切换更快
        # （CTk 控件创建约 30ms/条，60 条要 2s；20 条压到 0.7s 以内，剩余延迟补全）
        _saved = self.RENDER_MAX
        self.RENDER_MAX = min(_saved, 20)
        try:
            self._render_feed()
        finally:
            self.RENDER_MAX = _saved
        self._update_window_title()
        # 掀开遮罩（重建完成后）
        if overlay is not None:
            try:
                overlay.destroy()
            except Exception:
                pass
        # 切换完成后延迟补全剩余消息（后台分批，不卡切换）
        try:
            self.root.after(250, self._render_feed)
        except Exception:
            pass

    # --------------------------- 搜索防抖 ---------------------------

    def _clear_session_search(self):
        """一键清空会话/成员搜索框。"""
        try:
            self.search_var.set("")
            self._apply_session_list()
            self._set_status("已清空搜索", "mute")
        except Exception:
            pass

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

    def _render_nav_avatar(self):
        """导航栏头像渲染（延迟调用，启动不卡）。"""
        try:
            nav = getattr(self, "nav_avatar", None)
            if nav is None or not self._avatar:
                return
            img = _load_ctk_image(self._avatar, 36, 36)
            if img is not None:
                self._images.append(img)
                nav.configure(image=img, text="")
        except Exception:
            pass

    def _render_top_avatar(self):
        """渲染头像到导航栏（Web 端风格：头像在左上角导航栏）。"""
        try:
            nav = getattr(self, "nav_avatar", None)
            if nav is None:
                return
            img = _load_ctk_image(self._avatar, 36, 36) if self._avatar else None
            if img is not None:
                self._images.append(img)
                nav.configure(image=img, text="")
            else:
                name = self._profile_name or "?"
                nav.configure(image=None, text=name[:1].upper())
        except Exception:
            pass

    def _open_profile_card(self):
        """个人资料卡：头像 / 昵称 / ID / 个性签名。"""
        try:
            win = ctk.CTkToplevel(self.root)
            win.title("个人资料")
            win.geometry("380x360")
            win.resizable(False, False)
            win.attributes("-topmost", True)
            ctk.CTkLabel(win, text="个人资料", font=(FONT, 15, "bold"),
                         text_color=C("text")).pack(pady=(16, 8))

            av = ctk.CTkLabel(win, text="", width=80, height=80, corner_radius=40,
                              fg_color=C("input_bg"), cursor="hand2")
            av.pack(pady=4)
            self._avatar_preview_label(av)
            av.bind("<Button-1>", lambda e: self._change_avatar())
            ctk.CTkLabel(win, text="点击更换头像", font=(FONT, 9),
                         text_color=C("text_mute")).pack()

            name_var = ctk.StringVar(value=self._profile_name or self.nick_var.get())
            ctk.CTkEntry(win, textvariable=name_var, width=240, height=32, corner_radius=8,
                         border_width=0, fg_color=C("input_bg"), text_color=C("text"),
                         font=(FONT, 12)).pack(pady=(12, 4))
            ctk.CTkLabel(win, text=f"ID：{self.cid}", font=(FONT, 10),
                         text_color=C("text_mute")).pack(pady=(0, 2))
            ctk.CTkButton(win, text="📋 复制 ID", width=120, height=26, corner_radius=8,
                          fg_color=C("input_bg"), text_color=C("text_2"), hover_color=C("input_hover"),
                          font=(FONT, 11), command=lambda: self._copy_to_clipboard(self.cid)).pack(pady=2)

            bio_var = ctk.StringVar(value=self._bio or "")
            ctk.CTkEntry(win, textvariable=bio_var, width=240, height=32, corner_radius=8,
                         border_width=0, fg_color=C("input_bg"), text_color=C("text"),
                         placeholder_text="个性签名（选填）", font=(FONT, 12)).pack(pady=(10, 4))

            def _save_all():
                self._profile_name = name_var.get().strip() or "未命名"
                self._bio = bio_var.get().strip()
                try:
                    self.nick_var.set(self._profile_name)
                except Exception:
                    pass
                _save_profile(self._profile_name, self._avatar, self._bio)
                self._set_status("资料已保存", "ok")
                win.destroy()

            ctk.CTkButton(win, text="保存", width=120, height=32, corner_radius=8,
                          fg_color=C("accent"), hover_color=C("accent_hover"),
                          font=(FONT, 12, "bold"), command=_save_all).pack(pady=10)
            win.bind("<Escape>", lambda e: win.destroy())
        except Exception:
            pass

    def _avatar_preview_label(self, lbl):
        """把当前头像（或首字母）显示到个人资料卡。"""
        try:
            if self._avatar and os.path.isfile(self._avatar) and _HAS_PIL:
                from PIL import Image
                img = Image.open(self._avatar).convert("RGB")
                img = img.resize((80, 80))
                ctk_img = CTkImage(light_image=img, dark_image=img, size=(80, 80))
                self._images.append(ctk_img)
                lbl.configure(image=ctk_img, text="")
            else:
                name = self._profile_name or "未命名"
                lbl.configure(image=None, text=name[:1].upper(),
                              fg_color=_name_color(name), text_color="#ffffff",
                              font=(FONT, 26, "bold"))
        except Exception:
            pass

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
            _save_profile(self._profile_name, self._avatar, self._bio)
            self._render_top_avatar()
            self._set_status("头像已更新", "ok")
        else:
            messagebox.showerror("头像", "读取该图片失败，请换一张试试。")

    def _copy_current_chat_id(self):
        """双击聊天标题：复制当前会话的标识（群聊复制房间名、私聊复制对方 ID）。"""
        try:
            s = self._sessions.get(self._current)
            if s is None:
                return
            if s["kind"] == "group":
                val = s.get("room", "")
                tip = "已复制房间名：" + str(val)
            else:
                val = s.get("cid", "")
                tip = "已复制对方 ID：" + str(val)
            if val:
                self.root.clipboard_clear()
                self.root.clipboard_append(str(val))
                self._set_status(tip, "ok")
        except Exception:
            pass

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
        """从 GitHub release 数据里挑出安装包（P2PChat-Setup.exe）的下载地址。

        优先精确匹配 setup exe；没有则退而求其次取任意
        .exe 资产；再没有则用 html 推导直链（releases/download/）。
        """
        try:
            assets = data.get("assets") or []
            for a in assets:
                n = str(a.get("name", "")).lower()
                if n.endswith(".exe") and "setup" in n:
                    u = str(a.get("browser_download_url", "")).strip()
                    if u:
                        return u
            for a in assets:
                n = str(a.get("name", "")).lower()
                if n.endswith(".exe"):
                    u = str(a.get("browser_download_url", "")).strip()
                    if u:
                        return u
            # 推导直链：html_url 中的 releases/tag/{tag} -> releases/download/{tag}/P2PChat-Setup.exe
            html = str(data.get("html_url", "") or "")
            if "/releases/tag/" in html:
                tag = html.rsplit("/", 1)[-1].strip()
                if tag:
                    return (f"https://github.com/{UPDATE_OWNER}/{UPDATE_REPO}"
                            f"/releases/download/{tag}/P2PChat-Setup.exe")
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
        # 切换会话时停止语音播放，避免进度条残留
        try:
            if getattr(self, "_playing_voice", None):
                self._stop_voice_play(self._playing_voice,
                                      btn=getattr(self, "_playing_btn", None),
                                      bar=getattr(self, "_playing_bar", None))
        except Exception:
            pass
        # 切换会话：清掉引用回复栏（避免残留上一条会话的回复）
        try:
            if self._reply_to is not None:
                self._cancel_reply()
        except Exception:
            pass
        # 记录离开旧会话的时间，用于回来后画“新消息”分隔线
        old = self._sessions.get(self._current)
        if old is not None and old.get("key") != key:
            old["last_seen_ts"] = time.time()
            # 记录离开时的滚动位置，切回来恢复（不会总跳到底部）
            try:
                canvas = self.feed._parent_canvas
                frac = canvas.yview()[0]
                old["scroll_frac"] = frac
            except Exception:
                pass
            # 草稿保存：切换会话时保留当前输入内容
            try:
                draft = self.input_box.get("1.0", "end").strip()
                old["draft"] = draft if draft and not self._hint_active else ""
            except Exception:
                pass
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
        self._unread_total -= s.get("unread", 0)
        if self._unread_total < 0:
            self._unread_total = 0
        s["unread"] = 0
        s["@me"] = False
        self._update_chat_title()
        # 恢复草稿（如有）
        try:
            draft = (s.get("draft") or "").strip()
            if draft and self._hint_active:
                self.input_box.delete("1.0", "end")
                self.input_box.configure(text_color=C("text"))
                self._hint_active = False
            if draft:
                self.input_box.delete("1.0", "end")
                self.input_box.insert("1.0", draft)
                self.input_box.configure(text_color=C("text"))
                self._hint_active = False
                try:
                    self._update_send_btn_state()  # 恢复草稿后发送按钮点亮
                except Exception:
                    pass
        except Exception:
            pass
        if not (s.get("draft") or "").strip():
            self._reset_input_hint()
        self._render_feed()
        self._apply_session_list()
        try:
            self._refresh_nav_rail()
        except Exception:
            pass
        self._update_window_title()

    def _open_dm_dialog(self):
        """独立私聊入口：输入对方 ID 和昵称即可开聊，无需加入同一群聊。"""
        try:
            win = ctk.CTkToplevel(self.root)
            win.title("发起私聊")
            win.geometry("380x240")
            win.resizable(False, False)
            try:
                self._round_toplevel(win)
            except Exception:
                pass
            win.attributes("-topmost", True)
            ctk.CTkLabel(win, text="💌 发起私聊", font=(FONT, 15, "bold"),
                         text_color=C("text")).pack(pady=(18, 4))
            ctk.CTkLabel(win, text="输入对方的用户 ID（可让对方点「📋 复制ID」发给你）",
                         font=(FONT, 10), text_color=C("text_mute")).pack()
            cid_var = ctk.StringVar()
            ctk.CTkEntry(win, textvariable=cid_var, width=260, height=32, corner_radius=8,
                         border_width=0, fg_color=C("input_bg"), text_color=C("text"),
                         placeholder_text="对方 ID", font=(FONT, 12)).pack(pady=(8, 2))
            name_var = ctk.StringVar()
            ctk.CTkEntry(win, textvariable=name_var, width=260, height=32, corner_radius=8,
                         border_width=0, fg_color=C("input_bg"), text_color=C("text"),
                         placeholder_text="对方昵称（可选，如 ID 已知可留空）", font=(FONT, 12)).pack(pady=4)
            # 在线成员快捷选择（点一下自动填入 ID，QQ 式快速开聊）
            _peers_list = [(cid, p["name"]) for cid, p in self._peers.items()
                           if cid != self.cid][:6]
            if _peers_list:
                chip_row = ctk.CTkFrame(win, fg_color="transparent")
                chip_row.pack(fill="x", padx=16, pady=(2, 0))
                ctk.CTkLabel(chip_row, text="在线成员：", text_color=C("text_mute"),
                             font=(FONT, 9)).pack(side="left")
                for _cid, _nm in _peers_list:
                    ctk.CTkButton(chip_row, text=str(_nm)[:6], width=0, height=22, corner_radius=11,
                                  fg_color=C("input_bg"), text_color=C("text"),
                                  hover_color=C("accent_hover"), font=(FONT, 9),
                                  command=lambda c=_cid: _fill(c)).pack(side="left", padx=2)

            def _fill(cid):
                cid_var.set(cid)
                try:
                    p = self._peers.get(cid)
                    if p and not name_var.get().strip():
                        name_var.set(str(p.get("name", ""))[:20])
                except Exception:
                    pass
                name_var.icursor("end")

            def _go():
                cid = cid_var.get().strip()
                if not cid:
                    self._set_status("请先输入对方 ID", "err")
                    return
                if cid == self.cid:
                    self._set_status("不能和自己私聊", "err")
                    return
                nm = name_var.get().strip()
                try:
                    win.destroy()
                except Exception:
                    pass
                s = self._ensure_dm_session(cid, nm or None)
                s["online"] = cid in self._peers
                self._switch_to(s["key"])
                self._set_status("已进入私聊（发送即投递到对方收件箱）", "ok")
                self._ensure_connected()

            ctk.CTkButton(win, text="开始私聊", width=150, height=34, corner_radius=8,
                          font=(FONT, 12, "bold"), fg_color=C("accent"),
                          hover_color=C("accent_hover"), command=_go).pack(pady=(10, 4))
            win.bind("<Return>", lambda e: _go())
            win.bind("<Escape>", lambda e: win.destroy())
            win.grab_set()
            win.after(80, lambda: win.focus_force())
        except Exception:
            pass

    def _start_dm(self, cid, name):
        s = self._ensure_dm_session(cid, name)
        s["online"] = True
        self._switch_to(s["key"])

    def _is_contact(self, cid):
        return any(c["cid"] == cid for c in self._contacts)

    def _toggle_contact(self, cid, name):
        """收藏 / 取消收藏一个联系人。"""
        name = (name or "").strip() or "？"
        for c in self._contacts:
            if c["cid"] == cid:
                self._contacts.remove(c)
                _save_contacts(self._contacts)
                self._last_list_fp = None
                self._apply_session_list()
                self._set_status("已取消收藏", "ok")
                return
        self._contacts.append({"cid": cid, "name": name})
        _save_contacts(self._contacts)
        self._last_list_fp = None
        self._apply_session_list()
        self._set_status("已收藏联系人", "ok")

    def _update_chat_title(self):
        s = self._sessions.get(self._current)
        if s is None:
            self.chat_title.configure(text="未选择会话")
            return
        if s["kind"] == "group":
            n = sum(1 for p in self._peers.values() if s["room"] in (p.get("rooms") or []))
            self.chat_title.configure(text=f"🌸 {s['name']}")
            try:
                self.chat_sub.configure(text=f"群聊 · {n} 人在线")
            except Exception:
                pass
        else:
            online = s.get("online") or s.get("cid") in self._peers
            dot = "🟢 在线" if online else "⚪ 离线"
            self.chat_title.configure(text=s["name"])
            try:
                self.chat_sub.configure(text=f"私聊 · {dot}")
            except Exception:
                pass

    def _title_more_menu(self):
        """标题行 ⋯ 更多菜单：全部已读 / 消息筛选。"""
        try:
            m = tk.Menu(self.root, tearoff=0, font=(FONT, 10))
            m.add_command(label="✔ 全部已读", command=self._mark_all_read)
            m.add_separator()
            fimg = "✓ 只看图片" if self._feed_filter == "img" else "只看图片"
            ffile = "✓ 只看文件" if self._feed_filter == "file" else "只看文件"
            m.add_command(label=fimg, command=lambda: self._toggle_feed_filter("img"))
            m.add_command(label=ffile, command=lambda: self._toggle_feed_filter("file"))
            m.tk_popup(self.more_btn.winfo_rootx(),
                       self.more_btn.winfo_rooty() + self.more_btn.winfo_height())
        finally:
            try:
                m.grab_release()
            except Exception:
                pass

    def _toggle_members(self):
        """展开 / 收起当前会话的成员列表。"""
        self._members_visible = not self._members_visible
        if self._members_visible:
            try:
                self.members_frame.pack(fill="x", padx=8, pady=(0, 2), after=self.title_row)
            except Exception:
                pass
        else:
            try:
                self.members_frame.pack_forget()
            except Exception:
                pass
        self._refresh_members()

    def _refresh_members(self, debounce=False):
        """刷新成员列表内容（在线成员 / 私聊对方状态）。
        debounce=True 时合并连串更新（peers 批量变化用）；默认立即刷新。"""
        if not self._members_visible:
            return
        if debounce:
            if getattr(self, "_members_after", None) is not None:
                try:
                    self.root.after_cancel(self._members_after)
                except Exception:
                    pass
            self._members_after = self.root.after(120, self._apply_members_ui)
            return
        try:
            self._apply_members_ui()
        except Exception:
            pass

    def _apply_members_ui(self):
        try:
            self._members_after = None
        except Exception:
            pass
        if not getattr(self, "_members_visible", False):
            return
        try:
            for w in self.members_frame.winfo_children():
                w.destroy()
        except Exception:
            return
        s = self._sessions.get(self._current)
        if s is None:
            return
        if s["kind"] == "group":
            room = s["room"]
            others = []
            for cid, p in self._peers.items():
                if cid == self.cid:
                    continue
                n = str(p.get("name", "")).strip()
                if n and room in (p.get("rooms") or []):
                    others.append((cid, n))
            total = len(others) + 1  # 含自己
            ctk.CTkLabel(self.members_frame, text=f"在线成员 · {total} 人（点击私聊）",
                         text_color=C("text_mute"), font=(FONT, 10)).pack(anchor="w", padx=10, pady=(6, 2))
            if others:
                flow = ctk.CTkFrame(self.members_frame, fg_color="transparent")
                flow.pack(fill="x", padx=8, pady=(0, 8))
                for cid, nm in others:
                    ctk.CTkButton(flow, text=nm, height=26, corner_radius=13,
                                  fg_color=C("input_bg"), text_color=C("text"),
                                  hover_color=C("input_hover"), font=(FONT, 11),
                                  command=lambda c=cid, nm=nm: self._start_dm(c, nm)).pack(side="left", padx=2, pady=2)
            else:
                ctk.CTkLabel(self.members_frame, text="（暂无其他成员）",
                             text_color=C("text_mute"), font=(FONT, 11)).pack(anchor="w", padx=10, pady=(0, 8))
        else:
            online = s.get("cid") in self._peers
            ctk.CTkLabel(self.members_frame,
                         text=f"{s.get('name', '对方')}：{'🟢 在线' if online else '⚪ 离线'}",
                         text_color=C("text"), font=(FONT, 11)).pack(anchor="w", padx=10, pady=8)

    # --------------------------- 左侧会话/成员列表 ---------------------------

    def _list_fingerprint(self):
        """会话列表关键状态指纹：无变化时跳过重建。"""
        try:
            fp = [self._current, (self.search_var.get() or ""), len(self._rooms),
                  len(self._sessions), len(self._peers),
                  tuple(sorted((k, s.get("unread", 0), bool(s.get("online")), bool(s.get("@me")),
                                ((s.get("messages") or [None])[-1].get("mid") if s.get("messages") else None),
                                ((s.get("messages") or [None])[-1].get("ts") if s.get("messages") else None))
                               for k, s in self._sessions.items()))]
            return tuple(fp)
        except Exception:
            return None

    def _session_last_ts(self, s):
        """会话最后一条消息的时间戳（QQ 式按最近活跃排序用）。

        消息始终按追加顺序存储（_append_message 尾部追加、历史加载按时间序），
        因此最后一条即最新：直接取尾部 O(1)，避免每次列表重建全量 max() 扫描。"""
        msgs = s.get("messages") or []
        if not msgs:
            return 0.0
        try:
            return float(msgs[-1].get("ts") or 0)
        except Exception:
            return 0.0

    def _apply_session_list(self):
        fp = self._list_fingerprint()
        if fp is not None and fp == self._last_list_fp:
            return  # 无实质变化，跳过重建
        self._last_list_fp = fp
        for w in self.session_frame.winfo_children():
            w.destroy()
        kw = (self.search_var.get() or "").strip().lower()
        try:
            if kw:
                self.search_clear_btn.pack(side="left", padx=(4, 0))
            else:
                self.search_clear_btn.pack_forget()
        except Exception:
            pass
        dm_cids = {s["cid"] for s in self._sessions.values() if s["kind"] == "dm" and s["cid"]}

        groups = sorted([r for r in self._rooms if (not kw) or kw in r.lower()],
                         key=lambda r: (0 if self._is_pinned_session(self._group_key(r)) else 1,
                                        -self._session_last_ts(self._sessions.get(self._group_key(r)) or {}),
                                        r))
        dms = sorted([s for s in self._sessions.values()
                      if s["kind"] == "dm" and ((not kw) or kw in s["name"].lower())],
                     key=lambda s: (0 if self._is_pinned_session(s["key"]) else 1,
                                    -self._session_last_ts(s),
                                    -(s.get("unread") or 0), s["name"]))
        online_others = [(cid, p["name"]) for cid, p in self._peers.items()
                         if cid != self.cid and cid not in dm_cids
                         and ((not kw) or kw in p["name"].lower())]

        favorites = [c for c in self._contacts if c["cid"] not in dm_cids
                     and ((not kw) or kw in c["name"].lower())]
        total = len(groups) + len(dms) + len(online_others) + len(favorites)
        if favorites:
            self._add_section_header("★ 收藏", "fav")
            if not self._is_group_collapsed("fav"):
                for c in favorites:
                    self._add_contact_item(c)
        if groups:
            self._add_section_header("群聊", "groups")
            if not self._is_group_collapsed("groups"):
                for r in groups:
                    self._add_group_item(r)
        if dms:
            self._add_section_header("私聊", "dms")
            if not self._is_group_collapsed("dms"):
                for s in dms:
                    self._add_dm_item(s)
        if online_others:
            self._add_section_header("在线成员", "online")
            if not self._is_group_collapsed("online"):
                for cid, name in sorted(online_others, key=lambda x: x[1]):
                    self._add_member_item(cid, name)

        if total == 0 and not kw:
            ctk.CTkLabel(self.session_frame, text="（暂无会话，先在上方加入房间）",
                         text_color=C("text_mute"), font=(FONT, 10)).pack(anchor="w", padx=10, pady=8)
        elif total == 0:
            ctk.CTkLabel(self.session_frame, text="（无匹配）",
                         text_color=C("text_mute"), font=(FONT, 10)).pack(anchor="w", padx=10, pady=8)
        self._refresh_mention_btn()
        try:
            self._refresh_nav_rail()  # Web 端风格：导航栏房间图标联动
        except Exception:
            pass

    def _collapsed_groups(self):
        """会话分组折叠状态（懒加载自设置；读写持久化，重启保留）。"""
        if not hasattr(self, "_collapsed"):
            try:
                self._collapsed = set(_load_settings().get("collapsed_groups", []) or [])
            except Exception:
                self._collapsed = set()
        return self._collapsed

    def _toggle_group_collapse(self, key):
        """点击分组标题：折叠 / 展开该分组（状态持久化）。"""
        c = self._collapsed_groups()
        if key in c:
            c.discard(key)
        else:
            c.add(key)
        try:
            _update_settings("collapsed_groups", sorted(c))
        except Exception:
            pass
        self._apply_session_list()

    def _is_group_collapsed(self, key):
        return key in self._collapsed_groups()

    def _add_section_header(self, text, group_key=""):
        """分组标题（可点击折叠/展开，带装饰线）。"""
        collapsed = bool(group_key) and self._is_group_collapsed(group_key)
        arrow = ("▶ " if collapsed else "▼ ") if group_key else ""
        row = ctk.CTkFrame(self.session_frame, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(10, 2))
        ctk.CTkFrame(row, width=10, height=2, corner_radius=1,
                     fg_color=C("accent")).pack(side="left", padx=(0, 6))
        lbl = ctk.CTkLabel(row, text=arrow + text,
                           text_color=C("section"),
                           font=(FONT, 10, "bold"), anchor="w",
                           cursor=("hand2" if group_key else ""))
        lbl.pack(side="left")
        ctk.CTkFrame(row, height=1, corner_radius=0,
                     fg_color=C("hover")).pack(side="left", fill="x", expand=True, padx=(8, 0))
        if group_key:
            for w in (row, lbl):
                w.bind("<Button-1>", lambda e, k=group_key: self._toggle_group_collapse(k))

    def _session_avatar(self, parent, name, is_group=False, size=30):
        """会话列表圆形首字母头像（Discord/QQ 风格）：群聊显示 #，私聊显示昵称首字母。
        二次元主题下带樱粉描边。"""
        ch = ("#" if is_group else (str(name or "?")[:1].upper() or "?"))
        fg = C("input_bg") if is_group else _name_color(name or "?")
        bw, bc = ((2, C("accent")) if _APPEARANCE == "anime" else (0, None))
        av = ctk.CTkLabel(parent, text=ch, width=size, height=size, corner_radius=size // 2,
                          fg_color=fg, text_color="#ffffff", font=(FONT, 12 if is_group else 11, "bold"),
                          border_width=bw, border_color=bc,
                          cursor="hand2")
        av.pack(side="left", padx=(8, 2), pady=6)
        return av

    def _unread_badge(self, parent, n):
        txt = str(n) if n < 100 else "99+"
        w = max(20, 16 + (len(txt) - 1) * 8)
        return ctk.CTkLabel(parent, text=txt, width=w, height=20, corner_radius=R(10),
                            fg_color=C("danger"), text_color="#ffffff", font=(FONT, 10, "bold"))

    def _bind_row_drop(self, row, key):
        """拖拽文件到会话行：直接发送到该会话（Web 式快捷操作）。"""
        if not _DND_READY:
            return
        try:
            row.drop_target_register(DND_FILES)
            row.dnd_bind("<<Drop>>", lambda e, k=key: self._drop_to_session(e, k))
        except Exception:
            pass

    def _drop_to_session(self, event, key):
        try:
            paths = self.root.tk.splitlist(event.data)
            if not paths:
                return
            if key != self._current:
                self._switch_to(key)
            for p in paths:
                p = str(p).strip()
                if p and os.path.isfile(p):
                    self._do_send_file(p)
            return "break"
        except Exception:
            return None

    def _bind_row_hover(self, row, selected):
        """会话行悬停高亮（平滑渐变，QQ/Discord 式；选中行不变色）。"""
        if selected:
            return
        _anim = {"after": None, "step": 0, "steps": 0}

        def _cancel():
            if _anim["after"] is not None:
                try:
                    row.after_cancel(_anim["after"])
                except Exception:
                    pass
                _anim["after"] = None

        def _hex_mix(a, b, t):
            """两 hex 色线性插值；支持 transparent 特判。"""
            def _p(c):
                c = str(c).strip()
                if not c or c.lower() in ("transparent", "none", ""):
                    return None
                try:
                    c = c.lstrip("#")
                    if len(c) == 3:
                        c = "".join(ch * 2 for ch in c)
                    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
                except Exception:
                    return None
            pa, pb = _p(a), _p(b)
            if pa is None and pb is None:
                return "transparent"
            if pa is None:
                pa = (0, 0, 0)
            if pb is None:
                pb = (0, 0, 0)
            t = max(0.0, min(1.0, t))
            rgb = tuple(int(round(pa[i] + (pb[i] - pa[i]) * t)) for i in range(3))
            return "#%02x%02x%02x" % rgb

        def _start(step, target):
            _cancel()
            _anim["steps"] = 8
            _anim["step"] = step

            def _tick():
                _anim["after"] = None
                s = _anim["step"]
                if s > _anim["steps"]:
                    try:
                        row.configure(fg_color=target)
                    except Exception:
                        pass
                    return
                t = s / float(_anim["steps"])
                try:
                    cur = row.cget("fg_color")
                except Exception:
                    cur = "transparent"
                try:
                    row.configure(fg_color=_hex_mix(cur, target, 0.35))
                except Exception:
                    pass
                _anim["step"] = s + 1
                try:
                    _anim["after"] = row.after(16, _tick)
                except Exception:
                    _anim["after"] = None

            _tick()

        def on_enter(_e):
            if row.winfo_exists():
                _start(1, C("hover"))

        def on_leave(_e):
            _cancel()
            try:
                row.configure(fg_color="transparent")
            except Exception:
                pass

        row.bind("<Enter>", on_enter, add="+")
        row.bind("<Leave>", on_leave, add="+")

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

    def _session_time(self, s):
        """会话最后一条消息的短时间（今天显示 HH:MM，更早显示 月-日）。"""
        msgs = s.get("messages") or []
        if not msgs:
            return ""
        ts = msgs[-1].get("ts")
        if not ts:
            return ""
        try:
            lt = time.localtime(float(ts))
            now = time.localtime()
            if lt[:3] == now[:3]:
                return time.strftime("%H:%M", lt)
            return time.strftime("%m-%d", lt)
        except Exception:
            return ""

    def _add_group_item(self, room):
        key = self._group_key(room)
        selected = key == self._current
        s = self._sessions.get(key)
        unread = (s.get("unread") or 0) if s else 0
        n = sum(1 for p in self._peers.values() if room in (p.get("rooms") or []))
        preview = self._session_preview(s) if s else ""
        row = ctk.CTkFrame(self.session_frame, corner_radius=R(8),
                           fg_color=(C("selected_bg") if selected else "transparent"))
        row.pack(fill="x", pady=1)
        # 选中指示条（Discord 式左侧竖条）
        ctk.CTkFrame(row, width=3, height=30, corner_radius=2,
                     fg_color=(C("accent") if selected else "transparent")).pack(side="left", padx=(2, 0), fill="y", pady=4)
        av = self._session_avatar(row, room, is_group=True)
        muted = self._is_muted(key)
        mid = ctk.CTkFrame(row, fg_color="transparent")
        mid.pack(side="left", fill="x", expand=True, padx=(2, 4), pady=4)
        room_label = ("🔕 " + room) if muted else room
        nrow = ctk.CTkFrame(mid, fg_color="transparent")
        nrow.pack(fill="x")
        ctk.CTkLabel(nrow, text=room_label + (f"  {n}" if n else ""), anchor="w",
                     text_color=(C("selected_text") if selected else C("text")),
                     font=(FONT, max(11, self._chat_font_size - 1), "bold" if selected else "normal"), cursor="hand2").pack(side="left")
        stime = self._session_time(s) if s else ""
        if stime:
            tl = ctk.CTkLabel(nrow, text=stime, anchor="e",
                              text_color=C("text_mute"), font=(FONT, 9))
            tl.pack(side="right")
            # 悬停显示完整日期时间（QQ 式）
            try:
                _ft = _fmt_full_time((s.get("messages") or [{}])[-1].get("ts"))
                if _ft:
                    tl.bind("<Enter>", lambda e, t=_ft: self._set_status(t, "mute"))
                    tl.bind("<Leave>", lambda e: self._restore_status())
            except Exception:
                pass
        if s and (s.get("draft") or "").strip():
            ctk.CTkLabel(row, text="📝", text_color=C("text_mute"),
                         font=(FONT, 10), cursor="hand2").pack(side="right", padx=(0, 6))
        if preview:
            ctk.CTkLabel(mid, text=preview, anchor="w", text_color=C("text_mute"),
                         font=(FONT, max(9, self._chat_font_size - 3)), cursor="hand2").pack(anchor="w")
        if unread:
            self._unread_badge(row, unread).pack(side="right", padx=(0, 6))
        if s and s.get("@me"):
            ctk.CTkLabel(row, text="@", text_color=C("accent"),
                         font=(FONT, 11, "bold"), cursor="hand2").pack(side="right", padx=(0, 6))
        cross = ctk.CTkLabel(row, text="✕", width=24, text_color=C("text_mute"), cursor="hand2")
        cross.pack(side="right", padx=(0, 6))
        for w in [row, av] + list(mid.winfo_children()):
            w.bind("<Button-1>", lambda e, k=key: self._switch_to(k))
            w.bind("<Button-3>", lambda e, k=key: self._group_context_menu(e, k))
        cross.bind("<Button-1>", lambda e, r=room: self._remove_room(r))
        self._bind_row_hover(row, selected)
        self._bind_row_drop(row, key)

    def _add_dm_item(self, s):
        key = s["key"]
        selected = key == self._current
        unread = s.get("unread") or 0
        preview = self._session_preview(s)
        row = ctk.CTkFrame(self.session_frame, corner_radius=R(8),
                           fg_color=(C("selected_bg") if selected else "transparent"))
        row.pack(fill="x", pady=1)
        ctk.CTkFrame(row, width=3, height=30, corner_radius=2,
                     fg_color=(C("accent") if selected else "transparent")).pack(side="left", padx=(2, 0), fill="y", pady=4)
        av = self._session_avatar(row, s["name"])
        mid = ctk.CTkFrame(row, fg_color="transparent")
        mid.pack(side="left", fill="x", expand=True, padx=(2, 4), pady=4)
        nrow = ctk.CTkFrame(mid, fg_color="transparent")
        nrow.pack(fill="x")
        ctk.CTkLabel(nrow, text=(("🔕 " + s["name"]) if self._is_muted(key) else s["name"]), anchor="w",
                     text_color=(C("selected_text") if selected else C("text")),
                     font=(FONT, max(11, self._chat_font_size - 1), "bold" if (selected or unread) else "normal"),
                     cursor="hand2").pack(side="left")
        stime = self._session_time(s)
        if stime:
            tl = ctk.CTkLabel(nrow, text=stime, anchor="e",
                              text_color=C("text_mute"), font=(FONT, 9))
            tl.pack(side="right")
            # 悬停显示完整日期时间（QQ 式）
            try:
                _ft = _fmt_full_time((s.get("messages") or [{}])[-1].get("ts"))
                if _ft:
                    tl.bind("<Enter>", lambda e, t=_ft: self._set_status(t, "mute"))
                    tl.bind("<Leave>", lambda e: self._restore_status())
            except Exception:
                pass
        if preview:
            ctk.CTkLabel(mid, text=preview, anchor="w", text_color=C("text_mute"),
                         font=(FONT, max(9, self._chat_font_size - 3)), cursor="hand2").pack(anchor="w")
        if (s.get("draft") or "").strip():
            ctk.CTkLabel(row, text="📝", text_color=C("text_mute"),
                         font=(FONT, 10), cursor="hand2").pack(side="right", padx=(0, 4))
        dot = ctk.CTkLabel(row, text="●" if s["online"] else "○", width=14, anchor="e",
                           text_color=(C("online") if s["online"] else C("text_mute")),
                           font=(FONT, 9, "bold"), cursor="hand2")
        dot.pack(side="right", padx=(0, 4))
        if unread:
            self._unread_badge(row, unread).pack(side="right", padx=(0, 10))
        if s.get("@me"):
            ctk.CTkLabel(row, text="@", text_color=C("accent"),
                         font=(FONT, 11, "bold"), cursor="hand2").pack(side="right", padx=(0, 6))
        for w in [row, av, dot] + list(mid.winfo_children()):
            w.bind("<Button-1>", lambda e, k=key: self._switch_to(k))
            w.bind("<Button-3>", lambda e, s=s: self._dm_context_menu(e, s))
        self._bind_row_hover(row, selected)
        self._bind_row_drop(row, key)

    def _add_contact_item(self, c):
        """收藏联系人条目：点击发起私聊，右键取消收藏。"""
        cid, name = c["cid"], c["name"]
        row = ctk.CTkFrame(self.session_frame, corner_radius=8, fg_color="transparent")
        row.pack(fill="x", pady=1)
        av = self._session_avatar(row, name)
        star = ctk.CTkLabel(row, text="★", width=16, anchor="w", text_color=C("accent"),
                            font=(FONT, 11, "bold"), cursor="hand2")
        star.pack(side="left", padx=(2, 0), pady=8)
        lbl = ctk.CTkLabel(row, text=name, anchor="w", text_color=C("text"),
                           font=(FONT, 12), cursor="hand2")
        lbl.pack(side="left", fill="x", expand=True, pady=7)
        for w in (row, lbl, star, av):
            w.bind("<Button-1>", lambda e, c=cid, nm=name: self._start_dm(c, nm))
        row.bind("<Button-3>", lambda e, c=cid, nm=name: self._contact_context_menu(e, c, nm))
        self._bind_row_hover(row, False)

    def _contact_context_menu(self, event, cid, name):
        try:
            menu = tk.Menu(self.root, tearoff=0, font=(FONT, 10))
            menu.add_command(label="取消收藏", command=lambda: self._toggle_contact(cid, name))
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _add_member_item(self, cid, name):
        row = ctk.CTkFrame(self.session_frame, corner_radius=8, fg_color="transparent")
        row.pack(fill="x", pady=1)
        av = self._session_avatar(row, name)
        lbl = ctk.CTkLabel(row, text=name, anchor="w", text_color=C("text"),
                           font=(FONT, 12), cursor="hand2")
        lbl.pack(side="left", fill="x", expand=True, pady=7)
        for w in (row, lbl, av):
            w.bind("<Button-1>", lambda e, c=cid, nm=name: self._start_dm(c, nm))
        self._bind_row_hover(row, False)

    # --------------------------- 消息追加 / 持久化 ---------------------------

    def _flash_window(self):
        """新消息到达时闪烁任务栏图标（仅 Windows，窗口获得焦点后自动停止）。
        3 秒节流：后台连串新消息只闪一次，避免高频调用。"""
        if os.name != "nt":
            return
        try:
            now = time.time()
            if now - getattr(self, "_last_flash_ts", 0.0) < 3.0:
                return
            self._last_flash_ts = now
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
        if self._dnd:
            return
        if s.get("key") in self._muted:
            return
        if not self.notify_popup:
            return
        if s["key"] == self._current and self._window_focused:
            return
        try:
            title = str(s.get("name") or "新消息")
            who = str(name or "对方")
            preview = (text or "").strip().replace("\n", " ")[:80] or "（图片/文件）"
            if self._mentions_me(text):
                title = f"📢 @我 · {title}"
            _notify_windows(f"{title} · {who}", preview)
        except Exception:
            pass

    def _append_message(self, key, name, text, mine, img_path=None, file_path=None, system=False, mid=None, preview_tid=None, reply=None, voice=False):
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
        if reply:
            msg["reply"] = reply
        if voice:
            msg["voice"] = True
        s["messages"].append(msg)
        if len(s["messages"]) > self.FEED_MAX:
            # 被挤掉的旧消息同步移出索引（避免索引无限膨胀）
            for _rm in s["messages"][:-self.FEED_MAX]:
                _rid = _rm.get("mid")
                if _rid:
                    self._mid_index.get(key, {}).pop(str(_rid), None)
            s["messages"] = s["messages"][-self.FEED_MAX:]
        if mid:
            self._mid_index.setdefault(key, {})[str(mid)] = msg
        self._schedule_session_save(s)
        self._maybe_notify(s, name, text, mine, system)
        if not mine and not system and self._mentions_me(text):
            s["@me"] = True
            if key != self._current:
                self._schedule_session_list()
        if not mine and self.notify_sound and not system and not self._dnd \
                and s.get("key") not in self._muted:
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
                if voice and file_path and os.path.isfile(file_path):
                    self._add_voice_bubble(name, file_path, mine, msg.get("ts"), show_head, mid=mid)
                elif img_path and os.path.isfile(img_path):
                    self._add_image_bubble(name, img_path, mine, msg.get("ts"), show_head, mid=mid)
                else:
                    self._add_bubble(name, text, mine, msg.get("ts"), show_head,
                                     file_path=file_path, mid=mid)
        else:
            s["unread"] = s.get("unread", 0) + 1
            self._unread_total += 1
            self._schedule_session_list()
            self._update_window_title()
            if not mine and not self._dnd and s.get("key") not in self._muted:
                self._flash_window()

    def _rebuild_mid_index(self, s):
        """整表重建某会话的 mid 索引（历史重载 / 恢复后调用）。"""
        try:
            key = s.get("key")
            if key is None:
                return
            idx = {}
            for m in s.get("messages", []):
                mid = m.get("mid")
                if mid:
                    idx[str(mid)] = m
            if idx:
                self._mid_index[key] = idx
            else:
                self._mid_index.pop(key, None)
        except Exception:
            pass

    def _find_msg(self, key, mid):
        """按 mid 反查消息（索引 O(1)，miss 时线性回退并回填索引）。"""
        if mid is None:
            return None
        try:
            m = self._mid_index.get(key, {}).get(str(mid))
            if m is not None:
                return m
        except Exception:
            pass
        s = self._sessions.get(key)
        if s is not None:
            for m in s.get("messages", []):
                if m.get("mid") == mid:
                    try:
                        self._mid_index.setdefault(key, {})[str(mid)] = m
                    except Exception:
                        pass
                    return m
        return None

    def _schedule_session_save(self, s):
        """消息追加节流保存：100ms 内多次追加只合并写一次盘（
        避免每条消息都全量序列化+写文件，批量消息/历史加载时性能大幅提升）。"""
        try:
            key = s.get("key") or id(s)
            if getattr(self, "_session_save_after", None) is not None:
                try:
                    self.root.after_cancel(self._session_save_after)
                except Exception:
                    pass
            self._session_save_after = self.root.after(150, lambda k=key: self._flush_session_save(k))
        except Exception:
            self._save_session(s)

    def _flush_session_save(self, key):
        try:
            self._session_save_after = None
            s = self._sessions.get(key)
            if s is not None:
                self._save_session(s)
        except Exception:
            pass

    def _save_session(self, s):
        if s["kind"] == "group":
            _save_group_history(s["room"], s["messages"])
        else:
            _save_dm_history(s["cid"], s["name"], s["messages"])

    # --------------------------- 输入框提示 ---------------------------

    def _reset_input_hint(self):
        self.input_box.delete("1.0", "end")
        self.input_box.insert("1.0", self._input_hint_text())
        self.input_box.configure(text_color=C("text_mute"))
        self._hint_active = True

    def _input_hint_text(self):
        """动态输入提示：按当前会话类型生成（Discord 式）。"""
        try:
            s = self._sessions.get(self._current)
            if s and s.get("kind") == "dm":
                return f"发消息给 {s.get('name', '对方')}，回车发送；图片 / 文件可直接拖入"
            if s and s.get("kind") == "group":
                return f"发消息到 #{s.get('room', '')}，回车发送；图片 / 文件可直接拖入"
        except Exception:
            pass
        return HINT

    def _on_input_focus_in(self, event):
        if self._hint_active:
            self.input_box.delete("1.0", "end")
            self.input_box.configure(text_color=C("text"))
            self._hint_active = False
        # 聚焦高亮：工具钮与发送钮点亮（Web 式反馈）+ 输入框主题色发光边框
        try:
            self.emoji_btn.configure(fg_color=C("selected_bg"))
            self.send_btn.configure(fg_color=C("accent_hover"))
            self.input_box.configure(border_color=C("accent"))
        except Exception:
            pass

    def _on_input_focus_out(self, event):
        try:
            self.emoji_btn.configure(fg_color=C("input_bg"))
            self.send_btn.configure(fg_color=C("accent"))
            self.input_box.configure(border_color=C("input_bg"))
        except Exception:
            pass
        if not self.input_box.get("1.0", "end").strip():
            self._reset_input_hint()

    # --------------------------- 房间 / 连接 ---------------------------

    def _add_room_from_input(self):
        """弹出加入房间对话框（Web 式：独立小窗输入房间名）。"""
        try:
            win = ctk.CTkToplevel(self.root)
            win.title("加入房间")
            win.geometry("360x200")
            win.resizable(False, False)
            try:
                self._round_toplevel(win)
            except Exception:
                pass
            win.attributes("-topmost", True)
            ctk.CTkLabel(win, text="➕ 加入房间", font=(FONT, 14, "bold"),
                         text_color=C("text")).pack(pady=(16, 4))
            ctk.CTkLabel(win, text="输入房间名，双方加入同一房间即可互聊",
                         font=(FONT, 10), text_color=C("text_mute")).pack()
            var = ctk.StringVar(value="")
            ent = ctk.CTkEntry(win, textvariable=var, width=240, height=32,
                               corner_radius=8, border_width=0, fg_color=C("input_bg"),
                               text_color=C("text"), placeholder_text="房间名",
                               font=(FONT, 12))
            ent.pack(pady=(12, 6))
            ent.focus_set()
            # 已有房间快捷加入（chip）
            if self._rooms:
                chip_row = ctk.CTkFrame(win, fg_color="transparent")
                chip_row.pack(fill="x", padx=16, pady=(0, 2))
                ctk.CTkLabel(chip_row, text="已有房间：", text_color=C("text_mute"),
                             font=(FONT, 9)).pack(side="left")
                for _rm in self._rooms[-6:]:
                    ctk.CTkButton(chip_row, text=str(_rm)[:8], width=0, height=22, corner_radius=11,
                                  fg_color=C("input_bg"), text_color=C("text"),
                                  hover_color=C("accent_hover"), font=(FONT, 9),
                                  command=lambda r=_rm: _go_room(r)).pack(side="left", padx=2)

            def _go_room(room):
                win.destroy()
                self._add_room(room)

            def _go():
                room = var.get().strip() or "默认房间"
                win.destroy()
                self._add_room(room)

            ctk.CTkButton(win, text="加入", width=140, height=32, corner_radius=8,
                          font=(FONT, 12, "bold"), fg_color=C("accent"),
                          hover_color=C("accent_hover"), command=_go).pack(pady=4)
            win.bind("<Return>", lambda e: _go())
            win.bind("<Escape>", lambda e: win.destroy())
        except Exception:
            self._add_room("默认房间")

    def _refresh_room_combo(self):
        # 顶栏房间下拉已移除（导航栏 ➕ 对话框取代）；保留方法兼容旧调用
        pass

    def _add_room(self, room):
        room = (room or "").strip()
        if not room:
            return
        if room in self._rooms:
            self._switch_to(self._group_key(room))
            self._ensure_connected()
            return
        self._rooms.append(room)
        _save_rooms(self._rooms)
        self._ensure_group_session(room)
        if self.backend and self.backend.running:
            self.backend.add_room(room)
        self._apply_session_list()
        self._switch_to(self._group_key(room))
        # 房间即群组：加入即自动连接常驻上线（后台加速通道同样由后端自动拉起）
        self._ensure_connected()

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

    def _backup_data(self):
        """把全部聊天记录 / 设置 / 资料打包成 zip 备份。"""
        try:
            import zipfile
            from tkinter import filedialog
            path = filedialog.asksaveasfilename(title="备份数据", defaultextension=".zip",
                                                initialfile="p2p_backup.zip",
                                                filetypes=[("ZIP 压缩包", "*.zip")])
            if not path:
                return
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                for fn in sorted(os.listdir(DATA_DIR)):
                    fp = os.path.join(DATA_DIR, fn)
                    if os.path.isfile(fp):
                        zf.write(fp, "p2pdata/" + fn)
            self._set_status(f"已备份到 {path}", "ok")
        except Exception:
            self._set_status("备份失败", "err")

    def _auto_backup_loop(self):
        """自动定期备份：每小时把会话记录/设置打包到 history/auto_backup/，保留最近 24 份。"""
        while self._auto_backup_stop.is_set() is False:
            try:
                import zipfile
                import glob
                bk_dir = os.path.join(DATA_DIR, "..", "history", "auto_backup")
                try:
                    os.makedirs(bk_dir, exist_ok=True)
                except Exception:
                    pass
                ts = time.strftime("%Y%m%d_%H%M")
                path = os.path.join(bk_dir, f"p2p_auto_{ts}.zip")
                if not os.path.isfile(path):
                    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for fn in sorted(os.listdir(DATA_DIR)):
                            fp = os.path.join(DATA_DIR, fn)
                            if os.path.isfile(fp) and not fn.startswith(("paste_", "voice_")):
                                try:
                                    zf.write(fp, "p2pdata/" + fn)
                                except Exception:
                                    pass
                    # 保留最近 24 份
                    olds = sorted(glob.glob(os.path.join(bk_dir, "p2p_auto_*.zip")))
                    for f_ in olds[:-24]:
                        try:
                            os.remove(f_)
                        except Exception:
                            pass
            except Exception:
                pass
            self._auto_backup_stop.wait(3600)

    def _start_auto_backup(self):
        try:
            self._auto_backup_stop = threading.Event()
            threading.Thread(target=self._auto_backup_loop, daemon=True).start()
        except Exception:
            pass
        # 启动时后台清理下载目录中超过 30 天的临时/分片文件
        try:
            threading.Thread(target=self._cleanup_downloads, daemon=True).start()
        except Exception:
            pass

    def _cleanup_downloads(self):
        """清理收件夹里超过 30 天的 .p2pchat-part 分片残留（失败中断下载的垃圾）。"""
        try:
            if not os.path.isdir(DOWNLOADS_DIR):
                return
            import glob
            cutoff = time.time() - 30 * 86400
            for p in glob.glob(os.path.join(DOWNLOADS_DIR, ".p2pchat-part-*")):
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except Exception:
                    pass
        except Exception:
            pass

    def _restore_data(self):
        """从 zip 备份恢复数据（覆盖当前数据）。"""
        try:
            import zipfile
            from tkinter import filedialog
            path = filedialog.askopenfilename(title="选择备份文件",
                                              filetypes=[("ZIP 压缩包", "*.zip")])
            if not path:
                return
            if not messagebox.askyesno("恢复数据",
                                       "恢复将覆盖当前的全部聊天记录与设置，确定继续吗？"):
                return
            count = 0
            with zipfile.ZipFile(path, "r") as zf:
                for member in zf.namelist():
                    if member.startswith("p2pdata/"):
                        fn = os.path.basename(member)
                        if not fn or ".." in fn:
                            continue
                        data = zf.read(member)
                        with open(os.path.join(DATA_DIR, fn), "wb") as fh:
                            fh.write(data)
                        count += 1
            self._set_status(f"已恢复 {count} 个文件（重启后生效）", "ok")
            global _SETTINGS_CACHE
            _SETTINGS_CACHE = None  # 设置文件已还原，失效内存缓存（强制重新读盘）
        except Exception:
            self._set_status("恢复失败", "err")

    def _show_my_card(self):
        """显示我的名片二维码：对方扫码即可录入我的 ID 与昵称（QQ 扫码加好友的等价替代）。"""
        data = json.dumps({"p2pcard": 1, "cid": self.cid,
                           "name": (self._profile_name or self.nick_var.get() or "未命名")},
                          ensure_ascii=False)
        path = os.path.join(DATA_DIR, "my_card.png")
        if not _make_qr_png(data, path):
            self._set_status("生成名片失败（缺少 qrcode 库）", "err")
            return
        try:
            win = ctk.CTkToplevel(self.root)
            win.title("我的名片")
            win.geometry("300x380")
            win.resizable(False, False)
            win.attributes("-topmost", True)
            from PIL import Image as _I
            img = _I.open(path).convert("RGB")
            ctk_img = CTkImage(light_image=img, dark_image=img, size=(240, 240))
            self._images.append(ctk_img)
            ctk.CTkLabel(win, image=ctk_img, text="").pack(pady=(18, 4))
            ctk.CTkLabel(win, text=f"{self._profile_name or '未命名'} · ID: {self.cid}",
                         font=(FONT, 12, "bold"), text_color=C("text")).pack()
            ctk.CTkLabel(win, text="让对方在应用里「扫名片」即可录入我",
                         font=(FONT, 10), text_color=C("text_mute")).pack(pady=(2, 8))
            ctk.CTkButton(win, text="保存二维码", width=120, height=30, corner_radius=8,
                          fg_color=C("input_bg"), text_color=C("text"),
                          hover_color=C("input_hover"), font=(FONT, 11),
                          command=lambda: self._save_card_png(path)).pack(pady=6)
            win.bind("<Escape>", lambda e: win.destroy())
        except Exception:
            pass

    def _save_card_png(self, src):
        try:
            out = filedialog.asksaveasfilename(title="保存名片二维码", defaultextension=".png",
                                               initialfile="my_card.png",
                                               filetypes=[("PNG 图片", "*.png")])
            if out and os.path.isfile(src):
                with open(src, "rb") as fh, open(out, "wb") as fo:
                    fo.write(fh.read())
                self._set_status(f"已保存到 {out}", "ok")
        except Exception:
            self._set_status("保存失败", "err")

    def _scan_card(self):
        """扫名片：选择二维码图片，识别并录入联系人（ID + 昵称）。"""
        path = filedialog.askopenfilename(title="选择名片二维码图片",
                                          filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not path:
            return
        text = _read_qr_text(path)
        if not text:
            self._set_status("未识别到二维码", "err")
            return
        try:
            d = json.loads(text)
            if d.get("p2pcard") == 1 and d.get("cid"):
                cid = str(d["cid"])
                name = str(d.get("name", "？"))[:60]
                self._ensure_dm_session(cid, name)
                self._toggle_contact(cid, name)
                self._set_status(f"✅ 已录入名片：{name}（可点击发起私聊）", "ok")
                return
        except Exception:
            pass
        self._set_status("不是有效的 P2P 名片二维码", "err")

    def _open_firewall_ports(self):
        """手动开放直连端口（需管理员权限）：后台线程执行，不阻塞界面。"""
        self._set_status("正在请求开放直连端口…", "mute")
        def _work():
            ok = True
            msgs = []
            try:
                import subprocess
                for port in (LAN_PORT, LAN_MSG_PORT):
                    rule = f"P2PChat Direct {port}"
                    try:
                        r = subprocess.run(
                            ["netsh", "advfirewall", "firewall", "add", "rule",
                             f"name={rule}", "dir=in", "action=allow", "protocol=TCP",
                             f"localport={port}", "profile=any"],
                            capture_output=True, text=False, timeout=10,  # bytes 输出，防 GBK 解码崩溃
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                        _out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace").strip()
                        msgs.append((port, r.returncode, _out))
                        if r.returncode != 0:
                            ok = False
                    except Exception as e:
                        ok = False
                        msgs.append((port, -1, str(e)[:60]))
            except Exception:
                ok = False
            def _done():
                if ok:
                    self._set_status("已开放直连端口 47654/47656（IPv6 公网直连可用）", "ok")
                else:
                    self._set_status("开放失败，请以管理员身份运行后重试", "err")
                    try:
                        import tkinter.messagebox as mb
                        mb.showwarning("开放端口", "需要管理员权限才能修改防火墙。\n请右键程序图标选择「以管理员身份运行」后，再点一次本菜单项。")
                    except Exception:
                        pass
            try:
                self.root.after(0, _done)
            except Exception:
                pass
        try:
            threading.Thread(target=_work, daemon=True).start()
        except Exception:
            self._set_status("开放失败，请重试", "err")

    def _measure_network(self):
        """测量到 MQTT 服务器的连接延迟（TCP 建连耗时，主服务器 + 辅助服务器对比）。"""
        self._set_status("正在测速…", "mute")

        def _probe(host, port, timeout=5.0):
            import socket as _s
            t0 = time.time()
            s = _s.create_connection((host, int(port)), timeout=timeout)
            s.close()
            return (time.time() - t0) * 1000

        targets = [(self.broker, int(self.port))]
        seen = {(self.broker, int(self.port))}
        for h, p_ in AUX_BROKERS:
            if (h, int(p_)) not in seen:
                targets.append((h, int(p_)))
                seen.add((h, int(p_)))
        lines = []
        for host, port in targets[:4]:
            try:
                ms = _probe(host, port)
                lines.append(f"{host}:{port}  {ms:.0f}ms")
            except Exception:
                lines.append(f"{host}:{port}  超时")
        self._set_status(" · ".join(lines), "accent" if "超时" not in "".join(lines) else "ok")

    def _export_current_history(self, html=False):
        """把当前会话的聊天记录导出为 txt（默认）或 html 网页文件。"""
        s = self._sessions.get(self._current)
        if s is None:
            self._set_status("当前没有选中的会话", "err")
            return
        try:
            from tkinter import filedialog
            if html:
                path = filedialog.asksaveasfilename(
                    title="导出聊天记录（网页）", defaultextension=".html",
                    initialfile=f"聊天记录_{s.get('name', '会话')}.html",
                    filetypes=[("网页文件", "*.html")])
            else:
                path = filedialog.asksaveasfilename(
                    title="导出聊天记录", defaultextension=".txt",
                    initialfile=f"聊天记录_{s.get('name', '会话')}.txt",
                    filetypes=[("文本文件", "*.txt")])
        except Exception:
            path = ""
        if not path:
            return
        try:
            head_time = time.strftime("%Y-%m-%d %H:%M:%S")
            if html:
                self._write_history_html(path, s, head_time)
            else:
                lines = [f"===== {s.get('name', '会话')} 聊天记录 =====",
                         f"导出时间：{head_time}", ""]
                for m in s["messages"]:
                    ts = _fmt_time(m.get("ts"))
                    name = str(m.get("name", "?"))
                    text = str(m.get("text", ""))
                    if m.get("system"):
                        lines.append(f"[系统] {text}")
                    elif m.get("recalled"):
                        lines.append(f"[{ts}] {name}：（已撤回）")
                    elif m.get("img_path"):
                        lines.append(f"[{ts}] {name}：[图片] {text}")
                    else:
                        lines.append(f"[{ts}] {name}：{text}")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines))
            self._set_status(f"已导出到 {path}", "ok")
        except Exception:
            pass

    def _export_current_history_html(self):
        self._export_current_history(html=True)

    def _write_history_html(self, path, s, head_time):
        """把会话记录写成带气泡样式的单文件 HTML（双击即看，无外部依赖）。"""
        def _esc(t):
            import html as _h
            return _h.escape(str(t or ""))
        rows = []
        for m in s["messages"]:
            ts = _fmt_time(m.get("ts"))
            name = _esc(m.get("name", "?"))
            mine = bool(m.get("mine"))
            cls = "mine" if mine else "other"
            if m.get("system"):
                rows.append(f'<div class="sys">[系统] {_esc(m.get("text",""))}</div>')
                continue
            if m.get("recalled"):
                rows.append(f'<div class="sys">[{ts}] {name}：（已撤回）</div>')
                continue
            body = _esc(m.get("text", "")).replace("\n", "<br>")
            tag = body
            if m.get("img_path"):
                tag = f'<img src="file:///{m["img_path"]}" alt="图片">'
            elif m.get("voice"):
                tag = "🎤 [语音消息]"
            elif m.get("file_path"):
                _fn = _esc(os.path.basename(str(m["file_path"])))
                _sz = ""
                try:
                    _sz = f"（{os.path.getsize(m['file_path']) // 1024} KB）"
                except Exception:
                    pass
                tag = f'📎 <a href="file:///{_esc(m["file_path"])}">{_fn}</a> {_sz}'
            # 回复引用（QQ 式：引用块显示原消息）
            if m.get("reply"):
                _r = m["reply"]
                tag = (f'<div class="quote">↩ {_esc(str(_r.get("name",""))) }：'
                       f'{_esc(str(_r.get("text","")))[:60]}</div>' + tag)
            # 已编辑标记
            if m.get("edited"):
                tag += ' <span class="edited">已编辑</span>'
            rows.append(
                f'<div class="row {cls}"><div class="meta">{name} · {ts}</div>'
                f'<div class="bubble">{tag}</div></div>')
        css = """
        body{font-family:'Microsoft YaHei',sans-serif;max-width:760px;margin:24px auto;background:#1e1f22;color:#dbdee1;padding:0 16px}
        h1{font-size:18px;border-bottom:1px solid #333;padding-bottom:8px}
        .meta{font-size:11px;color:#949ba4;margin-bottom:2px}
        .row{display:flex;flex-direction:column;margin:8px 0}
        .row.mine{align-items:flex-end}.row.other{align-items:flex-start}
        .bubble{max-width:70%;padding:8px 12px;border-radius:12px;font-size:14px;word-break:break-all}
        .mine .bubble{background:#5865f2;color:#fff}
        .other .bubble{background:#2b2d31;color:#dbdee1}
        .sys{text-align:center;color:#949ba4;font-size:12px;margin:10px 0}
        img{max-width:260px;border-radius:8px}
        .quote{font-size:12px;color:#949ba4;border-left:3px solid #5865f2;padding:2px 8px;margin-bottom:4px;background:#313338;border-radius:6px}
        .edited{font-size:10px;color:#949ba4;margin-left:6px}
        a{color:#7289da}
        """
        doc = (f"<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
               f"<title>{_esc(s.get('name','会话'))} 聊天记录</title>"
               f"<style>{css}</style></head><body>"
               f"<h1>{_esc(s.get('name','会话'))} · 聊天记录（导出于 {_esc(head_time)}）</h1>"
               + "".join(rows) + "</body></html>")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(doc)

    def _clear_current_history(self):
        s = self._sessions.get(self._current)
        if s is None:
            messagebox.showinfo("提示", "当前没有选中的会话。")
            return
        if not messagebox.askyesno("清空记录",
                                   "确定清空当前会话的全部聊天记录吗？此操作不可撤销。"):
            return
        s["messages"] = []
        self._mid_index.pop(self._current, None)
        self._unread_total -= s.get("unread", 0)
        if self._unread_total < 0:
            self._unread_total = 0
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
        self._msg_search_after = self.root.after(150, self._apply_search)

    def _apply_search(self):
        self._msg_search_after = None
        try:
            q = self.search_entry.get().strip()
        except Exception:
            q = ""
        if q == self._search_query:
            return
        self._search_query = q
        # 记录命中列表，供 Enter / ▲▼ 上下跳转
        hits = []
        s = self._sessions.get(self._current)
        if q and s:
            hits = [m.get("mid") for m in s.get("messages", [])
                    if m.get("mid") and self._msg_matches_search(m, q)]
        self._search_hits = hits
        self._search_hit_idx = -1
        try:
            n = len(hits)
            self.search_count_lbl.configure(text=(f"{n} 条" if n else "无命中"))
        except Exception:
            pass
        self._render_feed()

    def _msg_matches_search(self, m, q):
        """判断消息是否命中搜索关键词（正文 / 发送者 / 文件名）。"""
        q = (q or "").lower()
        if not q:
            return False
        try:
            return (q in str(m.get("text", "")).lower()
                    or q in str(m.get("name", "")).lower()
                    or (m.get("file_path") and q in os.path.basename(str(m["file_path"])).lower()))
        except Exception:
            return False

    def _jump_search_hit(self, delta):
        """在搜索命中之间跳转（Enter=下一处，Shift+Enter=上一处）。"""
        try:
            hits = getattr(self, "_search_hits", None) or []
            if not hits:
                self._set_status("当前搜索没有命中", "mute")
                return
            idx = getattr(self, "_search_hit_idx", -1)
            idx = (idx + delta) % len(hits)
            self._search_hit_idx = idx
            self._scroll_to_mid(hits[idx])
            self._set_status(f"搜索命中 {idx + 1}/{len(hits)} · Enter 下一处", "accent")
        except Exception:
            pass

    def _open_global_search(self, event=None):
        """全局搜索（Ctrl+Shift+F）：跨所有会话实时搜索，列出命中消息预览，点击直达。"""
        try:
            win = ctk.CTkToplevel(self.root)
            win.title("全局搜索")
            win.geometry("480x540")
            win.resizable(False, False)
            try:
                self._round_toplevel(win)
            except Exception:
                pass
            win.attributes("-topmost", True)
            ctk.CTkLabel(win, text="🔍 全局搜索", font=(FONT, 14, "bold"),
                         text_color=C("text")).pack(pady=(12, 2))
            entry = ctk.CTkEntry(win, placeholder_text="搜索所有会话的消息（输入即搜）…",
                                 height=32, corner_radius=8, font=(FONT, 12),
                                 fg_color=C("input_bg"), text_color=C("text"), border_width=0)
            entry.pack(fill="x", padx=18, pady=(4, 6))
            results = ctk.CTkScrollableFrame(win, fg_color="transparent")
            results.pack(fill="both", expand=True, padx=14, pady=(0, 10))
            _search_after = {"id": None}

            def _do_search(_e=None):
                if _search_after["id"] is not None:
                    try:
                        win.after_cancel(_search_after["id"])
                    except Exception:
                        pass
                _search_after["id"] = win.after(120, lambda: _run_search())

            def _run_search():
                _search_after["id"] = None
                q = entry.get().strip()
                for w in results.winfo_children():
                    w.destroy()
                if not q:
                    ctk.CTkLabel(results, text="输入关键词后自动搜索所有会话",
                                 text_color=C("text_mute"), font=(FONT, 11)).pack(pady=16)
                    return
                ql = q.lower()
                total = 0
                MAX_HITS = 60  # 结果上限：超大聊天记录只列前 60 条，避免卡界面
                hits = []
                for key, s in self._sessions.items():
                    if s.get("kind") not in ("group", "dm"):
                        continue
                    if len(hits) >= MAX_HITS:
                        break
                    for m in s.get("messages", []):
                        if len(hits) >= MAX_HITS:
                            break
                        if not m.get("mid"):
                            continue
                        if not self._msg_matches_search(m, q):
                            continue
                        total += 1
                        txt = str(m.get("text", "")).replace("\n", " ").strip()
                        name = str(m.get("name", "?"))
                        ts = _fmt_time(m.get("ts")) if m.get("ts") else ""
                        snip = _extract_search_snippet(txt or "", q, width=18) or txt[:36]
                        hits.append((key, m.get("mid"), name, ts, snip))
                if not hits:
                    ctk.CTkLabel(results, text="没有找到匹配的消息",
                                 text_color=C("text_mute"), font=(FONT, 11)).pack(pady=16)
                    return
                shown = min(len(hits), MAX_HITS)
                ctk.CTkLabel(results, text=f"共 {total} 条命中 · 点击直达",
                             text_color=C("accent"), font=(FONT, 10, "bold")).pack(pady=(2, 4))
                for key, mid, name, ts, snip in hits[:shown]:
                    row = ctk.CTkFrame(results, fg_color=C("input_bg"), corner_radius=8)
                    row.pack(fill="x", pady=2)
                    row.bind("<Button-1>",
                             lambda e, k=key, mm=mid: self._jump_global_msg(win, k, mm))
                    head = ctk.CTkFrame(row, fg_color="transparent")
                    head.pack(fill="x", padx=10, pady=(6, 0))
                    ctk.CTkLabel(head, text=f"{name} · {ts}", text_color=C("text_mute"),
                                 font=(FONT, 9, "bold"), anchor="w").pack(side="left")
                    ctk.CTkLabel(head, text="→", text_color=C("accent"),
                                 font=(FONT, 10, "bold")).pack(side="right")
                    body = ctk.CTkLabel(row, text=snip, anchor="w", justify="left",
                                        wraplength=410, text_color=C("text"),
                                        font=(FONT, 11))
                    body.pack(fill="x", padx=10, pady=(2, 6))
                    for _w in (row, head, body):
                        _w.bind("<Button-1>",
                                lambda e, k=key, mm=mid: self._jump_global_msg(win, k, mm), add="+")
                if len(hits) > shown:
                    ctk.CTkLabel(results, text=f"… 仅显示前 {shown} 条（共 {total} 条）",
                                 text_color=C("text_mute"), font=(FONT, 9)).pack(pady=(2, 4))

            entry.bind("<KeyRelease>", _do_search)
            entry.bind("<Return>", _do_search)
            win.bind("<Escape>", lambda e: win.destroy())
            entry.focus_set()
        except Exception:
            pass

    def _jump_global_msg(self, win, key, mid):
        """全局搜索命中直达：切会话 + 滚动到该消息并高亮。"""
        try:
            win.destroy()
        except Exception:
            pass
        try:
            self._switch_to(key)
            self._jump_to_message(mid)
            self._set_status("已定位到匹配消息", "ok")
        except Exception:
            pass

    def _jump_global_result(self, win, key, q):
        """全局搜索跳转：切到目标会话并复用会话内搜索高亮。"""
        try:
            win.destroy()
        except Exception:
            pass
        try:
            self._switch_to(key)
            self._open_search()
            self.search_entry.delete(0, "end")
            self.search_entry.insert(0, q)
            self._apply_search()
            self._set_status(f"已跳转到「{key}」：搜索「{q}」", "ok")
        except Exception:
            pass

    def _shortcut_new_dm(self, event=None):
        self._open_dm_dialog()
        return "break"

    def _shortcut_reconnect(self, event=None):
        try:
            self._set_status("正在重新连接…", "mute")
            self._ensure_connected()
        except Exception:
            pass
        return "break"

    def _switch_session_dir(self, delta):
        """Alt+↑/↓ 在会话间切换（循环）。"""
        try:
            keys = list(self._sessions.keys())
            if not keys:
                return
            try:
                idx = keys.index(self._current)
            except Exception:
                idx = 0
            nxt = keys[(idx + delta) % len(keys)]
            self._switch_to(nxt)
        except Exception:
            pass

    def _goto_next_unread(self):
        """跳转到下一条有未读的会话（按会话列表显示顺序循环）。"""
        try:
            # 与会话列表同序：群聊（置顶优先/最近活跃）→ 私聊（置顶/未读/活跃）
            unread_keys = {k for k, s in self._sessions.items() if (s.get("unread") or 0) > 0}
            if not unread_keys:
                self._set_status("没有未读消息", "mute")
                return
            ordered = []
            for room in getattr(self, "_rooms", []) or []:
                k = self._group_key(room)
                if k in unread_keys:
                    ordered.append(k)
            dms = sorted([s for s in self._sessions.values() if s["kind"] == "dm" and s["key"] in unread_keys],
                         key=lambda s: (0 if self._is_pinned_session(s["key"]) else 1,
                                        -(s.get("unread") or 0),
                                        -self._session_last_ts(s), s["name"]))
            ordered.extend(s["key"] for s in dms)
            if not ordered:
                self._set_status("没有未读消息", "mute")
                return
            cur = self._current
            try:
                idx = ordered.index(cur)
            except Exception:
                idx = -1
            nxt = ordered[(idx + 1) % len(ordered)]
            self._switch_to(nxt)
            self._set_status(f"跳转到未读会话（剩 {len(ordered) - 1} 条未读会话）", "accent")
        except Exception:
            pass

    def _mark_all_read(self):
        """全部已读：一键清零所有会话未读数（任务栏角标同步消失）。"""
        try:
            n = 0
            for s in self._sessions.values():
                if s.get("unread"):
                    s["unread"] = 0
                    n += 1
                s["@me"] = False
            self._unread_total = 0
            self._last_list_fp = None
            self._apply_session_list()
            self._update_window_title()
            self._refresh_mention_btn()
            self._set_status("已全部标为已读" if n else "当前没有未读消息", "ok")
        except Exception:
            pass

    def _refresh_mention_btn(self):
        """刷新标题栏「@我」按钮的未读数量角标。"""
        try:
            n = sum(1 for s in self._sessions.values() if s.get("@me"))
            btn = getattr(self, "mention_btn", None)
            if btn is None:
                return
            if n:
                btn.configure(text=f"📢 @我 ({n})",
                              fg_color=C("accent"), text_color="#ffffff")
            else:
                btn.configure(text="📢 @我",
                              fg_color=C("input_bg"), text_color=C("text_2"))
        except Exception:
            pass

    def _open_mentions(self):
        """@我汇总：列出所有会话里 @我的消息，点击直接跳转（QQ 消息盒子）。"""
        try:
            items = []
            for key, s in self._sessions.items():
                for m in s.get("messages", []):
                    if m.get("mid") and self._mentions_me(m.get("text", ""))                             and not m.get("mine") and not m.get("system")                             and not m.get("recalled"):
                        items.append((float(m.get("ts") or 0), key, m))
            items.sort(key=lambda x: -x[0])
            win = ctk.CTkToplevel(self.root)
            win.title("@我 的消息")
            win.geometry("400x460")
            win.resizable(False, False)
            try:
                self._round_toplevel(win)
            except Exception:
                pass
            win.attributes("-topmost", True)
            if not items:
                ctk.CTkLabel(win, text="（还没有人 @ 你）", text_color=C("text_mute"),
                             font=(FONT, 12)).pack(pady=24)
                win.bind("<Escape>", lambda e: win.destroy())
                return
            top_row = ctk.CTkFrame(win, fg_color="transparent")
            top_row.pack(fill="x", padx=12, pady=(10, 2))
            ctk.CTkLabel(top_row, text=f"📢 共 {len(items)} 条 @我的消息 · 点击跳转",
                         text_color=C("accent"), font=(FONT, 11, "bold")).pack(side="left")
            # 一键清除所有 @我 角标（保留消息，仅消红点）
            ctk.CTkButton(top_row, text="全部已读", width=76, height=26, corner_radius=8,
                          fg_color=C("input_bg"), text_color=C("text_2"),
                          hover_color=C("input_hover"), font=(FONT, 10),
                          command=lambda: self._mark_all_mentions_read(win)).pack(side="right")
            scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
            scroll.pack(fill="both", expand=True, padx=12, pady=(0, 10))
            for ts, key, m in items[:100]:
                sname = self._sessions.get(key, {}).get("name") or key
                who = str(m.get("name", "对方"))
                snip = str(m.get("text", "")).replace("\n", " ")[:46]
                row = ctk.CTkButton(
                    scroll, text=f"[{sname}] {who}：{snip}", height=34, corner_radius=8,
                    anchor="w", fg_color=C("input_bg"), text_color=C("text"),
                    hover_color=C("input_hover"), font=(FONT, 11),
                    command=lambda k=key, mm=m.get("mid"): self._jump_mention(win, k, mm))
                row.pack(fill="x", pady=2)
            win.bind("<Escape>", lambda e: win.destroy())
        except Exception:
            pass

    def _mark_all_mentions_read(self, win=None):
        """清除所有会话的 @我 角标（消息保留）。"""
        try:
            for s in self._sessions.values():
                s["@me"] = False
            self._refresh_mention_btn()
            self._last_list_fp = None
            self._apply_session_list()
            self._set_status("所有 @我 已标为已读", "ok")
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass
        except Exception:
            pass

    def _jump_mention(self, win, key, mid):
        try:
            win.destroy()
        except Exception:
            pass
        try:
            self._switch_to(key)
            s = self._sessions.get(key)
            if s:
                s["@me"] = False
                self._refresh_mention_btn()
            # 命中消息可能在折叠区（RENDER_MAX 之外）：先展开全部历史再渲染跳转
            self._history_expanded = True
            self._render_feed()
            self.root.after(60, lambda m=mid: self._scroll_to_mid(m))
        except Exception:
            pass

    def _toggle_feed_filter(self, kind):
        """消息筛选开关：只看图片 / 只看文件（再点取消，可与搜索叠加）。"""
        try:
            self._feed_filter = "" if self._feed_filter == kind else kind
            for k, btn in (("img", getattr(self, "filter_img_btn", None)),
                           ("file", getattr(self, "filter_file_btn", None))):
                if btn is None:
                    continue
                on = self._feed_filter == k
                try:
                    btn.configure(fg_color=(C("accent") if on else C("input_bg")),
                                  text_color=("#ffffff" if on else C("text_2")))
                except Exception:
                    pass
            self._render_feed()
            if self._feed_filter:
                self._set_status(f"筛选：只看{'图片' if self._feed_filter == 'img' else '文件'}（再次点击取消）", "accent")
        except Exception:
            pass

    def _clear_all_history(self):
        if not self._sessions:
            messagebox.showinfo("提示", "当前没有任何会话记录。")
            return
        if not messagebox.askyesno("清空所有记录",
                                   "确定清空全部会话的聊天记录吗？此操作不可撤销。"):
            return
        for s in self._sessions.values():
            s["messages"] = []
            self._mid_index.pop(s.get("key"), None)
            self._unread_total -= s.get("unread", 0)
            s["unread"] = 0
            if s["kind"] == "group":
                _delete_group_history(s["room"])
            else:
                _delete_dm_history(s["cid"])
        if self._unread_total < 0:
            self._unread_total = 0
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
        self._mid_index.pop(key, None)
        if self._current == key:
            if self._rooms:
                self._switch_to(self._group_key(self._rooms[0]))
            else:
                self._current = None
                self._update_chat_title()
                self._render_feed()
        self._apply_session_list()

    def _group_context_menu(self, event, key):
        try:
            menu = tk.Menu(self.root, tearoff=0, font=(FONT, 10))
            muted = self._is_muted(key)
            pinned = self._is_pinned_session(key)
            room = self._sessions.get(key, {}).get("room", "") if self._sessions.get(key) else ""
            menu.add_command(label="✏️ 重命名显示名…", command=lambda: self._rename_session(key))
            menu.add_command(label=("取消置顶" if pinned else "置顶会话"),
                             command=lambda: self._toggle_pin_session(key))
            menu.add_command(label=("取消静音" if muted else "静音会话"),
                             command=lambda: self._toggle_mute(key))
            menu.add_separator()
            menu.add_command(label="清空记录", command=self._clear_current_history)
            if room:
                menu.add_command(label=f"删除并退出「{room}」",
                                 command=lambda r=room: self._remove_room(r))
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _dm_context_menu(self, event, s):
        try:
            menu = tk.Menu(self.root, tearoff=0, font=(FONT, 10))
            fav = self._is_contact(s["cid"])
            menu.add_command(label=("取消收藏" if fav else "★ 收藏联系人"),
                             command=lambda: self._toggle_contact(s["cid"], s["name"]))
            menu.add_command(label="✏️ 重命名显示名…", command=lambda: self._rename_session(s["key"]))
            muted = self._is_muted(s["key"])
            pinned = self._is_pinned_session(s["key"])
            menu.add_command(label=("取消置顶" if pinned else "置顶会话"),
                             command=lambda: self._toggle_pin_session(s["key"]))
            menu.add_command(label=("取消静音" if muted else "静音会话"),
                             command=lambda: self._toggle_mute(s["key"]))
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
        _save_profile(name, self._avatar, self._bio)
        if not self._avatar:
            self._render_top_avatar()
        if self.backend and self.backend.running:
            self.backend.change_nick(name)

    def _disconnect(self):
        """主动断开连接（房间即群组：断开后自动重新常驻上线由 _ensure_connected 兜底）。"""
        if self.backend and self.backend.running:
            self.backend.stop()
            self.backend = None
            self._peers = {}
            self._lan_peers = {}
            for s in self._sessions.values():
                if s["kind"] == "dm":
                    s["online"] = False
            self._set_status("未连接", "mute")
            self._apply_session_list()
            self._update_chat_title()

    def _ensure_connected(self):
        """房间即群组：确保后端已连接并常驻上线（加入房间 / 启动 / 断开后自动重连）。"""
        if self.backend and self.backend.running:
            return True
        name = self.nick_var.get().strip() or "未命名"
        # 连接前重新加载每个房间的历史（保证重连历史不丢）
        for room in self._rooms:
            s = self._ensure_group_session(room)
            s["messages"] = _load_group_history(room, self.FEED_MAX)
            self._rebuild_mid_index(s)

        self.backend = MqttBackend(
            name, self.cid, self.broker, self.port,
            on_text=self._cb_text,
            on_peers=self._cb_peers,
            on_file=self._cb_file,
            on_status=self._cb_status,
            on_dm=self._cb_dm,
            on_read=self._cb_read,
            on_recall=self._cb_recall,
            on_typing=self._cb_typing,
            on_delivered=self._cb_delivered,
            on_edit=self._cb_edit,
            on_reaction=self._cb_reaction,
            on_lan_peers=self._cb_lan_peers,
            passphrase=self.encrypt_pass,
        )
        for room in self._rooms:
            self.backend.add_room(room)
        self.backend.start()
        self._set_status("正在连接…", "mute")
        if self._current is None and self._rooms:
            self._switch_to(self._group_key(self._rooms[0]))
        else:
            self._render_feed()
        self._apply_session_list()
        return True

    def _toggle_connect(self):
        # 兼容旧调用：菜单“断开连接”走 _disconnect，其余一律确保连接
        if self.backend and self.backend.running:
            self._disconnect()
        else:
            self._ensure_connected()

    # --------------------------- 发送 ---------------------------

    def _update_send_btn_state(self):
        """输入为空时发送按钮置灰，有内容时点亮（交互反馈）。"""
        try:
            txt = self.input_box.get("1.0", "end").strip()
            has = bool(txt) and not self._hint_active
            if has != getattr(self, "_send_btn_active", False):
                self._send_btn_active = has
                self.send_btn.configure(
                    fg_color=(C("accent") if has else C("input_bg")),
                    text_color=("#ffffff" if has else C("text_mute")),
                    hover_color=(C("accent_hover") if has else C("input_hover")))
        except Exception:
            pass

    def _send_text(self):
        if self._hint_active:
            return
        text = self.input_box.get("1.0", "end").strip()
        if not text:
            return
        # 粘贴 p2pchat://msg/会话#mid 链接：识别并自动跳转到对应消息（不当作文字发出）
        if text.startswith("p2pchat://msg/") and "#" in text:
            try:
                rest = text[len("p2pchat://msg/"):]
                room_part, _, mid_part = rest.rpartition("#")
                if room_part and mid_part:
                    for k, s in self._sessions.items():
                        target_room = s.get("room") if s["kind"] == "group" else s.get("cid")
                        if target_room == room_part and s.get("kind") in ("group", "dm"):
                            self.input_box.delete("1.0", "end")
                            self._switch_to(k)
                            self._jump_to_message(mid_part)
                            self._set_status("已定位到消息链接指向的消息", "ok")
                            return
                    self._set_status(f"未找到会话 {room_part}（先加入该房间）", "err")
                    return
            except Exception:
                pass
        if not (self.backend and self.backend.online):
            self._show_system("尚未连接，无法发送。")
            return
        self.input_box.delete("1.0", "end")
        try:
            self._send_btn_active = False
            self.send_btn.configure(fg_color=C("input_bg"),
                                    text_color=C("text_mute"),
                                    hover_color=C("input_hover"))
        except Exception:
            pass
        s = self._sessions.get(self._current)
        if s is not None and s.get("draft"):
            s["draft"] = ""  # 发送成功清除草稿标记
            try:
                self._last_list_fp = None
                self._apply_session_list()
            except Exception:
                pass
        if s is None:
            return
        my = self.nick_var.get().strip() or "未命名"
        reply = self._reply_to
        if s["kind"] == "dm":
            if self.backend.send_dm(s["cid"], text, reply):
                self._append_message(s["key"], my, text, True, reply=reply)
                self._cancel_reply()
            else:
                self.input_box.insert("1.0", text)
                self._show_system("发送失败，请检查连接。")
        else:
            if self.backend.send_text(s["room"], text, reply):
                self._cancel_reply()
            else:
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
        """Ctrl+V：若剪贴板是图片/图片文件则作为图片发送，否则走默认文本粘贴。

        支持三种剪贴板内容：
        1) 图像对象（截图工具/浏览器复制的位图）
        2) 图片文件路径列表（文件管理器复制的图片）
        3) 普通文本（默认行为）
        """
        try:
            from PIL import Image, ImageGrab
            clip = None
            try:
                clip = ImageGrab.grabclipboard()
            except Exception:
                clip = None
            if clip is None:
                return None
            # 图片文件路径列表：发送第一个图片
            if isinstance(clip, (list, tuple)) and clip:
                p0 = str(clip[0])
                if os.path.isfile(p0) and _is_image_path(p0):
                    if not (self.backend and self.backend.online):
                        self._show_system("尚未连接，无法发送。")
                        return "break"
                    self._do_send_file(p0)
                    try:
                        self.input_box.focus_set()
                    except Exception:
                        pass
                    return "break"
                return None  # 文件非图片：走默认粘贴路径
            if isinstance(clip, Image.Image):
                if not (self.backend and self.backend.online):
                    self._show_system("尚未连接，无法发送。")
                    return "break"
                try:
                    _ensure_data_dir()
                    path = os.path.join(DATA_DIR, "paste_" + uuid.uuid4().hex[:10] + ".png")
                    if clip.mode not in ("RGB", "RGBA"):
                        clip = clip.convert("RGB")
                    clip.save(path, "PNG")
                    self._do_send_file(path)
                except Exception:
                    pass
                return "break"
        except Exception:
            pass
        return None

    def _mention_names(self):
        """可 @ 的成员：当前房间在线成员 + 在线名单（去重）。

        缓存按当前会话 key 缓存：渲染一屏消息时 _extract_mentions 对每条消息
        都会调用本方法，重复构建同一份名字列表是纯浪费（批量渲染 200 条时
        差异明显）。切换会话 / 成员变化 / 昵称变化时失效重算。"""
        tag = None
        try:
            s = self._sessions.get(self._current)
            room = s.get("room") if s else None
            tag = (self._current, room, tuple(sorted(str(p.get("name", "")) for p in self._peers.values())))
            if self._mention_cache and self._mention_cache[0] == tag:
                return self._mention_cache[1]
        except Exception:
            pass
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
        try:
            self._mention_cache = (tag, names)
        except Exception:
            pass
        return names

    def _autosize_input(self):
        """输入框高度自适应：内容多行时自动增高，发送后恢复（300ms 节流）。"""
        if getattr(self, "_autosize_after", None) is not None:
            try:
                self.root.after_cancel(self._autosize_after)
            except Exception:
                pass
        self._autosize_after = self.root.after(300, self._do_autosize_input)

    def _do_autosize_input(self):
        self._autosize_after = None
        try:
            box = self.input_box
            cur = int(box.cget("height"))
            text = box.get("1.0", "end").rstrip(chr(10))
            est = 0
            for ln in text.split(chr(10)):
                est += max(1, -(-len(ln) // 60))
            target = max(2, min(est, 5))
            # 行高随字号缩放：字号大时每行更高，避免多行输入框被估矮
            line_h = max(18, int(18 + (self._chat_font_size - 12) * 1.8))
            want = 72 if target <= 2 else (72 + (target - 2) * line_h)
            want = max(72, min(want, 180))
            if want != cur:
                box.configure(height=want)
        except Exception:
            pass

    def _on_input_key(self, event):
        """检测 @ 输入并弹出成员提及面板；同时广播“正在输入”。"""
        self._autosize_input()
        try:
            self._update_send_btn_state()
        except Exception:
            pass
        # @ 面板打开时：↑/↓ 选择、Enter 确认、Esc 关闭
        if getattr(self, "_mention_win", None) is not None and self._mention_win.winfo_exists():
            if event.keysym == "Down":
                self._mention_move(1)
                return "break"
            if event.keysym == "Up":
                self._mention_move(-1)
                return "break"
            if event.keysym == "Return":
                self._mention_confirm()
                return "break"
            if event.keysym == "Escape":
                self._close_mention_panel()
                return "break"
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

    def _mention_move(self, delta):
        """@ 面板 ↑/↓ 选择：高亮切换。"""
        try:
            btns = getattr(self, "_mention_btns", None) or []
            if not btns:
                return
            idx = getattr(self, "_mention_idx", 0) + delta
            self._mention_idx = idx % len(btns)
            for i, b in enumerate(btns):
                try:
                    b.configure(fg_color=(C("accent") if i == self._mention_idx else "transparent"),
                                text_color=("#ffffff" if i == self._mention_idx else C("text")))
                except Exception:
                    pass
        except Exception:
            pass

    def _mention_confirm(self):
        """@ 面板 Enter 确认：插入当前高亮的成员。"""
        try:
            btns = getattr(self, "_mention_btns", None) or []
            idx = getattr(self, "_mention_idx", 0)
            if 0 <= idx < len(btns):
                btns[idx].invoke()
        except Exception:
            pass

    def _open_mention_panel(self, partial):
        names = self._mention_names()
        # @全部成员：在空提示或匹配 all/所有人 时出现
        if not partial.strip() or partial.strip().lower() in ("all", "所有人"):
            names = ["所有人"] + names
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
        self._mention_btns = []
        self._mention_idx = 0
        for n in matches[:8]:
            b = ctk.CTkButton(self._mention_frame, text="@" + n, height=26, corner_radius=6,
                              fg_color="transparent", hover_color=C("hover"), text_color=C("text"),
                              font=(FONT, 11), anchor="w",
                              command=lambda nm=n: self._insert_mention(nm))
            b.pack(fill="x", pady=1)
            b.bind("<Enter>", lambda e, i=len(self._mention_btns): self._mention_hover(i))
            self._mention_btns.append(b)
        # 默认高亮第一个（键盘直接 Enter 插入）
        try:
            self._mention_btns[0].configure(fg_color=C("accent"), text_color="#ffffff")
        except Exception:
            pass
        self._mention_win.update_idletasks()
        w = self._mention_win.winfo_reqwidth()
        h = self._mention_win.winfo_reqheight()
        x = self.input_box.winfo_rootx()
        y = self.input_box.winfo_rooty() - h - 6
        self._mention_win.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _mention_hover(self, i):
        """@ 面板鼠标悬停：同步高亮到该按钮。"""
        try:
            self._mention_idx = i
            btns = getattr(self, "_mention_btns", None) or []
            for j, b in enumerate(btns):
                try:
                    b.configure(fg_color=(C("accent") if j == i else "transparent"),
                                text_color=("#ffffff" if j == i else C("text")))
                except Exception:
                    pass
        except Exception:
            pass

    def _insert_mention(self, name):
        try:
            text = self.input_box.get("1.0", "insert")
            at = text.rfind("@")
            if at >= 0:
                self.input_box.delete(f"1.0 + {at} chars", "insert")
                self.input_box.insert("insert", "@" + name + " ")
        except Exception:
            pass
        try:
            self._update_send_btn_state()
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
        """车回键发送模式（可设置）：
        默认 Enter=发送、Shift+Enter=换行；
        关闭“回车发送”后：QQ 风格 Ctrl+Enter=发送、Enter=换行。"""
        ctrl = bool(event.state & 0x0004)
        if getattr(self, "enter_sends", True):
            if event.state & 0x0001:     # Shift+回车 = 换行
                return None
            self._send_text()
            return "break"
        else:
            if ctrl:                    # Ctrl+Enter = 发送
                self._send_text()
                return "break"
            return None                 # Enter = 换行

    def _pick_file(self):
        paths = filedialog.askopenfilenames(title="选择要发送的文件或图片（可多选）")
        for path in paths:
            if path and os.path.isfile(path):
                self._do_send_file(path)

    def _cycle_voice_speed(self, path):
        """切换语音倍速：1x → 1.5x → 2x → 1x 循环。"""
        try:
            idx = (self._voice_speeds.get(path, 0) + 1) % len(_VOICE_SPEED)
            self._voice_speeds[path] = idx
            spd = _VOICE_SPEED[idx]
            btn = self._voice_spd_btns.get(path)
            if btn is not None:
                txt = ("1x" if spd == 1.0 else ("1.5x" if spd == 1.5 else "2x"))
                btn.configure(text=txt)
            self._set_status(f"语音倍速：{txt}", "ok")
        except Exception:
            pass

    def _toggle_voice_play(self, path, btn=None, bar=None):
        """点击语音气泡：播放（显示进度条）；再点停止。

        btn/bar 为调用方自己的控件引用：同一条语音被转发成多条消息时，
        _voice_btns/_voice_bars 按 path 索引会互相覆盖，用传入的引用隔离。"""
        if getattr(self, "_playing_voice", None) == path:
            self._stop_voice_play(path, btn=btn, bar=bar)
            return
        old_path = getattr(self, "_playing_voice", None)
        if old_path and old_path != path:
            self._stop_voice_play(old_path)  # 先停旧语音，避免按钮/进度残留
        self._playing_voice = path
        self._playing_btn = btn
        self._playing_bar = bar
        bar = bar or self._voice_bars.get(path)
        if bar is not None:
            try:
                bar.set(0)
                bar.pack(anchor="w", pady=(4, 0))
            except Exception:
                pass
        btn = btn or self._voice_btns.get(path)
        if btn is not None:
            try:
                btn.configure(text="⏹ 停止", fg_color=C("accent"),
                              hover_color=C("accent_hover"), text_color="#ffffff")
            except Exception:
                pass
        spd = _VOICE_SPEED[self._voice_speeds.get(path, 0)]
        _play_voice(path, speed=spd)
        self._voice_start_ts = time.time()
        self._voice_tick()

    def _stop_voice_play(self, path, done=False, btn=None, bar=None):
        """停止播放，恢复按钮文本并隐藏进度条（btn/bar 优先用调用方引用）。"""
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        if self._playing_voice == path:
            self._playing_voice = None
        if getattr(self, "_voice_tick_job", None) is not None:
            try:
                self.root.after_cancel(self._voice_tick_job)
            except Exception:
                pass
            self._voice_tick_job = None
        bar = bar or self._voice_bars.get(path)
        if bar is not None:
            try:
                bar.pack_forget()
                bar.set(0)
            except Exception:
                pass
        btn = btn or self._voice_btns.get(path)
        if btn is not None:
            try:
                dur = self._voice_durs.get(path) or 0
                dur_txt = f"{dur:.0f}″" if dur > 0 else ""
                btn.configure(text=f"🎤 语音 {dur_txt}",
                              fg_color=C("input_bg"),
                              hover_color=C("input_hover"), text_color=C("text"))
            except Exception:
                pass

    def _voice_tick(self):
        """播放进度定时刷新：每 100ms 更新一次，播完自动复位。"""
        path = getattr(self, "_playing_voice", None)
        if not path:
            return
        bar = getattr(self, "_playing_bar", None) or self._voice_bars.get(path)
        dur = self._voice_durs.get(path) or 0
        if bar is not None:
            try:
                if dur > 0:
                    el = time.time() - getattr(self, "_voice_start_ts", time.time())
                    bar.set(min(1.0, el / dur))
            except Exception:
                pass
        # 非 WAV（系统播放器播放）读不出时长：给兜底 20s 超时自动复位，避免按钮卡高亮
        if dur <= 0:
            dur = 20.0
        if (time.time() - getattr(self, "_voice_start_ts", time.time())) >= dur + 0.3:
            self._stop_voice_play(path, done=True,
                                  btn=getattr(self, "_playing_btn", None),
                                  bar=bar)
            return
        self._voice_tick_job = self.root.after(300, self._voice_tick)

    def _start_voice(self):
        """按住说话：开始录音。"""
        if getattr(self, "_voice_recording", False):
            return
        if not (self.backend and self.backend.online):
            self._set_status("未连接，无法发送语音", "err")
            return
        try:
            import sounddevice  # noqa: F401
        except Exception:
            self._set_status("当前环境不支持录音（缺少 sounddevice）", "err")
            return
        self._voice_recording = True
        self._voice_stop_evt = threading.Event()
        self._voice_tmp = os.path.join(DATA_DIR, "voice_" + uuid.uuid4().hex[:10] + ".wav")
        self._voice_thread = threading.Thread(
            target=lambda: _record_voice_to(self._voice_tmp, self._voice_stop_evt), daemon=True)
        self._voice_thread.start()
        try:
            self.voice_btn.configure(text="⏹", fg_color=C("danger"))
        except Exception:
            pass
        self._set_status("录音中… 松开按钮发送", "accent")

    def _stop_voice(self):
        """松开按钮：停止录音并发送（太短则取消）。"""
        if not getattr(self, "_voice_recording", False):
            return
        self._voice_recording = False
        try:
            self._voice_stop_evt.set()
        except Exception:
            pass
        try:
            self.voice_btn.configure(text="🎤", fg_color=C("input_bg"))
        except Exception:
            pass
        try:
            self._voice_thread.join(timeout=1.5)
        except Exception:
            pass
        if os.path.isfile(self._voice_tmp) and os.path.getsize(self._voice_tmp) > 4000:
            self._do_send_file(self._voice_tmp)
            self._set_status("语音已发送", "ok")
        else:
            self._set_status("录音太短，已取消", "mute")

    def _cancel_voice_recording(self):
        """录音中右键取消：停止录音并丢弃（不发送）。"""
        if not getattr(self, "_voice_recording", False):
            return
        self._voice_recording = False
        try:
            self._voice_stop_evt.set()
        except Exception:
            pass
        try:
            self.voice_btn.configure(text="🎤", fg_color=C("input_bg"))
        except Exception:
            pass
        try:
            self._voice_thread.join(timeout=1.5)
        except Exception:
            pass
        try:
            if os.path.isfile(self._voice_tmp):
                os.remove(self._voice_tmp)
        except Exception:
            pass
        self._set_status("已取消录音", "mute")

    def _toggle_emoji_panel(self):
        """打开/关闭表情面板。所有 460 个表情用单个 Canvas 绘制（create_text），
        控件数从 460 降到 1，首次打开 <0.2s；切页只重绘文本，瞬间完成。"""
        win = getattr(self, "_emoji_win", None)
        if win is not None:
            try:
                if win.winfo_exists() and win.state() == "normal":
                    self._close_emoji_panel()
                    return
            except Exception:
                win = None
        if win is None:
            try:
                import tkinter as _tk
                # 原生 tk.Toplevel（而非 CTkToplevel）：CTk 的 geometry() 会按 DPI
                # 二次缩放（如 133% 时 640x388 被放大成 960x582），导致弹出位置/尺寸
                # 异常飞出屏幕。原生 Toplevel 的 geometry 是精确逻辑像素。
                win = _tk.Toplevel(self.root)
                self._emoji_win = win
                win.overrideredirect(True)
                win.configure(bg=C("panel"))
                win.attributes("-topmost", True)
                try:
                    self._round_toplevel(win)  # Win11 圆角
                except Exception:
                    pass
                self._emoji_group_idx = 0
                self._emoji_locked = False
                self._emoji_font = getattr(self, "_emoji_font", None) or ("Segoe UI Emoji", 15)
                self._emoji_tab_font = (FONT, 9)
                self._emoji_title_font = (FONT, 10)
                # 分类页签（最近使用在前，有记录才显示；标签取短名防页签溢出）
                self._recent_emojis = list(_load_settings().get("recent_emojis", []) or [])[:24]
                self._emoji_groups = []
                if self._recent_emojis:
                    self._emoji_groups.append({"label": "最近", "items": self._recent_emojis})
                for _g in EMOJI_GROUPS:
                    self._emoji_groups.append({"label": str(_g.get("label", "?")).split(" /")[0],
                                               "items": _g.get("items", [])})
                # 布局参数
                cols = 10
                cell = 36
                rows = 7
                self._emoji_cell = cell
                self._emoji_cols = cols
                grid_w = cols * cell + 4
                grid_h = rows * cell + 8
                head_h = 30
                tab_h = 30
                pad = 6
                # 宽度：页签行自然宽度 与 表情网格宽度 取大者（上限 560）
                tf = self._emoji_tab_font
                try:
                    meas = _tk.Font(root=self.root, font=tf)
                    tab_w = sum(meas.measure(g["label"]) + 18 for g in self._emoji_groups) + pad * 2
                except Exception:
                    tab_w = 380
                self._emoji_size = (min(560, max(grid_w + pad * 2, tab_w)),
                                    head_h + tab_h + grid_h + pad)
                self._emoji_head_h = head_h
                self._emoji_tab_h = tab_h
                self._emoji_pad = pad
                win.geometry(f"{self._emoji_size[0]}x{self._emoji_size[1]}")
                # 顶栏（Canvas 绘制：标题 + 锁定按钮，点击命中检测）
                tcv = _tk.Canvas(win, width=self._emoji_size[0], height=head_h,
                                 bg=C("panel"), highlightthickness=0)
                tcv.pack()
                self._emoji_title_cv = tcv
                tcv.bind("<Button-1>", self._on_emoji_titlebar_click)
                # 页签行（Canvas 绘制：选中高亮，点击切换分组）
                bcv = _tk.Canvas(win, width=self._emoji_size[0], height=tab_h,
                                 bg=C("panel"), highlightthickness=0)
                bcv.pack()
                self._emoji_tab_cv = bcv
                self._emoji_tab_btns = []  # 兼容旧引用：现由 Canvas 绘制
                bcv.bind("<Button-1>", self._on_emoji_tabbar_click)
                # 单个 Canvas：画当前分组全部表情
                cv = _tk.Canvas(win, width=grid_w, height=grid_h,
                                bg=C("panel"), highlightthickness=0)
                cv.pack(padx=pad, pady=(0, pad))
                self._emoji_cv = cv
                cv.bind("<Button-1>", self._on_emoji_canvas_click)
                cv.bind("<Motion>", self._on_emoji_canvas_hover)
                cv.bind("<Leave>", lambda e: cv.delete("emojihl"))
                self._emoji_items = []  # (em, x, y)
                self._emoji_drawn = -1  # 已绘制分组标记（-1 = 未绘制）
                self._draw_emoji_titlebar()
                self._draw_emoji_tabs()
                win.bind("<Escape>", lambda e: self._close_emoji_panel())
                win.bind("<FocusOut>", lambda e: self._on_emoji_focus_out())
                win.bind("<FocusIn>", lambda e: self._on_emoji_focus_in())
                win.withdraw()
            except Exception:
                try:
                    win.destroy()
                except Exception:
                    pass
                self._emoji_win = None
                return
        try:
            # 先移除上次展示的绑定（避免重复绑定增长提交时间）
            if getattr(self, "_emoji_root_bind", None) is not None:
                try:
                    self.root.unbind("<Button-1>", self._emoji_root_bind)
                except Exception:
                    pass
                self._emoji_root_bind = None
            self._cancel_emoji_focus_after()   # 取消上一次遗留的失焦关闭定时器
            self._emoji_opened_at = time.time()  # 刚打开瞬间的焦点抖动不视为"点到外部"
            win.deiconify()
            win.attributes("-topmost", True)
            # 定位：以面板真实渲染尺寸计算（不再用可能失真的缓存值），
            # 优先弹在 😊 按钮上方并右侧对齐，四边做屏幕边界检测，任何屏幕尺寸下都完整可见。
            try:
                win.update_idletasks()
                w = max(240, win.winfo_reqwidth())
                h = max(220, win.winfo_reqheight())
            except Exception:
                w, h = getattr(self, "_emoji_size", None) or (380, 360)
            try:
                bx = self.emoji_btn.winfo_rootx()
                by = self.emoji_btn.winfo_rooty()
                bw = self.emoji_btn.winfo_width()
            except Exception:
                bx = self.root.winfo_rootx() + self.root.winfo_width() - w - 24
                by = self.root.winfo_rooty() + self.root.winfo_height() - h - 130
                bw = 0
            # 屏幕工作区（Windows 下扣除任务栏）
            try:
                sw = win.winfo_screenwidth()
                sh = win.winfo_screenheight()
            except Exception:
                sw, sh = 1920, 1080
            taskbar = 48  # 任务栏高度预估
            x = bx + bw - w            # 右对齐按钮
            y = by - h - 8             # 按钮上方
            if y < 4:                  # 上方放不下 → 按钮下方
                y = by + self.emoji_btn.winfo_height() + 8
            if y + h > sh - taskbar:   # 下方也放不下 → 贴屏幕底部
                y = max(4, sh - taskbar - h)
            if x < 4:                  # 左边界
                x = 4
            if x + w > sw - 4:         # 右边界
                x = max(4, sw - 4 - w)
            win.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")
            self._emoji_hidden = False
            # 绘制延后一帧：先显示空面板，表情后台逐步绘制，开启不卡
            if getattr(self, "_emoji_drawn", -1) != getattr(self, "_emoji_group_idx", 0):
                self.root.after(60, lambda: self._draw_emoji_group(self._emoji_group_idx))
            # 显示期间监听主窗口点击：点击主界面（非面板）即关闭（仅当次显示，用完移除）
            self._emoji_root_bind = self.root.bind("<Button-1>", self._on_click_outside, add="+")
            win.focus_force()
        except Exception:
            pass

    def _on_emoji_focus_out(self):
        """面板失去焦点：未锁定时延迟关闭（点击别处 / 按 Alt+Tab 等）。

        修复「点击表情分区页签也会关闭面板」：打开瞬间的焦点归还、以及
        焦点在面板内部控件间转移（点页签/锁定按钮）产生的 FocusOut 都是
        抖动，不关闭；真正失焦到外部时才延迟 120ms 关闭，且定时器可取消，
        焦点回到面板 / 面板内交互时立即取消。"""
        if getattr(self, "_emoji_locked", False):
            return
        try:
            # 刚打开瞬间（focus_force + 焦点归还）的 FocusOut 是系统焦点抖动，忽略
            if time.time() - getattr(self, "_emoji_opened_at", 0.0) < 0.8:
                return
            # 焦点仍在面板内部（点击页签、锁定按钮等）不算「点到外部」
            foc = self.root.focus_get()
            if foc is not None:
                w = foc
                win = getattr(self, "_emoji_win", None)
                while w is not None:
                    if win is not None and w is win:
                        return
                    w = getattr(w, "master", None)
            self._cancel_emoji_focus_after()
            self._emoji_focus_after = self.root.after(120, self._close_emoji_panel)
        except Exception:
            pass

    def _on_emoji_focus_in(self, event=None):
        """焦点回到面板：取消待执行的关闭（点页签/点表情后焦点弹回时）。"""
        self._cancel_emoji_focus_after()

    def _cancel_emoji_focus_after(self):
        if getattr(self, "_emoji_focus_after", None) is not None:
            try:
                self.root.after_cancel(self._emoji_focus_after)
            except Exception:
                pass
            self._emoji_focus_after = None

    def _draw_emoji_group(self, gi):
        """在 Canvas 上绘制当前分组的表情。优化：
        缓存 emoji 字体对象（首次加载字体最慢）、
        图组已绘制则跳过，切换时免重复绘制。"""
        try:
            if getattr(self, "_emoji_drawn", -1) == gi:
                return
            cv = getattr(self, "_emoji_cv", None)
            if cv is None:
                return
            self._emoji_font = getattr(self, "_emoji_font", None) or ("Segoe UI Emoji", 15)
            cv.delete("all")
            cv.delete("emojihl")
            groups = getattr(self, "_emoji_groups", None) or EMOJI_GROUPS
            g = groups[gi] if 0 <= gi < len(groups) else groups[0]
            cell = getattr(self, "_emoji_cell", 36)
            cols = getattr(self, "_emoji_cols", 10)
            self._emoji_items = []
            for i, em in enumerate(g["items"]):
                rr, c = divmod(i, cols)
                x = 4 + c * cell + cell // 2
                y = 4 + rr * cell + cell // 2
                cv.create_text(x, y, text=em, font=self._emoji_font, fill=C("text"))
                self._emoji_items.append((em, x, y))
            self._emoji_drawn = gi
            # 高度自适应：按当前分组的实际行数收缩/伸展面板（紧凑不空旷）
            try:
                import math as _m
                rows = max(1, _m.ceil(len(g["items"]) / cols))
                new_grid_h = rows * cell + 8
                if int(cv.cget("height")) != new_grid_h:
                    cv.configure(height=new_grid_h)
                    win = getattr(self, "_emoji_win", None)
                    if win is not None and win.winfo_exists():
                        sz = getattr(self, "_emoji_size", (380, 360))
                        total_h = (getattr(self, "_emoji_head_h", 30)
                                   + getattr(self, "_emoji_tab_h", 30)
                                   + new_grid_h + getattr(self, "_emoji_pad", 6) * 2)
                        self._emoji_size = (sz[0], total_h)
                        win.geometry(f"{sz[0]}x{total_h}+{win.winfo_x()}+{win.winfo_y()}")
            except Exception:
                pass
        except Exception:
            pass

    def _on_emoji_canvas_click(self, event):
        """Canvas 点击命中检测：点到哪个表情就插入哪个。"""
        try:
            # 点击时尚未绘制（延迟绘制未赶上）：同步绘制再命中
            if not getattr(self, "_emoji_items", []):
                self._draw_emoji_group(getattr(self, "_emoji_group_idx", 0))
            for em, x, y in getattr(self, "_emoji_items", []) or []:
                if abs(event.x - x) <= 16 and abs(event.y - y) <= 16:
                    self._insert_emoji(em)
                    return
        except Exception:
            pass

    def _on_emoji_canvas_hover(self, event):
        """悬停高亮当前表情（单个矩形，无控件创建）。"""
        try:
            cv = getattr(self, "_emoji_cv", None)
            if cv is None:
                return
            for em, x, y in getattr(self, "_emoji_items", []) or []:
                if abs(event.x - x) <= 16 and abs(event.y - y) <= 16:
                    if getattr(self, "_emoji_hl", None) == (em, x, y):
                        return  # 高亮未变化，跳过重绘（Motion 高频触发优化）
                    self._emoji_hl = (em, x, y)
                    cv.delete("emojihl")
                    # 圆角高亮：中间矩形 + 两端椭圆帽
                    cv.create_oval(x - 17, y - 13, x - 5, y + 13,
                                   fill=C("hover"), outline="", tags="emojihl")
                    cv.create_oval(x + 5, y - 13, x + 17, y + 13,
                                   fill=C("hover"), outline="", tags="emojihl")
                    cv.create_rectangle(x - 11, y - 13, x + 11, y + 13,
                                        fill=C("hover"), outline="", tags="emojihl")
                    cv.tag_lower("emojihl")
                    return
            if getattr(self, "_emoji_hl", None) is not None:
                self._emoji_hl = None
                cv.delete("emojihl")
        except Exception:
            pass

    def _toggle_emoji_lock(self):
        """锁定/解锁表情面板：锁定时连续点多个表情不自动关闭。"""
        self._cancel_emoji_focus_after()
        self._emoji_locked = not getattr(self, "_emoji_locked", False)
        try:
            self._draw_emoji_titlebar()  # 重绘锁图标（Canvas 顶栏）
        except Exception:
            pass
        self._set_status("表情面板已锁定，可连续插入多个表情" if self._emoji_locked
                         else "表情面板已解锁，插入后自动关闭", "ok")

    def _on_click_outside(self, event):
        """点击主窗口区域（非面板）：未锁定时关闭面板。这个绑定在展示时加在主窗口上，关闭时移除。"""
        if getattr(self, "_emoji_locked", False):
            return
        win = getattr(self, "_emoji_win", None)
        if win is None:
            return
        try:
            if not win.winfo_exists():
                return
            # 面板已隐藏则不处理
            if getattr(self, "_emoji_hidden", False):
                return
            self._close_emoji_panel()
        except Exception:
            pass

    def _close_emoji_panel(self):
        self._cancel_emoji_focus_after()
        self._emoji_hidden = True
        win = getattr(self, "_emoji_win", None)
        if win is not None:
            try:
                if win.winfo_exists():
                    win.withdraw()
            except Exception:
                self._emoji_win = None
        try:
            if getattr(self, "_emoji_root_bind", None) is not None:
                self.root.unbind("<Button-1>", self._emoji_root_bind)
                self._emoji_root_bind = None
        except Exception:
            pass

    def _draw_emoji_titlebar(self):
        """Canvas 绘制面板顶栏：左侧标题，右侧锁定按钮（圆角胶囊）。"""
        try:
            tcv = getattr(self, "_emoji_title_cv", None)
            if tcv is None:
                return
            tcv.delete("all")
            w = tcv.winfo_width()
            if w <= 1:
                w = getattr(self, "_emoji_size", (380, 360))[0]
            h = tcv.winfo_height()
            if h <= 1:
                h = getattr(self, "_emoji_head_h", 30)
            tcv.create_text(10, h // 2, text="✿ 表情（点击外部关闭）",
                            font=self._emoji_title_font, fill=C("text_mute"), anchor="w")
            # 底部品牌装饰线（与主窗口顶栏色条呼应）
            tcv.create_rectangle(0, h - 2, w, h, fill=C("accent"), outline="", width=0)
            # 锁定按钮：右侧圆角胶囊
            self._emoji_lock_box = (w - 64, 4, w - 8, h - 4)
            x1, y1, x2, y2 = self._emoji_lock_box
            locked = getattr(self, "_emoji_locked", False)
            tcv.create_rectangle(x1, y1, x2, y2,
                                 fill=(C("accent") if locked else C("input_bg")),
                                 outline="", width=0)
            tcv.create_text((x1 + x2) // 2, (y1 + y2) // 2,
                            text=("🔒" if locked else "🔓"),
                            font=(FONT, 10),
                            fill=("#ffffff" if locked else C("text_2")))
        except Exception:
            pass

    def _on_emoji_titlebar_click(self, event):
        """顶栏点击：命中锁定按钮区域则切换锁定状态。"""
        try:
            box = getattr(self, "_emoji_lock_box", None)
            if box and box[0] <= event.x <= box[2] and box[1] <= event.y <= box[3]:
                self._toggle_emoji_lock()
        except Exception:
            pass

    def _draw_emoji_tabs(self):
        """Canvas 绘制分类页签：选中组画 accent 圆角胶囊，其余平铺。"""
        try:
            bcv = getattr(self, "_emoji_tab_cv", None)
            if bcv is None:
                return
            bcv.delete("all")
            groups = getattr(self, "_emoji_groups", None) or []
            w = bcv.winfo_width()
            if w <= 1:
                w = getattr(self, "_emoji_size", (380, 360))[0]
            h = bcv.winfo_height()
            if h <= 1:
                h = getattr(self, "_emoji_tab_h", 30)
            try:
                import tkinter.font as _tkfont
                meas = _tkfont.Font(root=self.root, font=self._emoji_tab_font)
            except Exception:
                meas = None
            x = getattr(self, "_emoji_pad", 6)
            gi = getattr(self, "_emoji_group_idx", 0)
            self._emoji_tab_boxes = []
            for i, g in enumerate(groups):
                try:
                    tw = meas.measure(g["label"]) if meas else len(g["label"]) * 12
                except Exception:
                    tw = len(g["label"]) * 12
                bw = tw + 16
                if x + bw > w - 4:
                    break  # 超宽截断（分组过多时保面板宽度稳定）
                sel = (i == gi)
                if sel:
                    bcv.create_rectangle(x, 3, x + bw, h - 3,
                                         fill=C("accent"), outline="", width=0)
                    # 底部 accent 指示条（选中页签下划线）
                    bcv.create_rectangle(x + 6, h - 2, x + bw - 6, h,
                                         fill="#ffffff", outline="", width=0)
                bcv.create_text(x + bw // 2, h // 2, text=g["label"],
                                font=self._emoji_tab_font,
                                fill=("#ffffff" if sel else C("text")))
                self._emoji_tab_boxes.append((x, 0, x + bw, h, i))
                x += bw + 2
        except Exception:
            pass

    def _on_emoji_tabbar_click(self, event):
        """页签点击命中检测：点到哪个分组就切换到哪个。"""
        try:
            for x1, y1, x2, y2, gi in getattr(self, "_emoji_tab_boxes", []) or []:
                if x1 <= event.x <= x2:
                    self._switch_emoji_group(gi)
                    return
        except Exception:
            pass

    def _switch_emoji_group(self, gi):
        """切换表情分类页签：仅重绘 Canvas 文本，毫秒级。"""
        try:
            self._cancel_emoji_focus_after()  # 点页签是面板内交互，绝不关闭面板
            self._emoji_group_idx = gi
            self._draw_emoji_tabs()
            self._draw_emoji_group(gi)
        except Exception:
            pass

    def _insert_emoji(self, em):
        self._cancel_emoji_focus_after()
        try:
            if self._hint_active:
                self.input_box.delete("1.0", "end")
                self.input_box.configure(text_color=C("text"))
                self._hint_active = False
            self.input_box.insert("insert", em)
            self.input_box.focus_set()
        except Exception:
            pass
        # 插入后同步发送按钮状态（有内容点亮）
        try:
            self._update_send_btn_state()
        except Exception:
            pass
        # 记录最近使用（存设置，最多 24 个）
        try:
            rec = list(_load_settings().get("recent_emojis", []) or [])
            if em in rec:
                rec.remove(em)
            rec.insert(0, em)
            _update_settings("recent_emojis", rec[:24])
        except Exception:
            pass
        # 锁定时保持面板开着，方便一次点多个表情；否则插入后自动关闭
        if not getattr(self, "_emoji_locked", False):
            self._close_emoji_panel()

    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            return
        for p in paths or []:
            p = str(p or "").strip()
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
        # 大图智能压缩：超过阈值的图片自动压缩后再发，大幅减少流量、加快传输（原图保留本地）
        send_path = _auto_compress_image(path) if _is_image_path(path) else path
        if s["kind"] == "group":
            self.backend.send_file(s["room"], send_path)
        else:
            self.backend.send_file_dm(s["cid"], send_path)

    # --------------------------- 回调（切回主线程） ---------------------------

    def _cb_text(self, room, name, text, mine, mid=None, reply=None):
        self.root.after(0, lambda: self._append_message(self._group_key(room), name, text, mine, mid=mid, reply=reply))

    def _cb_peers(self, peers):
        self.root.after(0, lambda: self._refresh_peers(peers))

    def _cb_file(self, room, event, info):
        self.root.after(0, lambda: self._show_file_event(room, event, info))

    def _cb_status(self, online, msg):
        self.root.after(0, lambda: self._set_status(msg, "ok" if online else "err"))

    def _cb_dm(self, from_cid, name, text, mid=None, reply=None):
        self.root.after(0, lambda: self._receive_dm(from_cid, name, text, mid, reply))

    def _cb_read(self, room, mid, cid, name):
        self.root.after(0, lambda: self._receive_read(room, mid, cid, name))

    def _cb_recall(self, room, mid, who):
        self.root.after(0, lambda: self._receive_recall(room, mid, who))

    def _cb_typing(self, room, name, cid):
        self.root.after(0, lambda: self._receive_typing(room, name, cid))

    def _cb_delivered(self, room, mid, cid, name):
        self.root.after(0, lambda: self._receive_delivered(room, mid, cid, name))

    def _cb_edit(self, room, mid, who, text):
        self.root.after(0, lambda: self._receive_edit(room, mid, who, text))

    def _cb_reaction(self, room, mid, emoji, cid, name):
        self.root.after(0, lambda: self._receive_reaction(room, mid, emoji, cid, name))

    def _cb_lan_peers(self, peers):
        self.root.after(0, lambda: self._update_lan_peers(peers or {}))

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
            # 主标题保持与 _update_chat_title 一致的风格（群聊 🌸 前缀 / 私聊纯名），
            # “正在输入”显示在副标题，避免覆盖在线状态与群前缀
            if s is None:
                self.chat_title.configure(text="聊天")
            elif s["kind"] == "group":
                self.chat_title.configure(text=f"🌸 {s['name']}")
            else:
                self.chat_title.configure(text=s["name"])
            try:
                self.chat_sub.configure(text=f"{name} 正在输入…", text_color=C("accent"))
            except Exception:
                pass
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

    def _receive_dm(self, from_cid, name, text, mid=None, reply=None):
        s = self._ensure_dm_session(from_cid, name)
        s["online"] = True
        self._append_message(s["key"], name, text, False, mid=mid, reply=reply)
        self._schedule_session_list()

    def _footer_text(self, m):
        """计算消息底部（已读/已送达/已编辑）标签的文案与颜色。"""
        edited = " · 已编辑" if m.get("edited") else ""
        rb = m.get("read_by") or []
        db = m.get("delivered_by") or []
        if rb:
            names = "、".join(rb[:5])
            if len(rb) > 5:
                names += f" 等 {len(rb)} 人"
            return f"已读 {names}{edited}", C("accent")
        if db:
            names = "、".join(db[:5])
            if len(db) > 5:
                names += f" 等 {len(db)} 人"
            return f"已送达 {names}{edited}", C("text_mute")
        if m.get("edited"):
            return "已编辑", C("text_mute")
        return "", None

    def _refresh_reaction_row(self, mid, m):
        """重建某条消息的回应 badge 行。"""
        old = self._reaction_rows.pop(mid, None)
        if old is not None:
            try:
                old.destroy()
            except Exception:
                pass
        bubble = self._bubble_frames.get(mid)
        if bubble is None:
            return
        if m and m.get("reactions"):
            row = self._build_reaction_row(bubble, m.get("reactions"), mid)
            if row is not None:
                self._reaction_rows[mid] = row

    def _refresh_message_badge(self, mid):
        """局部刷新单条消息的正文/回执/回应，避免整页重渲染（性能优化）。"""
        if mid not in self._bubble_frames:
            self._schedule_feed_refresh()  # 兜底：气泡不在缓存里，整页刷新
            return
        s = self._sessions.get(self._current)
        m = self._find_msg(self._current, mid) if s else None
        if m is None:
            self._schedule_feed_refresh()
            return
        body = self._body_labels.get(mid)
        if body is not None:
            try:
                body.configure(text=str(m.get("text", "")))
            except Exception:
                pass
        lbl = self._footer_labels.get(mid)
        if lbl is not None:
            text, color = self._footer_text(m)
            try:
                if text:
                    lbl.configure(text=text, text_color=color)
                else:
                    lbl.destroy()
                    self._footer_labels.pop(mid, None)
            except Exception:
                pass
        self._refresh_reaction_row(mid, m)

    def _receive_reaction(self, room, mid, emoji, cid, name):
        """收到表情回应：切换该表情下某人的回应（再次发送=取消）。"""
        if not mid or not emoji or cid == self.cid:
            return
        if str(room).startswith("@"):
            key = self._dm_key(str(room)[1:])
        else:
            key = self._group_key(room)
        s = self._sessions.get(key)
        if s is None:
            return
        m = self._find_msg(key, mid)
        if m is not None:
            react = m.setdefault("reactions", {})
            bucket = react.setdefault(emoji, {})
            if cid in bucket:
                bucket.pop(cid, None)
                if not bucket:
                    react.pop(emoji, None)
            else:
                bucket[cid] = name or "匿名"
            self._save_session(s)
            if key == self._current:
                self._refresh_message_badge(mid)

    def _do_reaction(self, mid, emoji):
        """本地切换我的表情回应并广播。"""
        if not (self.backend and self.backend.online):
            self._set_status("未连接，无法回应", "err")
            return
        s = self._sessions.get(self._current)
        if s is None:
            return
        m = self._find_msg(self._current, mid)
        if m is None:
            return
        is_dm = s.get("kind") == "dm"
        target = s.get("cid") if is_dm else s.get("room")
        react = m.setdefault("reactions", {})
        bucket = react.setdefault(emoji, {})
        my_name = self.nick_var.get().strip() or "未命名"
        if self.cid in bucket:
            bucket.pop(self.cid, None)
            if not bucket:
                react.pop(emoji, None)
        else:
            bucket[self.cid] = my_name
        self._save_session(s)
        self.backend.send_reaction(target, mid, emoji, is_dm)
        self._refresh_message_badge(mid)

    def _receive_edit(self, room, mid, who, text):
        """收到编辑指令：更新对应消息的正文并标记“已编辑”。"""
        if not mid:
            return
        if str(room).startswith("@"):
            key = self._dm_key(str(room)[1:])
        else:
            key = self._group_key(room)
        s = self._sessions.get(key)
        if s is None:
            return
        m = self._find_msg(key, mid)
        if m is not None:
            m["text"] = str(text)[:MAX_TEXT]
            m["edited"] = True
            self._save_session(s)
            if key == self._current:
                self._refresh_message_badge(mid)

    def _do_edit(self, mid, new_text):
        """提交一次消息编辑（本地立即更新 + 广播）。"""
        new_text = (new_text or "").strip()
        if not new_text:
            self._set_status("编辑内容为空，已取消", "mute")
            return
        if not (self.backend and self.backend.online):
            self._set_status("未连接，无法编辑", "err")
            return
        s = self._sessions.get(self._current)
        if s is None:
            return
        # 限时校验：仅自己的消息且发送 5 分钟内可编辑
        _tgt = self._find_msg(self._current, mid)
        if _tgt is None or not _tgt.get("mine"):
            self._set_status("只能编辑自己发送的消息", "err")
            return
        try:
            _age = time.time() - float(_tgt.get("ts") or 0)
        except Exception:
            _age = 0
        if _age > EDIT_WINDOW:
            self._set_status("已超过可编辑时间（仅发送 5 分钟内）", "err")
            return
        is_dm = s.get("kind") == "dm"
        target = s.get("cid") if is_dm else s.get("room")
        if self.backend.send_edit(target, mid, new_text, is_dm):
            _em = self._find_msg(self._current, mid)
            if _em is not None:
                _em["text"] = new_text
                _em["edited"] = True
                self._save_session(s)
            self._render_feed()
            self._set_status("已编辑", "ok")
        else:
            self._set_status("编辑失败", "err")

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
        m = self._find_msg(key, mid)
        if m is not None and m.get("mine"):
            names = m.setdefault("delivered_by", [])
            if name and name not in names:
                names.append(name)
            self._save_session(s)
            if key == self._current:
                self._refresh_message_badge(mid)

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
        m = self._find_msg(key, mid)
        if m is not None and m.get("mine"):
            names = m.setdefault("read_by", [])
            if name and name not in names:
                names.append(name)
            self._save_session(s)
            if key == self._current:
                self._refresh_message_badge(mid)

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
        m = self._find_msg(key, mid)
        if m is not None:
            if m.get("recalled"):
                return
            m["recalled"] = True
            m["recalled_by"] = who or "对方"
            self._save_session(s)
            if key == self._current:
                self._schedule_feed_refresh()

    def _do_recall(self, mid):
        """右键撤回自己的消息。"""
        if not (self.backend and self.backend.online):
            self._set_status("未连接，无法撤回", "err")
            return
        s = self._sessions.get(self._current)
        if s is None:
            return
        # 限时校验：仅自己的消息且发送 2 分钟内可撤回
        _tgt = self._find_msg(self._current, mid)
        if _tgt is None or not _tgt.get("mine"):
            self._set_status("只能撤回自己发送的消息", "err")
            return
        try:
            _age = time.time() - float(_tgt.get("ts") or 0)
        except Exception:
            _age = 0
        if _age > RECALL_WINDOW:
            self._set_status("已超过可撤回时间（仅发送 2 分钟内）", "err")
            return
        is_dm = s.get("kind") == "dm"
        target = s.get("cid") if is_dm else s.get("room")
        if self.backend.send_recall(target, mid, is_dm):
            self._set_status("已撤回", "ok")
            _rm = self._find_msg(self._current, mid)
            if _rm is not None:
                _rm["recalled"] = True
                _rm["recalled_by"] = "我"
                self._save_session(s)
            self._render_feed()
        else:
            self._set_status("撤回失败", "err")

    def _start_reply(self, name, text, mid=None):
        """进入引用回复状态：显示回复栏并聚焦输入框。"""
        self._reply_to = {"name": str(name or "")[:20],
                          "text": str(text or "").replace("\n", " ")[:60],
                          "mid": mid or None}
        for w in self.reply_bar.winfo_children():
            w.destroy()
        try:
            self.reply_bar.configure(fg_color=C("warn_bg"), corner_radius=10)
        except Exception:
            pass
        # 左侧 accent 竖条（QQ 引用块样式）
        ctk.CTkFrame(self.reply_bar, width=3, height=30, corner_radius=2,
                     fg_color=C("accent")).pack(side="left", padx=(10, 8), pady=8)
        ctk.CTkLabel(self.reply_bar, text=f"↩ 回复 {self._reply_to['name']}：{self._reply_to['text']}",
                     text_color=C("warn_text"), font=(FONT, 10), anchor="w",
                     justify="left", wraplength=480).pack(side="left", pady=8)
        ctk.CTkButton(self.reply_bar, text="✕", width=24, height=24, corner_radius=8,
                      fg_color="transparent", text_color=C("warn_text"),
                      hover_color=C("input_hover"), font=(FONT, 11),
                      command=self._cancel_reply).pack(side="right", padx=8)
        try:
            self.reply_bar.pack(fill="x", padx=8, pady=(4, 0), before=self._ibar)
        except Exception:
            self.reply_bar.pack(fill="x", padx=8, pady=(4, 0))
        try:
            self.input_box.focus_set()
        except Exception:
            pass

    def _cancel_reply(self):
        self._reply_to = None
        try:
            self.reply_bar.pack_forget()
        except Exception:
            pass

    def _is_pinned(self, mid):
        s = self._sessions.get(self._current)
        if not s:
            return False
        for m in s.get("messages", []):
            if m.get("mid") == mid:
                return bool(m.get("pinned"))
        return False

    def _toggle_pin(self, mid):
        """置顶 / 取消置顶一条消息。"""
        s = self._sessions.get(self._current)
        if s is None:
            return
        for m in s.get("messages", []):
            if m.get("mid") == mid:
                m["pinned"] = not m.get("pinned", False)
                self._save_session(s)
                self._render_feed()
                self._set_status("已置顶" if m["pinned"] else "已取消置顶", "ok")
                return

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

    def _update_lan_peers(self, peers):
        """后台自动发现同网段成员后更新界面。"""
        self._lan_peers = peers
        self._refresh_status_bar()
        self._refresh_members()

    def _refresh_status_bar(self):
        if self.backend and self.backend.online:
            total = len(self._peers)
            host = getattr(self, "broker", "") or DEFAULT_BROKER
            self._set_status(f"已连接 {host} · {len(self._rooms)} 个房间 · 共 {total} 人在线", "ok")
        else:
            self._set_status("未连接", "mute")

    def _refresh_peers(self, peers):
        self._peers = peers or {}
        # 防抖：presence 连串更新（多通道同时到达/多人同时上线）合并为一次界面刷新，
        # 避免每收到一条在线状态就重建成员面板/会话列表导致卡顿
        if getattr(self, "_peers_after", None) is not None:
            try:
                self.root.after_cancel(self._peers_after)
            except Exception:
                pass
        self._peers_after = self.root.after(200, self._apply_peers_ui)

    def _apply_peers_ui(self):
        try:
            self._peers_after = None
            for s in self._sessions.values():
                if s["kind"] == "dm":
                    s["online"] = s["cid"] in self._peers
                    p = self._peers.get(s["cid"])
                    if p and p.get("name") and not s.get("local_alias"):
                        # 对方改了昵称（presence 更新）→ 同步会话显示名
                        if s.get("name") != p["name"]:
                            s["name"] = str(p["name"])[:40]
            self._schedule_session_list()
            self._update_chat_title()
            self._refresh_members(debounce=True)  # presence 批量更新合并刷新
            self._refresh_status_bar()
        except Exception:
            pass

    # --------------------------- 界面更新 ---------------------------

    def _is_muted(self, key):
        return key in self._muted

    def _is_pinned_session(self, key):
        return key in self._pinned_sessions

    def _toggle_pin_session(self, key):
        if key in self._pinned_sessions:
            self._pinned_sessions.discard(key)
            self._set_status("已取消置顶", "ok")
        else:
            self._pinned_sessions.add(key)
            self._set_status("已置顶该会话", "ok")
        _update_settings("pinned_sessions", sorted(self._pinned_sessions))
        self._last_list_fp = None
        self._apply_session_list()

    def _toggle_mute(self, key):
        if key in self._muted:
            self._muted.discard(key)
            self._set_status("已取消静音", "ok")
        else:
            self._muted.add(key)
            self._set_status("已静音该会话", "ok")
        _update_settings("muted_sessions", sorted(self._muted))
        self._last_list_fp = None
        self._apply_session_list()

    def _rename_session(self, key):
        """重命名会话显示名（本地备注，不广播）。"""
        try:
            s = self._sessions.get(key)
            if s is None:
                return
            from tkinter import simpledialog
            cur = s.get("local_alias") or s.get("name") or ""
            v = simpledialog.askstring("重命名显示名",
                                       "输入新的显示名（仅本机生效，留空恢复原名称）：",
                                       initialvalue=cur)
            if v is None:
                return
            v = v.strip()
            if v:
                s["local_alias"] = v[:30]
                s["name"] = v[:30]
            else:
                s.pop("local_alias", None)
                if s["kind"] == "group":
                    s["name"] = s.get("room", "群聊")
                else:
                    p = self._peers.get(s.get("cid"))
                    s["name"] = (p.get("name") if p else s.get("name") or "对方")[:40]
            self._last_list_fp = None
            self._apply_session_list()
            if key == self._current:
                self._update_chat_title()
            self._set_status("已重命名显示名" if v else "已恢复原名称", "ok")
        except Exception:
            pass

    def _restore_status(self):
        """把状态栏恢复为连接状态（悬停显示时间后调用）。"""
        self._refresh_status_bar()

    def _set_status(self, msg, color="mute"):
        # color 支持语义键（mute/ok/err/accent）或直接传十六进制色值
        if isinstance(color, str) and color in THEMES.get(_APPEARANCE, THEMES["dark"]):
            color = C(color)
        self.status_var.set(msg)
        self.status_label.configure(text_color=color)
        # 顶栏连接状态同步（圆点 + 文本）
        try:
            on = bool(self.backend and self.backend.online)
            dot = "●" if on else "○"
            col = C("online") if on else C("text_mute")
            self.conn_lbl.configure(text=f"{dot} {msg}", text_color=col)
        except Exception:
            pass
        self._update_window_title()

    def _update_window_title(self):
        # 窗口标题实时反映连接状态 + 未读消息数（无变化时不重复设置，避免频繁重绘）
        try:
            if self.backend and self.backend.online:
                base = f"P2P 聊天 · 已连接 · {len(self._peers)} 人在线"
            else:
                base = "P2P 聊天 · 未连接"
            unread = self._unread_total
            if unread:
                base = f"● {base}  [{unread} 条未读]"
            if self.appearance == "anime":
                base = "🌸 " + base
            if base != self._last_title:
                self._last_title = base
                self.root.title(base)
            self._update_taskbar_badge()
        except Exception:
            pass

    def _update_taskbar_badge(self):
        """按总未读数更新任务栏图标角标（Windows 7+ 红色数字徽标）。"""
        if os.name != "nt":
            return
        try:
            n = self._unread_total
            if n == self._last_badge_n:
                return
            self._last_badge_n = n
            old = self._overlay_hicon
            hicon = _build_badge_icon(n) if n > 0 else 0
            _taskbar3_set_overlay(self.root.winfo_id(), hicon)
            if old and old != hicon:
                try:
                    import ctypes
                    ctypes.windll.user32.DestroyIcon(old)
                except Exception:
                    pass
            self._overlay_hicon = hicon
        except Exception:
            pass

    def _scroll_bottom_now(self):
        try:
            self._hide_new_msg_floating()
        except Exception:
            pass
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
        视口顶出底部，会误判成“用户在上翻”。
        用户上翻时新消息到达：底部显示“↓ 新消息”浮标，点击回到最新。"""
        if self._suppress_auto_scroll:
            return
        def _do():
            try:
                canvas = self.feed._parent_canvas
                canvas.update_idletasks()
                canvas.configure(scrollregion=canvas.bbox("all"))
                if self._stick_bottom:
                    canvas.yview_moveto(1.0)
                else:
                    # 累计新到达未读数（用户上翻期间）
                    self._float_unread = getattr(self, "_float_unread", 0) + 1
                    self._show_new_msg_floating(self._float_unread)
            except Exception:
                pass
        try:
            self.root.after(1, _do)
        except Exception:
            pass

    def _show_new_msg_floating(self, count=1):
        """底部“↓ 新消息”浮标：用户在上翻时新消息到达后可点击返回最新。
        count 为未读条数（多条时显示数量）。"""
        try:
            if getattr(self, "_new_msg_floating", None) is not None:
                # 已显示：更新条数文本
                try:
                    self._new_msg_floating.configure(
                        text=(f"↓ {count} 条新消息" if count > 1 else "↓ 新消息"))
                except Exception:
                    pass
                return
            host = self.feed.master
            btn = ctk.CTkButton(host, text=(f"↓ {count} 条新消息" if count > 1 else "↓ 新消息"),
                                height=28, corner_radius=14,
                                fg_color=C("accent"), hover_color=C("accent_hover"),
                                text_color="#ffffff", font=(FONT, 11, "bold"),
                                command=self._goto_newest)
            btn.place(relx=0.5, rely=0.94, anchor="s")
            self._new_msg_floating = btn
        except Exception:
            pass

    def _goto_newest(self):
        """点击浮标：回到最新并隐藏浮标。"""
        try:
            self._hide_new_msg_floating()
        except Exception:
            pass
        self._float_unread = 0
        self._scroll_bottom_now()
        self._stick_bottom = True

    def _hide_new_msg_floating(self):
        try:
            if getattr(self, "_new_msg_floating", None) is not None:
                try:
                    self._new_msg_floating.destroy()
                except Exception:
                    pass
                self._new_msg_floating = None
        except Exception:
            pass

    def _add_file_offer_card(self, key, room, info):
        # 在聊天区渲染一条需手动确认的文件请求卡片（不弹窗）
        if key != self._current:
            # 非当前会话：先标记提醒，切过去再点
            s = self._sessions.get(key)
            if s is not None:
                s["unread"] = s.get("unread", 0) + 1
                self._unread_total += 1
                self._apply_session_list()
                self._update_window_title()
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

    def _message_row(self, name, mine, show_head, highlight=False, hl_color=None):
        """创建带头像的消息行，返回气泡控件；头像仅在消息组首条显示，其余缩进对齐。
        highlight=True 时给气泡加高亮边框（@ 我的消息用 accent；搜索命中用 hl_color）。"""
        AV, GAP = 34, 8
        row = ctk.CTkFrame(self.feed, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(6 if show_head else 1))
        bcolor = hl_color or (C("accent") if highlight else None)
        if mine:
            if show_head:
                self._avatar_label(row, name, True, AV).pack(side="right")
            else:
                ctk.CTkFrame(row, width=AV + GAP, height=1, fg_color="transparent").pack(side="right")
            bubble = ctk.CTkFrame(row, corner_radius=R(14), fg_color=C("mine_bubble"),
                                   border_width=(2 if highlight else 0),
                                   border_color=bcolor)
            bubble.pack(side="right", padx=(0, GAP if show_head else 0))
        else:
            if show_head:
                self._avatar_label(row, name, False, AV).pack(side="left")
            else:
                ctk.CTkFrame(row, width=AV + GAP, height=1, fg_color="transparent").pack(side="left")
            _anime_edge = (1 if (_APPEARANCE == "anime" and not highlight) else 0)
            bubble = ctk.CTkFrame(row, corner_radius=R(14), fg_color=C("other_bubble"),
                                   border_width=(2 if highlight else _anime_edge),
                                   border_color=(bcolor if highlight else
                                                 (C("panel_2") if _anime_edge else None)))
            bubble.pack(side="left", padx=(GAP if show_head else 0, 0))
        return bubble

    def _add_bubble(self, name, text, mine, ts=None, show_head=True, file_path=None,
                     read_by=None, delivered_by=None, mid=None, recalled=False,
                     recalled_by=None, edited=False, reply=None, reactions=None,
                     search_hl=False):
        if recalled:
            label = "（已撤回）" if mine else f"（{recalled_by or '对方'} 撤回了一条消息）"
            self._render_system_line(label)
            return
        tstr = _fmt_time(ts) if ts else ""
        bubble = self._message_row(name, mine, show_head,
                                   highlight=self._mentions_me(text) or search_hl,
                                   hl_color=(C("search_hl") if search_hl and not self._mentions_me(text) else None))
        if mid:
            self._bubble_frames[mid] = bubble
            # 多选模式：点击气泡勾选（再点取消）
            if getattr(self, "_multi_mode", False):
                self._multi_frames[mid] = bubble
                bubble.configure(cursor="hand2")
                for _w in (bubble,):
                    _w.bind("<Button-1>", lambda e, m=mid: self._toggle_multi_select(m), add="+")
        if reply:
            rname = str(reply.get("name", ""))[:20]
            rtext = str(reply.get("text", "")).replace("\n", " ")[:40]
            rmid = reply.get("mid")
            rlbl = ctk.CTkLabel(bubble, text=f"↩ {rname}：{rtext}", text_color=C("text_mute"),
                                font=(FONT, 9), anchor="w", justify="left",
                                wraplength=440, cursor=("hand2" if rmid else ""))
            rlbl.pack(anchor="w", padx=12, pady=(6, 0))
            if rmid:
                rlbl.bind("<Button-1>", lambda e, m=rmid: self._jump_to_message(m))
        if show_head:
            head = ctk.CTkFrame(bubble, fg_color="transparent")
            head.pack(fill="x", padx=12, pady=(6, 0))
            ctk.CTkLabel(head, text=name, text_color=C("text_mute"),
                         font=(FONT, 10)).pack(side="left")
            if tstr:
                ctk.CTkLabel(head, text=tstr, text_color=C("text_mute"),
                             font=(FONT, 9)).pack(side="right")
        _long = (mid and len(str(text)) > 320 and mid not in self._expanded_msgs
                 and not self._search_query)
        body = ctk.CTkLabel(bubble, text=(str(text)[:320] + "…" if _long else text),
                            wraplength=460, justify="left",
                            text_color=(C("mine_text") if mine else C("other_text")),
                            font=(FONT, self._chat_font_size))
        body.pack(anchor="w", padx=12, pady=((2 if show_head else 6), 8))
        if _long:
            ctk.CTkButton(bubble, text="展开全文", width=84, height=22, corner_radius=6,
                          fg_color="transparent", text_color=C("accent"),
                          hover_color=C("hover"), font=(FONT, 10),
                          command=lambda m=mid, ft=str(text): self._expand_message(m, ft)
                          ).pack(anchor="w", padx=12, pady=(0, 4))
        if mid:
            self._body_labels[mid] = body
        body.bind("<Button-3>", lambda e, t=text, p=file_path: self._message_menu(e, t, p, mine=mine, mid=mid, name=name))
        body.bind("<Double-Button-1>", lambda e, t=text, n=name, m=mid: self._start_reply(n, t, m))
        bubble.bind("<Double-Button-1>", lambda e, t=text, n=name, m=mid: self._start_reply(n, t, m))
        # 消息内 @提及高亮：识别 @昵称 生成可点击标签（点击直接引用回复该成员）
        m_names = self._mention_names()
        mentions = _extract_mentions(text, m_names)
        if mentions:
            row_m = ctk.CTkFrame(bubble, fg_color="transparent")
            row_m.pack(anchor="w", padx=12, pady=(0, 2))
            ctk.CTkLabel(row_m, text="提及 ", text_color=C("text_mute"),
                         font=(FONT, 9)).pack(side="left")
            for mn in mentions[:4]:
                tag = ctk.CTkLabel(row_m, text="@" + mn, text_color=C("accent"),
                                   font=(FONT, 9, "bold"), cursor="hand2")
                tag.pack(side="left", padx=(0, 4))
                tag.bind("<Button-1>", lambda e, mn=mn: self._start_reply(mn, str(text)[:60]))
        # 消息内链接：识别 URL 生成可点击标签（QQ/微信/Discord 风格，点击浏览器打开）
        urls = _extract_urls(text)
        if urls:
            for u in urls:
                link = ctk.CTkLabel(bubble, text=u, wraplength=460, justify="left",
                                    text_color=C("accent"), font=(FONT, 12, "underline"),
                                    cursor="hand2")
                link.pack(anchor="w", padx=12, pady=(0, 6))
                link.bind("<Button-1>", lambda e, u=u: _open_url(u))
                link.bind("<Button-3>", lambda e, t=u: self._message_menu(e, t, None, mine=mine, mid=mid, name=name))
        # 搜索命中片段高亮：显示关键词上下文，一眼看到命中位置
        if search_hl and self._search_query:
            _snip = _extract_search_snippet(text, self._search_query)
            if _snip:
                snip_lbl = ctk.CTkLabel(
                    bubble, text="🔍 " + _snip, wraplength=460, justify="left",
                    font=(FONT, 10, "bold"),
                    fg_color=C("search_hl"), corner_radius=6,
                    text_color=C("warn_text"))
                snip_lbl.pack(anchor="w", padx=12, pady=(0, 6))
        if file_path:
            # QQ 式：点击文件消息直接打开/下载
            def _open_file(_e, p=file_path):
                self._open_file_location(p)
            for w in (bubble, body):
                w.bind("<Button-1>", _open_file)
                w.configure(cursor="hand2")
            # 一键打开所在文件夹（免右键）
            frow = ctk.CTkFrame(bubble, fg_color="transparent")
            frow.pack(anchor="w", padx=12, pady=(0, 6))
            ctk.CTkButton(frow, text="📂 打开文件夹", width=96, height=24, corner_radius=6,
                          fg_color=C("input_bg"), text_color=C("text_2"),
                          hover_color=C("input_hover"), font=(FONT, 10),
                          command=lambda p=file_path: self._open_file_location(p)).pack(side="left")
        if ts:
            ft = _fmt_full_time(ts)
            def _enter(_e, ft=ft): self._set_status(f"{name} · {ft}", "mute")
            def _leave(_e): self._restore_status()
            for w in (bubble, body):
                w.bind("<Enter>", _enter)
                w.bind("<Leave>", _leave)
        footer = None
        if mine and read_by:
            names = "、".join(read_by[:5])
            if len(read_by) > 5:
                names += f" 等 {len(read_by)} 人"
            footer = ctk.CTkLabel(bubble, text=f"已读 {names}" + (" · 已编辑" if edited else ""),
                                  text_color=C("accent"), font=(FONT, 9))
        elif mine and delivered_by:
            names = "、".join(delivered_by[:5])
            if len(delivered_by) > 5:
                names += f" 等 {len(delivered_by)} 人"
            footer = ctk.CTkLabel(bubble, text=f"已送达 {names}" + (" · 已编辑" if edited else ""),
                                  text_color=C("text_mute"), font=(FONT, 9))
        elif edited:
            footer = ctk.CTkLabel(bubble, text="已编辑", text_color=C("text_mute"),
                                  font=(FONT, 9))
        if footer is not None:
            footer.pack(anchor="e", padx=12, pady=(0, 4))
            if mid:
                self._footer_labels[mid] = footer
            if mid and (read_by or delivered_by):
                # 点击「已读/已送达」标签 → 消息详情（谁已读 / 谁回应了）
                footer.configure(cursor="hand2")
                footer.bind("<Button-1>", lambda e, m=mid: self._show_message_details(m))
        if reactions:
            row = self._build_reaction_row(bubble, reactions, mid)
            if mid:
                self._reaction_rows[mid] = row
        if mid:
            # 悬停快捷操作浮层（Discord/QQ 式）：👍 回应 / ↩ 引用 / ⧉ 转发 / 📋 复制
            # 文件消息额外传 path → 浮层提供 📂 打开位置 / 📋 复制路径
            _hp = file_path if (file_path and os.path.isfile(file_path)) else None
            for _w in (bubble, body):
                _w.bind("<Enter>", lambda e, m=mid, b=bubble, n=name, t=text, pp=_hp:
                        self._hover_enter(m, b, n, t, pp), add="+")
                _w.bind("<Leave>", lambda e, m=mid: self._hover_leave(m), add="+")
        self._maybe_scroll_bottom()
        self._trim_feed()

    # ------------------------- 回应芯片 / 消息详情 / 悬停快捷操作 -------------------------

    def _build_reaction_row(self, bubble, react, mid):
        """回应芯片行：每个芯片可点击切换「我」的回应（我回应过的高亮 accent），
        行尾 "+" 打开快捷表情面板；右键芯片查看该表情回应名单。"""
        try:
            row = ctk.CTkFrame(bubble, fg_color="transparent")
            react = react or {}
            for emo, bucket in react.items():
                n = len(bucket) if isinstance(bucket, dict) else int(bucket)
                if n <= 0:
                    continue
                mine_r = isinstance(bucket, dict) and self.cid in bucket
                chip = ctk.CTkButton(
                    row, text=f"{emo} {n}", height=24, corner_radius=12,
                    fg_color=(C("accent") if mine_r else C("input_bg")),
                    hover_color=(C("accent_hover") if mine_r else C("input_hover")),
                    text_color=("#ffffff" if mine_r else C("text")),
                    font=(FONT, 10),
                    command=lambda e=emo: self._do_reaction(mid, e))
                chip.pack(side="left", padx=2, pady=2)
                chip.bind("<Button-3>", lambda e, emo=emo: self._show_message_details(mid, emo))
            plus = ctk.CTkButton(row, text="＋", width=30, height=24, corner_radius=12,
                                 fg_color=C("input_bg"), hover_color=C("input_hover"),
                                 text_color=C("text_2"), font=(FONT, 12),
                                 command=lambda: self._show_quick_reactions(
                                     mid, self.root.winfo_pointerx(), self.root.winfo_pointery()))
            plus.pack(side="left", padx=(4, 2), pady=2)
            return row
        except Exception:
            return None

    def _show_quick_reactions(self, mid, x, y):
        """快捷表情回应面板：在鼠标上方弹出，点一下即回应（可连续点不同表情）。"""
        try:
            win = ctk.CTkToplevel(self.root)
            win.overrideredirect(True)
            win.configure(fg_color=C("panel_2"))
            win.attributes("-topmost", True)
            for emo in ["👍", "❤️", "😂", "😮", "😢", "🙏", "🔥", "🎉"]:
                b = ctk.CTkButton(win, text=emo, width=34, height=34, corner_radius=17,
                                  fg_color="transparent", hover_color=C("hover"),
                                  text_color=C("text"), font=(FONT, 16),
                                  command=lambda e=emo: self._quick_react(mid, e, win))
                b.pack(side="left", padx=2, pady=4)
            win.bind("<Escape>", lambda e: win.destroy())
            win.bind("<FocusOut>", lambda e: win.destroy())
            win.update_idletasks()
            w = win.winfo_reqwidth()
            h = win.winfo_reqheight()
            sw = win.winfo_screenwidth()
            px = min(max(x - w // 2, 4), sw - w - 4)
            py = y - h - 12
            if py < 4:
                py = y + 12
            win.geometry(f"{w}x{h}+{int(px)}+{int(py)}")
            win.focus_set()
        except Exception:
            pass

    def _quick_react(self, mid, emo, win):
        try:
            win.destroy()
        except Exception:
            pass
        self._do_reaction(mid, emo)

    def _show_message_details(self, mid, focus_emoji=None):
        """消息详情弹窗：已读 / 已送达名单 + 各表情回应名单（点已读标签或右键回应芯片打开）。"""
        try:
            s = self._sessions.get(self._current)
            if s is None:
                return
            m = self._find_msg(self._current, mid)
            if m is None:
                return
            win = ctk.CTkToplevel(self.root)
            win.title("消息详情")
            win.geometry("340x430")
            win.resizable(False, False)
            win.attributes("-topmost", True)
            ctk.CTkLabel(win, text="消息详情", font=(FONT, 14, "bold"),
                         text_color=C("text")).pack(pady=(14, 4))
            body = ctk.CTkScrollableFrame(win, fg_color="transparent")
            body.pack(fill="both", expand=True, padx=16, pady=(0, 10))
            shown = False
            # 元信息：发送者 / 时间 / 状态
            try:
                _who = str(m.get("name", "?")) + ("（我）" if m.get("mine") else "")
                _when = _fmt_full_time(m.get("ts")) if m.get("ts") else ""
                _extra = []
                if m.get("edited"):
                    _extra.append("已编辑")
                if m.get("recalled"):
                    _extra.append("已撤回")
                if m.get("pinned"):
                    _extra.append("置顶")
                meta = f"{_who} · {_when}" + (" · " + " / ".join(_extra) if _extra else "")
                ctk.CTkLabel(body, text=meta, anchor="w", justify="left",
                             wraplength=290, font=(FONT, 10, "bold"),
                             text_color=C("text_2")).pack(anchor="w", pady=(0, 4))
                if m.get("mid"):
                    ctk.CTkLabel(body, text=f"ID: {m['mid'][:16]}…", anchor="w",
                                 font=(FONT, 9), text_color=C("text_mute")).pack(anchor="w", pady=(0, 2))
            except Exception:
                pass
            rb = m.get("read_by") or []
            if rb:
                shown = True
                ctk.CTkLabel(body, text=f"✅ 已读（{len(rb)} 人）", anchor="w",
                             font=(FONT, 11, "bold"), text_color=C("online")).pack(anchor="w", pady=(8, 2))
                ctk.CTkLabel(body, text="、".join(str(x) for x in rb), wraplength=290,
                             justify="left", font=(FONT, 11), text_color=C("text"),
                             anchor="w").pack(anchor="w", pady=(0, 6))
            db = m.get("delivered_by") or []
            if db:
                shown = True
                ctk.CTkLabel(body, text=f"📤 已送达（{len(db)} 人）", anchor="w",
                             font=(FONT, 11, "bold"), text_color=C("text_mute")).pack(anchor="w", pady=(8, 2))
                ctk.CTkLabel(body, text="、".join(str(x) for x in db), wraplength=290,
                             justify="left", font=(FONT, 11), text_color=C("text"),
                             anchor="w").pack(anchor="w", pady=(0, 6))
            react = m.get("reactions") or {}
            if react:
                shown = True
                for emo, bucket in react.items():
                    n = len(bucket) if isinstance(bucket, dict) else int(bucket)
                    if n <= 0:
                        continue
                    names = list(bucket.values()) if isinstance(bucket, dict) else [str(bucket)]
                    if isinstance(bucket, dict) and self.cid in bucket:
                        names = [f"{x}（我）" for x in names]
                    ctk.CTkLabel(body, text=f"{emo}  {n}", anchor="w",
                                 font=(FONT, 11, "bold"), text_color=C("accent")).pack(anchor="w", pady=(8, 2))
                    ctk.CTkLabel(body, text="、".join(str(x) for x in names), wraplength=290,
                                 justify="left", font=(FONT, 11), text_color=C("text"),
                                 anchor="w").pack(anchor="w", pady=(0, 6))
            if not shown:
                ctk.CTkLabel(body, text="（暂无已读 / 回应信息）", text_color=C("text_mute"),
                             font=(FONT, 11)).pack(pady=20)
            win.bind("<Escape>", lambda e: win.destroy())
        except Exception:
            pass

    # ---- 悬停快捷操作浮层（同时只存在一个，懒创建，离开自动隐藏） ----

    def _hover_enter(self, mid, bubble, name, text, path=None):
        try:
            self._hover_inside = True
            self._cancel_hover_after()
            if self._hover_bar is not None and self._hover_mid == mid:
                return  # 已显示同一消息的浮层
            self._hover_mid = mid
            self._hover_after = self.root.after(
                180, lambda m=mid, b=bubble, n=name, t=text, p=path: self._show_hover_bar(m, b, n, t, p))
        except Exception:
            pass

    def _hover_leave(self, mid):
        try:
            self._hover_inside = False
            self._cancel_hover_after()
            if self._hover_bar is not None and self._hover_mid == mid:
                self._hover_after = self.root.after(250, self._destroy_hover_bar)
        except Exception:
            pass

    def _show_hover_bar(self, mid, bubble, name, text, path=None):
        """在气泡外侧显示快捷操作浮层（QQ/Discord 式）：
        自己消息→气泡左侧、对方消息→气泡右侧，浮层与气泡顶边对齐；
        按钮：👍 回应 / ↩ 引用 / ⧉ 转发 / 🔊 朗读 / 📋 复制；
        path 非空（图片消息）额外提供 🔍 查看大图 / 💾 保存。"""
        try:
            if not getattr(self, "_hover_inside", False):
                return
            if getattr(self, "_multi_mode", False):
                return
            if bubble is None:
                return
            try:
                if not bubble.winfo_exists():
                    return
            except Exception:
                return
            self._destroy_hover_bar(keep_state=True)
            is_mine = bool(self._body_labels.get(mid)) and self._is_mine_bubble(mid)
            bar = ctk.CTkFrame(bubble, corner_radius=8, fg_color=C("panel_2"),
                               border_width=1, border_color=C("hover"))
            self._hover_bar = bar
            self._hover_mid = mid
            btns = [("👍", lambda: self._hover_react(mid)),
                    ("↩", lambda: self._hover_reply(name, text, mid)),
                    ("⧉", lambda: self._hover_forward(mid, text)),
                    ("📋", lambda: self._hover_copy(text))]
            if (text or "").strip():
                btns.insert(3, ("🔊", lambda: self._hover_speak(text)))
            if path and os.path.isfile(path):
                if _is_image_path(path):
                    btns.insert(0, ("🔍", lambda: self._hover_view_image(path)))
                    btns.insert(1, ("💾", lambda: self._hover_save_image(path)))
                else:
                    btns.insert(0, ("📂", lambda: self._hover_open_folder(path)))
                    btns.insert(1, ("📋", lambda: self._hover_copy_path(path)))
            for t, cmd in btns:
                b = ctk.CTkButton(bar, text=t, width=28, height=24, corner_radius=6,
                                  fg_color="transparent", hover_color=C("hover"),
                                  text_color=C("text"), font=(FONT, 12), command=cmd)
                b.pack(side="left", padx=1, pady=1)
            # 紧贴气泡外侧放置：自己消息靠右对齐（气泡在右），浮层放其左外侧；
            # 对方消息气泡在左，浮层放其右外侧。place 相对 bubble，保证随气泡移动。
            if is_mine:
                bar.place(relx=0.0, x=-8, y=0, anchor="ne")
            else:
                bar.place(relx=1.0, x=8, y=0, anchor="nw")
            bar.bind("<Enter>", lambda e: self._hover_bar_enter())
            bar.bind("<Leave>", lambda e: self._hover_bar_leave())
            for _b in bar.winfo_children():
                _b.bind("<Enter>", lambda e: self._hover_bar_enter(), add="+")
                _b.bind("<Leave>", lambda e: self._hover_bar_leave(), add="+")
        except Exception:
            pass

    def _is_mine_bubble(self, mid):
        """根据本地会话数据判断某消息是否是自己发的（浮层放左右哪侧用）。"""
        try:
            s = self._sessions.get(self._current)
            if s:
                m = self._find_msg(self._current, mid)
                if m is not None:
                    return bool(m.get("mine"))
        except Exception:
            pass
        return False

    def _hover_bar_enter(self):
        try:
            self._hover_inside = True
            self._cancel_hover_after()
        except Exception:
            pass

    def _hover_bar_leave(self):
        try:
            self._hover_inside = False
            self._cancel_hover_after()
            self._hover_after = self.root.after(250, self._destroy_hover_bar)
        except Exception:
            pass

    def _cancel_hover_after(self):
        if getattr(self, "_hover_after", None) is not None:
            try:
                self.root.after_cancel(self._hover_after)
            except Exception:
                pass
            self._hover_after = None

    def _destroy_hover_bar(self, keep_state=False):
        try:
            self._cancel_hover_after()
        except Exception:
            pass
        bar = self._hover_bar
        self._hover_bar = None
        if not keep_state:
            self._hover_mid = None
        if bar is not None:
            try:
                bar.destroy()
            except Exception:
                pass

    def _hover_react(self, mid):
        try:
            x = self.root.winfo_pointerx()
            y = self.root.winfo_pointery()
            self._destroy_hover_bar()
            self._show_quick_reactions(mid, x, y)
        except Exception:
            pass

    def _hover_reply(self, name, text, mid):
        self._destroy_hover_bar()
        self._start_reply(name, text, mid)

    def _hover_forward(self, mid, text=""):
        """转发当前消息：直接弹出转发对话框（QQ 式单条转发）。"""
        self._destroy_hover_bar()
        s = self._sessions.get(self._current)
        if s is None:
            return
        m = self._find_msg(self._current, mid)
        items = []
        if m is not None:
            p = None
            if m.get("img_path") and os.path.isfile(m["img_path"]):
                p = m["img_path"]
            elif m.get("file_path") and os.path.isfile(m["file_path"]):
                p = m["file_path"]
            if p:
                items.append({"type": "file", "path": p, "label": os.path.basename(p)})
            else:
                t = str(m.get("text", "") or "").strip()
                if t and not m.get("system"):
                    items.append(t)
        if not items:
            self._set_status("没有可转发的消息", "err")
            return
        self._forward_dialog(items)

    def _hover_copy(self, text):
        self._destroy_hover_bar()
        self._copy_to_clipboard(text)

    def _hover_speak(self, text):
        self._destroy_hover_bar()
        self._speak_text(text)

    def _hover_view_image(self, path):
        self._destroy_hover_bar()
        self._open_image(path)

    def _hover_save_image(self, path):
        self._destroy_hover_bar()
        self._save_image_dialog(path)

    def _hover_open_folder(self, path):
        self._destroy_hover_bar()
        self._open_file_location(path)

    def _hover_copy_path(self, path):
        self._destroy_hover_bar()
        self._copy_to_clipboard(path)

    def _mentions_me(self, text):
        """判断消息正文是否 @ 了我（用于高亮）。"""
        my = (self.nick_var.get().strip() or self._profile_name or "").strip()
        if not my:
            return False
        return f"@{my}" in str(text)

    def _add_voice_bubble(self, name, path, mine, ts=None, show_head=True, mid=None):
        """渲染语音消息气泡：显示时长，点击播放。"""
        tstr = _fmt_time(ts) if ts else ""
        bubble = self._message_row(name, mine, show_head)
        if mid:
            self._bubble_frames[mid] = bubble
        if show_head:
            head = ctk.CTkFrame(bubble, fg_color="transparent")
            head.pack(fill="x", padx=12, pady=(6, 0))
            ctk.CTkLabel(head, text=name, text_color=C("text_mute"),
                         font=(FONT, 10)).pack(side="left")
            if tstr:
                ctk.CTkLabel(head, text=tstr, text_color=C("text_mute"),
                             font=(FONT, 9)).pack(side="right")
        dur = 0
        try:
            if path in _VOICE_DUR_CACHE:
                dur = _VOICE_DUR_CACHE[path]
            else:
                import wave
                with wave.open(path, "rb") as wf:
                    dur = wf.getnframes() / float(wf.getframerate())
                _VOICE_DUR_CACHE[path] = dur
        except Exception:
            pass
        dur_txt = f"{dur:.0f}″" if dur > 0 else ""
        vbox = ctk.CTkFrame(bubble, fg_color="transparent")
        vbox.pack(fill="x", padx=12, pady=6)
        _vbtn = ctk.CTkButton(
            vbox, text=f"🎤 语音 {dur_txt}", width=130, height=34, corner_radius=16,
            fg_color=C("input_bg"), text_color=C("text"),
            hover_color=C("input_hover"), font=(FONT, 12))
        _vbtn.configure(command=lambda p=path, b=_vbtn: self._toggle_voice_play(p, btn=b))
        _vbtn.pack(anchor="w", side="left")
        self._voice_btns[path] = _vbtn
        # 倍速切换按钮（1x / 1.5x / 2x 循环）
        self._voice_speeds[path] = 0
        spd_btn = ctk.CTkButton(
            vbox, text="1x", width=38, height=34, corner_radius=10,
            fg_color="transparent", text_color=C("text_mute"),
            hover_color=C("input_hover"), font=(FONT, 10, "bold"),
            command=lambda p=path: self._cycle_voice_speed(p))
        spd_btn.pack(anchor="w", side="left", padx=(6, 0))
        self._voice_spd_btns[path] = spd_btn
        # 语音右键菜单：转发 / 打开位置
        _vbtn.bind(
            "<Button-3>",
            lambda e, p=path: self._voice_menu(e, p))
        # 播放进度条：点击播放时实时显示播放进度（QQ/Discord 风格）
        bar = ctk.CTkProgressBar(vbox, width=130, height=6, corner_radius=3,
                                 fg_color=C("input_bg"), progress_color=C("accent"))
        bar.set(0)
        bar.pack(anchor="w", pady=(4, 0))
        bar.pack_forget()  # 默认隐藏，播放时才显示
        self._voice_bars[path] = bar
        # 进度条与播放按钮一一对应：转发产生的同路径消息各持各的控件
        _vbtn._voice_bar = bar
        self._voice_durs[path] = dur
        if mid:
            vtxt = f"🎤 语音：{os.path.basename(path)}"
            self._bind_hover(mid, bubble, vbox, self._voice_btns[path], spd_btn,
                             name=name, text=vtxt)
        self._maybe_scroll_bottom()
        self._trim_feed()

    def _bind_hover(self, mid, *ws, name="", text="", path=None):
        """给一批控件绑定悬停快捷浮层 Enter/Leave（add="+" 与既有绑定共存）。
        path 非空时浮层额外提供 🔍 查看大图 / 💾 保存（图片消息用）。"""
        if not ws:
            return
        b0 = ws[0]
        for w in ws:
            try:
                w.bind("<Enter>", lambda e, m=mid, b=b0, n=name, t=text, p=path:
                       self._hover_enter(m, b, n, t, p), add="+")
                w.bind("<Leave>", lambda e, m=mid: self._hover_leave(m), add="+")
            except Exception:
                pass

    def _add_image_bubble(self, name, path, mine, ts=None, show_head=True, mid=None):
        tstr = _fmt_time(ts) if ts else ""
        if not (_HAS_PIL and path and os.path.isfile(path)):
            self._add_bubble(name, "🖼 一张图片", mine, ts, show_head, mid=mid)
            return
        try:
            cache_key = (path, int(os.path.getmtime(path)))
            ctk_img = self._thumb_cache.get(cache_key)
            bubble = self._message_row(name, mine, show_head)
            if mid:
                self._bubble_frames[mid] = bubble
            if show_head:
                head = ctk.CTkFrame(bubble, fg_color="transparent")
                head.pack(fill="x", padx=12, pady=(6, 2))
                ctk.CTkLabel(head, text=f"{name} · 图片", text_color=C("text_mute"),
                             font=(FONT, 10)).pack(side="left")
                if tstr:
                    ctk.CTkLabel(head, text=tstr, text_color=C("text_mute"),
                                 font=(FONT, 9)).pack(side="right")
            itxt = f"🖼 图片：{os.path.basename(path)}"
            if ctk_img is not None:
                self._images.append(ctk_img)
                self._pack_image_label(bubble, ctk_img, path, mid=mid, name=name, itxt=itxt)
            else:
                # 后台解码缩略图，UI 线程先显示占位（图片多时不卡界面）
                ph = ctk.CTkLabel(bubble, text="🖼 加载中…", width=140, height=96,
                                  fg_color=C("input_bg"), text_color=C("text_mute"),
                                  font=(FONT, 11))
                ph.pack(padx=6, pady=4)
                if mid:
                    self._bind_hover(mid, bubble, ph, name=name, text=itxt, path=path)
                threading.Thread(target=self._decode_thumb_worker,
                                 args=(path, cache_key, bubble, ph, mid, name, itxt), daemon=True).start()
            self._maybe_scroll_bottom()
            self._trim_feed()
        except Exception:
            self._add_bubble(name, "🖼 一张图片（无法预览）", mine, ts, mid=mid)

    def _decode_thumb_worker(self, path, cache_key, bubble, ph, mid=None, name="", itxt=""):
        """后台线程解码缩略图（PIL 解码不进 UI 线程，全局并发上限 4）。"""
        _THUMB_SEM.acquire()
        try:
            self._decode_thumb_worker_inner(path, cache_key, bubble, ph, mid, name, itxt)
        finally:
            _THUMB_SEM.release()

    def _decode_thumb_worker_inner(self, path, cache_key, bubble, ph, mid=None, name="", itxt=""):
        img = None
        try:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None
            im = Image.open(path)
            try:
                im.draft("RGB", (560, 560))
            except Exception:
                pass
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            im = im.copy()
            im.thumbnail((280, 280))
            img = im
        except Exception:
            img = None
        try:
            self.root.after(0, lambda: self._apply_image_thumb(
                bubble, ph, cache_key, img, path, mid=mid, name=name, itxt=itxt))
        except Exception:
            pass

    def _apply_image_thumb(self, bubble, ph, cache_key, img, path, mid=None, name="", itxt=""):
        """主线程：解码完成后把占位替换为图片。"""
        try:
            if bubble is None or not bubble.winfo_exists():
                return
            if ph is not None:
                try:
                    ph.destroy()
                except Exception:
                    pass
            if img is None:
                return
            ctk_img = CTkImage(light_image=img, dark_image=img, size=(img.width, img.height))
            self._thumb_cache[cache_key] = ctk_img
            if len(self._thumb_cache) > 256:
                self._thumb_cache.clear()
            self._images.append(ctk_img)
            self._pack_image_label(bubble, ctk_img, path, mid=mid, name=name, itxt=itxt)
        except Exception:
            pass

    def _pack_image_label(self, bubble, ctk_img, path, mid=None, name="", itxt=""):
        _img = ctk.CTkLabel(bubble, image=ctk_img, text="", cursor="hand2")
        _img.pack(padx=6, pady=4)
        _img.bind("<Button-1>", lambda e, p=path: self._open_image(p))
        _img.bind("<Button-3>", lambda e, p=path: self._image_menu(e, p))
        if mid:
            self._bind_hover(mid, bubble, _img, name=name, text=itxt, path=path)

    def _voice_menu(self, event, path):
        """语音消息右键菜单：转发 / 打开位置。"""
        try:
            menu = tk.Menu(self.root, tearoff=0, font=(FONT, 10))
            menu.add_command(label="转发", command=lambda: self._forward_voice(path))
            menu.add_command(label="打开文件位置", command=lambda: self._open_file_location(path))
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _forward_voice(self, path):
        """转发语音：作为文件发送到当前选中会话。"""
        try:
            if not (path and os.path.isfile(path)):
                self._set_status("语音文件不存在", "err")
                return
            if not (self.backend and self.backend.online):
                self._set_status("未连接，无法转发", "err")
                return
            s = self._sessions.get(self._current)
            if s is None:
                return
            if s["kind"] == "group":
                self.backend.send_file(s["room"], path)
            else:
                self.backend.send_file_dm(s["cid"], path)
            self._set_status("已转发语音到当前会话", "ok")
        except Exception:
            pass

    def _image_menu(self, event, path):
        """图片消息右键菜单：转发 / 保存 / 复制图片 / 打开大图 / 打开位置。"""
        try:
            menu = tk.Menu(self.root, tearoff=0, font=(FONT, 10))
            menu.add_command(label="转发到…",
                             command=lambda: self._forward_dialog(
                                 [{"type": "file", "path": path,
                                   "label": os.path.basename(path)}]))
            menu.add_command(label="保存图片到本地…", command=lambda: self._save_image_dialog(path))
            menu.add_command(label="复制图片", command=lambda: self._copy_image(path))
            menu.add_separator()
            menu.add_command(label="查看大图", command=lambda: self._open_image(path))
            menu.add_command(label="打开文件位置", command=lambda: self._open_file_location(path))
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _save_image_dialog(self, path):
        """把图片保存到用户指定位置。"""
        try:
            if not (path and os.path.isfile(path)):
                self._set_status("图片文件不存在", "err")
                return
            base = os.path.basename(path) or "image.png"
            name, ext = os.path.splitext(base)
            dest = filedialog.asksaveasfilename(
                title="保存图片",
                defaultextension=ext or ".png",
                initialfile=(name + (ext or ".png")))
            if not dest:
                return
            import shutil
            shutil.copyfile(path, dest)
            self._set_status(f"已保存：{dest}", "ok")
        except Exception:
            self._set_status("保存失败", "err")

    def _copy_image(self, path):
        """把图片复制到系统剪贴板（可粘贴到其它软件）。"""
        try:
            if not (path and os.path.isfile(path)):
                return
            if os.name == "nt":
                from PIL import Image
                img = Image.open(path)
                # Windows 剪贴板图片格式
                import io as _io
                buf = _io.BytesIO()
                img.convert("RGB").save(buf, "BMP")
                data = buf.getvalue()[14:]  # 去掉 BMP 文件头
                buf2 = _io.BytesIO()
                buf2.write(b"DIB")
                import struct
                buf2.write(struct.pack("<i", 40))
                # 用 win32 系统剪贴板不宜；回退为复制文件路径
                self.root.clipboard_clear()
                self.root.clipboard_append(os.path.normpath(path))
                self._set_status("图片路径已复制到剪贴板", "ok")
            else:
                self.root.clipboard_clear()
                self.root.clipboard_append(os.path.normpath(path))
                self._set_status("图片路径已复制到剪贴板", "ok")
        except Exception:
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(os.path.normpath(path))
                self._set_status("图片路径已复制到剪贴板", "ok")
            except Exception:
                pass

    def _expand_message(self, mid, full_text):
        """展开长消息全文：更新正文标签、销毁「展开全文」按钮并记住不再折叠。"""
        try:
            body = self._body_labels.get(mid)
            if body is not None:
                try:
                    body.configure(text=full_text)
                except Exception:
                    pass
            self._expanded_msgs.add(mid)
            bubble = self._bubble_frames.get(mid)
            if bubble is not None:
                for w in bubble.winfo_children():
                    try:
                        if isinstance(w, ctk.CTkButton) and w.cget("text") == "展开全文":
                            w.destroy()
                    except Exception:
                        pass
            self._maybe_scroll_bottom()
        except Exception:
            pass

    def _copy_as_quote(self, name, text):
        """复制为引用格式：方便在群里引用别人说的话（QQ 式）。"""
        t = str(text or "").replace("\r", "").strip()
        quote = f"引用 {name or '对方'} 的消息：\n{t}\n"
        self._copy_to_clipboard(quote)
        self._set_status("已复制为引用格式", "ok")

    def _speak_text(self, text):
        """朗读消息（Windows 系统语音 SAPI，无需第三方库）。"""
        try:
            t = (text or "").strip()[:200]
            if not t:
                return
            if os.name != "nt":
                self._set_status("当前系统不支持语音朗读", "err")
                return
            import subprocess as _sp
            safe = t.replace('"', "\u201c").replace("'", "\u2019")
            ps = ("Add-Type -AssemblyName System.Speech; "
                  "(New-Object System.Speech.Synthesis.SpeechSynthesizer)."
                  "Speak('" + safe + "')")
            _sp.Popen(["powershell", "-NoProfile", "-Command", ps],
                      creationflags=0x08000000,  # CREATE_NO_WINDOW
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            self._set_status("正在朗读…", "mute")
        except Exception:
            self._set_status("朗读失败", "err")

    def _copy_msg_link(self, mid):
        """复制消息定位码：会话#mid，粘贴到任意会话可让别人直接定位到该消息。"""
        try:
            s = self._sessions.get(self._current)
            room = (s.get("room") if s and s.get("kind") == "group"
                    else (s.get("cid") if s else ""))
            link = f"p2pchat://msg/{room}#{mid}"
            self._copy_to_clipboard(link)
            self._set_status(f"已复制消息链接（{room}#{mid[:8]}…）", "ok")
        except Exception:
            pass

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

    def _message_menu(self, event, text, file_path=None, mine=False, mid=None, name=""):
        """消息右键菜单：复制 / 转发 / 回应 / 置顶 / 编辑 / 撤回。

        编辑 / 撤回规则（像 QQ）：只能操作自己发的消息，
        且发送后有时间窗限制（撤回 2 分钟内、编辑 5 分钟内）。"""
        try:
            # 反查消息获取时间戳（用于限时判断）
            mts = None
            if mid:
                _fm = self._find_msg(self._current, mid)
                if _fm is not None:
                    mts = _fm.get("ts")
            age = 0.0
            try:
                if mts:
                    age = time.time() - float(mts)
            except Exception:
                age = 0.0
            can_recall = bool(mine and mid and age <= RECALL_WINDOW)
            can_edit = bool(mine and mid and age <= EDIT_WINDOW)
            is_mine_old = bool(mine and mid and age > RECALL_WINDOW)

            menu = tk.Menu(self.root, tearoff=0, font=(FONT, 10))
            menu.add_command(label="复制", command=lambda: self._copy_to_clipboard(text))
            menu.add_command(label="复制为引用", command=lambda: self._copy_as_quote(name, text))
            if (text or "").strip():
                menu.add_command(label="🔊 朗读", command=lambda: self._speak_text(text))
            if mid:
                menu.add_command(label="复制消息链接",
                                 command=lambda: self._copy_msg_link(mid))
                menu.add_command(label="ℹ 消息详情",
                                 command=lambda: self._show_message_details(mid))
            # 转发：文件消息带文件路径，文字消息带文本（修复文件消息转发丢失文件）
            _fwd_items = [{"type": "file", "path": file_path,
                            "label": os.path.basename(file_path)}] if (file_path and os.path.isfile(file_path)) else text
            menu.add_command(label="转发", command=lambda: self._forward_dialog(_fwd_items))
            menu.add_command(label="多选转发…", command=self._start_multi_select)
            menu.add_command(label="引用回复", command=lambda: self._start_reply(name or "对方", text, mid))
            if mid:
                react_menu = tk.Menu(menu, tearoff=0)
                for emo in ["👍", "❤️", "😂", "😮", "😢", "🙏"]:
                    react_menu.add_command(label=emo, command=lambda e=emo: self._do_reaction(mid, e))
                menu.add_cascade(label="回应", menu=react_menu)
                menu.add_command(label=("取消置顶" if self._is_pinned(mid) else "置顶"),
                                 command=lambda: self._toggle_pin(mid))
            if can_edit:
                menu.add_command(label="编辑", command=lambda: self._edit_message_dialog(mid, text))
            if can_recall:
                menu.add_command(label="撤回", command=lambda: self._do_recall(mid))
            if is_mine_old and mine:
                menu.add_command(label="撤回（超时，仅 2 分钟内）", state="disabled")
            if not mine and mid:
                menu.add_command(label="编辑/撤回（仅自己的消息）", state="disabled")
            if file_path:
                menu.add_command(label="打开文件位置", command=lambda: self._open_file_location(file_path))
                menu.add_command(label="复制路径", command=lambda: self._copy_to_clipboard(file_path))
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _start_multi_select(self):
        """进入多选转发模式：点击消息气泡勾选，底部工具栏转发。"""
        try:
            self._multi_mode = True
            self._multi_selected = []
            self._multi_frames = {}
            # 底部工具栏
            bar = ctk.CTkFrame(self._ibar.master, fg_color=C("panel"), corner_radius=10)
            self._multi_bar = bar
            bar.pack(fill="x", padx=8, pady=(0, 4), before=self._ibar)
            self._multi_count_lbl = ctk.CTkLabel(bar, text="已选 0 条（点击消息勾选）",
                                                 text_color=C("text"), font=(FONT, 11))
            self._multi_count_lbl.pack(side="left", padx=10, pady=6)
            ctk.CTkButton(bar, text="→ 转发", width=70, height=28, corner_radius=8,
                          fg_color=C("accent"), hover_color=C("accent_hover"),
                          font=(FONT, 11, "bold"), command=self._finish_multi_forward).pack(side="right", padx=(0, 8), pady=5)
            ctk.CTkButton(bar, text="取消", width=60, height=28, corner_radius=8,
                          fg_color=C("input_bg"), text_color=C("text_2"),
                          hover_color=C("input_hover"), font=(FONT, 11),
                          command=self._exit_multi_select).pack(side="right", padx=6, pady=5)
            self._render_feed()  # 重渲染：气泡可点击勾选
            self._set_status("多选转发模式：点击消息勾选，再点“→ 转发”", "accent")
        except Exception:
            self._exit_multi_select()

    def _toggle_multi_select(self, mid):
        """切换某条消息的选中状态并更新高亮。"""
        try:
            if mid in self._multi_selected:
                self._multi_selected.remove(mid)
            else:
                self._multi_selected.append(mid)
            f = self._multi_frames.get(mid)
            if f is not None:
                try:
                    f.configure(border_width=(2 if mid in self._multi_selected else 0),
                                border_color=(C("accent") if mid in self._multi_selected else None))
                except Exception:
                    pass
            try:
                self._multi_count_lbl.configure(text=f"\u5df2\u9009 {len(self._multi_selected)} \u6761\uff08\u70b9\u51fb\u6d88\u606f\u52fe\u9009\uff09")
            except Exception:
                pass
        except Exception:
            pass

    def _finish_multi_forward(self):
        """收集已选消息（文字 / 图片 / 文件），转发。"""
        try:
            s = self._sessions.get(self._current)
            items = []
            if s:
                for m in s["messages"]:
                    mid = m.get("mid")
                    if mid and mid in self._multi_selected:
                        p = None
                        if m.get("img_path") and os.path.isfile(m["img_path"]):
                            p = m["img_path"]
                        elif m.get("file_path") and os.path.isfile(m["file_path"]):
                            p = m["file_path"]
                        if p:
                            items.append({"type": "file", "path": p,
                                          "label": os.path.basename(p)})
                        else:
                            t = str(m.get("text", "")).strip()
                            if t and not m.get("system"):
                                items.append(t)
            self._exit_multi_select()
            if items:
                self._forward_dialog(items)
            else:
                self._set_status("没有选中可转发的消息", "err")
        except Exception:
            self._exit_multi_select()

    def _exit_multi_select(self):
        """退出多选模式，清理工具栏。"""
        try:
            self._multi_mode = False
            self._multi_selected = []
            self._multi_frames = {}
            bar = getattr(self, "_multi_bar", None)
            if bar is not None:
                try:
                    bar.destroy()
                except Exception:
                    pass
                self._multi_bar = None
            self._render_feed()
        except Exception:
            pass

    def _forward_dialog(self, items):
        """转发对话框：预览内容 + 搜索过滤目标 + 多目标复选批量转发。

        items 为 str（单条文字）或列表（多条文字/文件 dict）。"""
        try:
            if isinstance(items, str):
                items = [items]
            items = [it for it in (items or []) if it]
            win = ctk.CTkToplevel(self.root)
            win.title("转发到…")
            win.geometry("380x520")
            win.resizable(False, False)
            try:
                self._round_toplevel(win)
            except Exception:
                pass
            win.attributes("-topmost", True)
            ctk.CTkLabel(win, text=("转发内容" if len(items) == 1
                                    else f"转发内容（{len(items)} 条）"),
                         font=(FONT, 13, "bold"), text_color=C("text")).pack(pady=(14, 2))

            # 内容预览（单条直接显示，多条滚动）
            prev_h = 120 if len(items) <= 2 else 150
            preview = ctk.CTkScrollableFrame(win, fg_color=C("input_bg"), corner_radius=8)
            preview.pack(fill="x", padx=14, pady=(2, 6))
            for it in items[:6]:
                if isinstance(it, dict) and it.get("path"):
                    ctk.CTkLabel(preview, text=f"📎 {it.get('label', '文件')}",
                                 anchor="w", font=(FONT, 10), text_color=C("text_2")).pack(
                        fill="x", padx=8, pady=1)
                else:
                    _t = str(it).replace("\n", " ")[:80]
                    ctk.CTkLabel(preview, text=("💬 " + _t if _t else "（空消息）"),
                                 anchor="w", font=(FONT, 10), text_color=C("text_2"),
                                 justify="left", wraplength=300).pack(fill="x", padx=8, pady=1)
            if len(items) > 6:
                ctk.CTkLabel(preview, text=f"… 等 {len(items)} 条",
                             font=(FONT, 9), text_color=C("text_mute")).pack(anchor="w", padx=8)

            # 目标搜索框
            search_var = ctk.StringVar()
            ctk.CTkEntry(win, textvariable=search_var, height=28, corner_radius=8,
                         border_width=0, fg_color=C("input_bg"), text_color=C("text"),
                         placeholder_text="搜索转发目标…", font=(FONT, 11)).pack(fill="x", padx=14, pady=(4, 2))

            # 目标列表（复选框多选）
            sel = {}  # key -> ("group", room) 或 ("dm", cid)
            listbox = ctk.CTkScrollableFrame(win, fg_color="transparent")
            listbox.pack(fill="both", expand=True, padx=14, pady=(2, 4))
            targets = []
            for room in getattr(self, "_rooms", []) or []:
                targets.append((f"# {room}", ("group", room)))
            seen_dm = set()
            for s in list(self._sessions.values()):
                if s.get("kind") == "dm" and s.get("cid"):
                    seen_dm.add(s["cid"])
                    targets.append((f"@ {s['name']}", ("dm", s["cid"])))
            for cid, name in self._peers.items():
                if cid != self.cid and cid not in seen_dm and (not self._rooms or True):
                    seen_dm.add(cid)
                    targets.append((f"@ {name}", ("dm", cid)))

            def _render_targets():
                for w in listbox.winfo_children():
                    w.destroy()
                kw = (search_var.get() or "").strip().lower()
                for label, key in targets:
                    if kw and kw not in label.lower():
                        continue
                    row = ctk.CTkFrame(listbox, fg_color="transparent")
                    row.pack(fill="x", pady=1)
                    chk = ctk.CTkCheckBox(row, text=label, height=26,
                                          fg_color=C("input_bg"), text_color=C("text"),
                                          hover_color=C("input_hover"), font=(FONT, 12),
                                          command=lambda k=key, l=label: _toggle(k, l))
                    chk.pack(side="left", padx=(2, 0))
                    chk._fkey = key

            def _toggle(key, label):
                if key in sel:
                    sel.pop(key, None)
                else:
                    sel[key] = label
                _update_count()

            def _update_count():
                try:
                    n = len(sel)
                    fwd_btn.configure(text=f"转发到 {n} 个会话 →" if n else "选择目标…",
                                      fg_color=(C("accent") if n else C("input_bg")))
                except Exception:
                    pass

            search_var.trace_add("write", lambda *_: _render_targets())
            _render_targets()

            # 底部操作栏
            bottom = ctk.CTkFrame(win, fg_color="transparent")
            bottom.pack(fill="x", padx=14, pady=(2, 12))
            fwd_btn = ctk.CTkButton(bottom, text="选择目标…", width=150, height=32, corner_radius=8,
                                    fg_color=C("input_bg"), text_color="#ffffff",
                                    hover_color=C("accent_hover"), font=(FONT, 12, "bold"),
                                    command=lambda: _do_forward_all())
            fwd_btn.pack(side="right")
            ctk.CTkButton(bottom, text="取消", width=70, height=32, corner_radius=8,
                          fg_color=C("input_bg"), text_color=C("text_2"),
                          hover_color=C("input_hover"), font=(FONT, 12),
                          command=win.destroy).pack(side="right", padx=(0, 8))

            def _do_forward_all():
                if not sel:
                    self._set_status("请先选择转发目标", "err")
                    return
                ok_total = 0
                fail = 0
                for key, _label in list(sel.items()):
                    kind, tgt = key
                    if self._do_forward(tgt, items, kind == "dm", win, keep_open=True):
                        ok_total += 1
                    else:
                        fail += 1
                win.destroy()
                if ok_total:
                    self._set_status(f"已转发到 {ok_total} 个会话" + (f"，{fail} 个失败" if fail else ""), "ok")
                else:
                    self._set_status("转发失败", "err")

            win.bind("<Escape>", lambda e: win.destroy())
        except Exception:
            pass

    def _do_forward(self, target, items, is_dm, win, keep_open=False):
        """转发到指定会话；返回是否全部成功。keep_open=True 时保留对话框（多目标批量转发）。"""
        if not keep_open:
            try:
                win.destroy()
            except Exception:
                pass
        if not (self.backend and self.backend.online):
            self._set_status("未连接，无法转发", "err")
            return False
        if isinstance(items, str):
            items = [items]
        items = [it for it in (items or []) if it]
        if not items:
            self._set_status("没有可转发的消息", "err")
            return False
        my = self.nick_var.get().strip() or "未命名"
        ok = 0
        total = len(items)
        for it in items:
            try:
                if isinstance(it, dict) and it.get("path") and os.path.isfile(it["path"]):
                    if is_dm:
                        if self.backend.send_file_dm(target, it["path"]):
                            ok += 1
                    else:
                        if self.backend.send_file(target, it["path"]):
                            ok += 1
                else:
                    t = str(it).strip()
                    if not t:
                        continue
                    if is_dm:
                        if self.backend.send_dm(target, t):
                            self._append_message(self._dm_key(target), my, t, True)
                            ok += 1
                    else:
                        if self.backend.send_text(target, t):
                            ok += 1
            except Exception:
                pass
        if not keep_open:
            self._set_status(f"已转发 {ok}/{total} 条", "ok" if ok else "err")
        return ok == total and total > 0

    def _edit_message_dialog(self, mid, text):
        """弹出编辑消息对话框（预填原文，保存后提交编辑）。"""
        try:
            win = ctk.CTkToplevel(self.root)
            win.title("编辑消息")
            win.geometry("440x230")
            win.resizable(False, False)
            win.attributes("-topmost", True)
            box = ctk.CTkTextbox(win, width=400, height=110, corner_radius=10,
                                 fg_color=C("input_bg"), text_color=C("text"),
                                 font=(FONT, 12), wrap="word")
            box.pack(padx=20, pady=(20, 10))
            box.insert("1.0", text or "")
            box.focus_set()

            def _save():
                new = box.get("1.0", "end").strip()
                win.destroy()
                self._do_edit(mid, new)

            ctk.CTkButton(win, text="保存修改", width=120, height=32, corner_radius=8,
                          font=(FONT, 12, "bold"), fg_color=C("accent"),
                          hover_color=C("accent_hover"), command=_save).pack(pady=(0, 14))
            win.bind("<Escape>", lambda e: win.destroy())
        except Exception:
            pass

    def _render_system_line(self, text):
        """系统消息行：居中文本 + 两侧装饰线（QQ 式时间线外观）。"""
        row = ctk.CTkFrame(self.feed, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=5)
        ctk.CTkFrame(row, height=1, fg_color=C("hover")).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkLabel(row, text=str(text), text_color=C("text_mute"), wraplength=460,
                     justify="center", font=(FONT, 10)).pack(side="left")
        ctk.CTkFrame(row, height=1, fg_color=C("hover")).pack(side="left", fill="x", expand=True, padx=(8, 0))

    def _render_pinned_card(self, m):
        """置顶消息卡片：固定显示在消息列表最上方；点击卡片跳到原消息位置。"""
        try:
            card = ctk.CTkFrame(self.feed, corner_radius=10, fg_color=C("warn_bg"))
            card.pack(fill="x", padx=12, pady=4)
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.pack(fill="x", padx=12, pady=(6, 0))
            ctk.CTkLabel(top, text=f"📌 置顶 · {m.get('name', '')}", text_color=C("warn_text"),
                         font=(FONT, 10, "bold"), anchor="w").pack(side="left")
            mid = m.get("mid")
            ctk.CTkButton(top, text="取消置顶", width=70, height=22, corner_radius=6,
                          fg_color=C("input_bg"), text_color=C("warn_text"),
                          hover_color=C("input_hover"), font=(FONT, 10),
                          command=lambda: self._toggle_pin(mid)).pack(side="right")
            body = ctk.CTkLabel(card, text=str(m.get("text", ""))[:200], text_color=C("warn_text"),
                                font=(FONT, 11), anchor="w", justify="left",
                                wraplength=480)
            body.pack(fill="x", padx=12, pady=(2, 8))
            # 点击卡片正文/标题 → 滚动到原消息并高亮
            if mid:
                for _w in (card, top, body):
                    try:
                        _w.configure(cursor="hand2")
                        _w.bind("<Button-1>", lambda e, mm=mid: self._jump_to_message(mm), add="+")
                    except Exception:
                        pass
        except Exception:
            pass

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
            over = len(kids) - ChatApp.FEED_MAX
            if over > 0:
                # 批量销毁：单次 Tcl destroy 调用替代逐条 Python 销毁（大会话更快）
                batch = kids[:over]
                try:
                    self.feed.tk.call("destroy", *[str(w) for w in batch])
                except Exception:
                    for w in batch:
                        try:
                            w.destroy()
                        except Exception:
                            pass
        except Exception:
            pass

    def _jump_to_message(self, mid):
        try:
            s = self._sessions.get(self._current)
            if s is None or not mid:
                return
            exists = self._find_msg(self._current, mid) is not None
            if not exists:
                self._set_status("被引用的消息不在当前会话", "err")
                return
            self._history_expanded = True
            self._render_feed()
            self.root.after(50, lambda m=mid: self._scroll_to_mid(m))
        except Exception:
            pass

    def _scroll_to_mid(self, mid):
        """滚动到指定消息并高亮（引用跳转 / 搜索命中导航共用）。"""
        try:
            canvas = self.feed._parent_canvas
            canvas.update_idletasks()
            canvas.configure(scrollregion=canvas.bbox("all"))
            bf = self._bubble_frames.get(mid)
            if bf is not None:
                y = bf.winfo_y()
                total = max(1, canvas.winfo_reqheight())
                frac = max(0.0, min(1.0, y / total))
                canvas.yview_moveto(frac)
                bf.configure(border_width=2, border_color=C("search_hl"))
                def _unhl(_b=bf):
                    try:
                        _b.configure(border_width=0, border_color=None)
                    except Exception:
                        pass
                self.root.after(1200, _unhl)
        except Exception:
            pass

    def _expand_history(self):
        self._history_expanded = True
        self._render_feed()

    def _on_feed_wheel(self, event):
        """滚轮事件：向上滚到顶部时触发加载更早历史（防抖 150ms）。"""
        try:
            delta = getattr(event, "delta", 0)
            num = getattr(event, "num", 0)
            if delta > 0 or num == 4:  # 向上滚
                if self._at_top() and not self._history_expanded:
                    if getattr(self, "_older_after", None) is not None:
                        try:
                            self.root.after_cancel(self._older_after)
                        except Exception:
                            pass
                    self._older_after = self.root.after(150, self._maybe_load_older)
        except Exception:
            pass

    def _at_top(self):
        """当前是否滚到顶部（用于自动加载更早历史）。"""
        try:
            canvas = self.feed._parent_canvas
            top, _bottom = canvas.yview()
            return float(top) <= 0.02
        except Exception:
            return False

    def _maybe_load_older(self):
        """滚到顶部时自动加载更早消息（已展开则无事可做）。"""
        if self._history_expanded:
            return
        s = self._sessions.get(self._current)
        if s is None or self._search_query:
            return
        if len(s["messages"]) <= self.RENDER_MAX:
            return
        if not self._at_top():
            return
        # 抬高渲染上限，重渲染并保持顶部位置
        self._history_expanded = True
        try:
            canvas = self.feed._parent_canvas
            canvas.yview_moveto(0.0)
        except Exception:
            pass
        self._render_feed()

    def _schedule_feed_refresh(self):
        """合并短时间内的多次回执（已读/送达/编辑/撤回），只重渲染一次。"""
        if self._feed_after is not None:
            try:
                self.root.after_cancel(self._feed_after)
            except Exception:
                pass
        self._feed_after = self.root.after(80, self._do_feed_refresh)

    def _do_feed_refresh(self):
        self._feed_after = None
        self._render_feed()

    def _render_welcome_page(self):
        """无会话时的樱花欢迎页（Canvas 绘制：花瓣装饰 + 引导文案）。"""
        try:
            import tkinter as _tk2
            w = max(420, self.feed.winfo_width() or 460)
            h = 300
            cv = _tk2.Canvas(self.feed, width=w, height=h, bg=C("app_bg"),
                             highlightthickness=0)
            cv.pack(pady=36)
            # 随机散落的花瓣（固定种子保证稳定）
            try:
                import random as _rnd
                rnd = _rnd.Random(42)
            except Exception:
                rnd = None
            petal_colors = [C("accent"), C("text_mute"), C("section")]
            for i in range(14):
                px = rnd.randint(20, w - 20) if rnd else 30 + i * 28
                py = rnd.randint(14, h - 14) if rnd else 20 + (i * 37) % (h - 40)
                sz = rnd.randint(3, 7) if rnd else 5
                col = petal_colors[i % len(petal_colors)]
                cv.create_oval(px - sz, py - sz // 2, px + sz, py + sz // 2,
                               fill=col, outline="")
            cv.create_text(w // 2, h // 2 - 26, text="✿ 欢迎来到 P2P 聊天 ✿",
                           font=(FONT, 17, "bold"), fill=C("text"))
            cv.create_text(w // 2, h // 2 + 8, text="在左侧加入房间，或点「💌 私聊」开始聊天",
                           font=(FONT, 11), fill=C("text_mute"))
            cv.create_text(w // 2, h // 2 + 34, text="无需服务器 · 免注册 · 端到端加密可选",
                           font=(FONT, 10), fill=C("text_mute"))
        except Exception:
            ctk.CTkLabel(self.feed, text="请在左侧选择或加入一个会话。",
                         text_color=C("text_mute"), font=(FONT, 11)).pack(pady=20)

    def _render_feed(self):
        for w in self.feed.winfo_children():
            w.destroy()
        self._images = []
        self._bubble_frames = {}
        self._reaction_rows = {}
        self._body_labels = {}
        self._footer_labels = {}
        self._feed_batch_gen = (getattr(self, "_feed_batch_gen", 0) or 0) + 1
        s = self._sessions.get(self._current)
        if s is None:
            self._update_chat_title()
            self._render_welcome_page()
            return
        msgs = s["messages"]
        if self._feed_filter == "img":
            msgs = [m for m in msgs if m.get("img_path")]
        elif self._feed_filter == "file":
            msgs = [m for m in msgs if m.get("file_path")]
        if not msgs and not self._search_query:
            # 空聊天封面：大表情 + 引导文案 + 快捷操作按钮（有会话但还没聊过）
            cv = None
            try:
                import tkinter as _tk3
                nm = s.get("name", "会话")
                cv = _tk3.Canvas(self.feed, width=420, height=230, bg=self._chat_bg_color(),
                                 highlightthickness=0)
                cv.pack(pady=40)
                cv.create_text(210, 55, text="🎉", font=(FONT, 40))
                cv.create_text(210, 125, text=f"和 {nm} 开始聊天吧",
                               font=(FONT, 14, "bold"), fill=C("text"))
                cv.create_text(210, 152, text="发条消息、贴张图或按住 🎤 说句话",
                               font=(FONT, 11), fill=C("text_mute"))
                # 快捷操作：💌 发起私聊 / ➕ 加入房间（Web 式引导，免翻菜单）
                btn_row = ctk.CTkFrame(self.feed, fg_color="transparent")
                btn_row.pack(pady=(0, 6))
                ctk.CTkButton(btn_row, text="💌 发起私聊", width=110, height=30, corner_radius=8,
                              fg_color=C("accent"), hover_color=C("accent_hover"),
                              text_color="#ffffff", font=(FONT, 11, "bold"),
                              command=self._open_dm_dialog).pack(side="left", padx=5)
                ctk.CTkButton(btn_row, text="➕ 加入房间", width=110, height=30, corner_radius=8,
                              fg_color=C("input_bg"), text_color=C("text_2"),
                              hover_color=C("input_hover"), font=(FONT, 11),
                              command=self._add_room_from_input).pack(side="left", padx=5)
            except Exception:
                pass
            self._update_chat_title()
            return
        if self._feed_filter and not msgs:
            ctk.CTkLabel(self.feed,
                         text=f"（当前会话没有{'图片' if self._feed_filter == 'img' else '文件'}消息）",
                         text_color=C("text_mute"), font=(FONT, 11)).pack(pady=24)
            self._update_chat_title()
            return
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
        self._feed_render = {
            "msgs": msgs, "idx": 0, "last_day": None,
            "last_seen": s.get("last_seen_ts"), "shown_new": False,
            "pinned": [m for m in msgs if m.get("pinned") and not m.get("recalled")],
        }
        self._suppress_auto_scroll = True  # 全量渲染：逐条 auto-scroll 交给末尾一次 _scroll_bottom
        for pm in self._feed_render["pinned"]:
            self._render_pinned_card(pm)
        self._render_feed_batch(self._feed_batch_gen)

    def _render_feed_batch(self, gen):
        """分批渲染消息（每批约 25 条），让大会话切换不卡界面。"""
        if gen != getattr(self, "_feed_batch_gen", 0):
            return
        st = self._feed_render
        if st is None:
            return
        msgs = st["msgs"]
        end = min(st["idx"] + 25, len(msgs))
        last_day = st["last_day"]
        last_seen = st["last_seen"]
        shown_new = st["shown_new"]
        for idx in range(st["idx"], end):
            m = msgs[idx]
            if m.get("pinned"):
                continue
            ts = m.get("ts")
            if last_seen and ts and float(ts) > last_seen and not shown_new and not m.get("mine"):
                self._render_system_line("新消息")
                shown_new = True
            dlabel = _day_label(ts) if ts else ""
            day_break = bool(dlabel and dlabel != last_day)
            if day_break:
                self._render_system_line(dlabel)
                last_day = dlabel
            show_head = day_break or self._should_show_head(msgs, idx)
            if m.get("system"):
                self._render_system_line(m.get("text", ""))
            elif m.get("voice") and m.get("file_path") and os.path.isfile(m["file_path"]):
                self._add_voice_bubble(m["name"], m["file_path"], m["mine"], ts, show_head,
                                       mid=m.get("mid"))
            elif m.get("img_path") and os.path.isfile(m["img_path"]):
                self._add_image_bubble(m["name"], m["img_path"], m["mine"], ts, show_head,
                                       mid=m.get("mid"))
            else:
                self._add_bubble(m["name"], m["text"], m["mine"], ts, show_head,
                                 file_path=m.get("file_path"), read_by=m.get("read_by"),
                                 delivered_by=m.get("delivered_by"), mid=m.get("mid"),
                                 recalled=m.get("recalled"), recalled_by=m.get("recalled_by"),
                                 edited=m.get("edited"), reply=m.get("reply"),
                                 reactions=m.get("reactions"),
                                 search_hl=bool(self._search_query and
                                                self._search_query.lower() in str(m.get("text", "")).lower()))
        st["idx"] = end
        st["last_day"] = last_day
        st["shown_new"] = shown_new
        if end < len(msgs):
            self.root.after(12, lambda g=gen: self._render_feed_batch(g))
        else:
            self._finish_feed_render()

    def _finish_feed_render(self):
        self._suppress_auto_scroll = False
        s = self._sessions.get(self._current)
        if self._search_query:
            self._scroll_top()
        else:
            # 若该会话保存过滚动位置（切走前正在看历史），恢复之；否则回到底部
            frac = (s or {}).get("scroll_frac")
            if frac and not self._history_expanded:
                try:
                    canvas = self.feed._parent_canvas
                    canvas.update_idletasks()
                    canvas.configure(scrollregion=canvas.bbox("all"))
                    canvas.yview_moveto(max(0.0, min(1.0, float(frac))))
                except Exception:
                    self._scroll_bottom()
            else:
                self._scroll_bottom()
        if s:
            self._ack_reads(s)

    def _flush_feed_render(self):
        """同步把剩余批次渲染完（测试 / 收尾用）。"""
        while getattr(self, "_feed_render", None) is not None:
            st = self._feed_render
            if st is None or st["idx"] >= len(st["msgs"]):
                break
            self._render_feed_batch(self._feed_batch_gen)

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
                                 img_path=tp, preview_tid=info.get("tid"),
                                 mid=info.get("tid"))
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
            if is_image(mime) and info.get("path") and os.path.isfile(info["path"]):
                self._append_message(key, my, f"🖼 图片：{name}", True,
                                     img_path=info["path"], mid=info.get("tid"))
            else:
                self._show_system(f"📤 正在发送文件：{name}（{fmt_size(size)}）", key)
        elif event == "accepted":
            self._show_system(f"✅ 对方已接受，开始发送：{name}", key)
        elif event == "accepting":
            self._show_system(f"📥 已同意接收，正在等待数据：{name}", key)
        elif event == "progress":
            self._set_status(f"传输中 {info.get('percent', 0)}% · {name}", "accent")
        elif event == "sent":
            if is_image(mime):
                self._set_status(f"✅ 图片已发送：{name}", "ok")
            elif _is_voice_name(name) and info.get("path"):
                self._append_message(key, my, f"🎤 语音：{name}", True,
                                     file_path=info.get("path", ""), voice=True)
            else:
                self._append_message(key, my, f"📎 {name}（{fmt_size(size)}）· 点击打开", True,
                                     file_path=info.get("path", ""))
        elif event == "offer":
            if is_image(mime) and info.get("thumb"):
                self._show_image_preview(key, room, info)
            else:
                # QQ 式：语音/文件一律自动接收，不弹窗询问；直接显示在聊天界面
                if self.backend:
                    self.backend.accept_file(info.get("tid"))
                sname = info.get("sname", "对方")
                if not _is_voice_name(name):
                    self._append_message(key, sname,
                                         f"📥 正在接收文件：{name}（{fmt_size(size)}）",
                                         False, system=True)
        elif event == "rejected":
            self._append_message(key, "", f"⚠️ 对方拒绝接收：{name}", False, system=True)
        elif event == "done":
            sname = info.get("sname", "对方")
            path = info.get("path", "")
            if is_image(mime):
                if not self._replace_preview(key, info.get("tid"), path):
                    self._append_message(key, sname, f"🖼 图片：{name}", False,
                                         img_path=path, mid=info.get("tid"))
            elif _is_voice_name(name):
                self._append_message(key, sname, f"🎤 语音：{name}", False,
                                     file_path=path, voice=True)
            else:
                self._append_message(key, sname, f"📎 {name}（{fmt_size(size)}）· 点击打开", False,
                                     file_path=path)
            self._show_system(f"✅ 已保存到：{path}", key)
        elif event == "error":
            self._append_message(key, "", f"⚠️ {name}：{info.get('msg', '失败')}", False, system=True)

    def _on_close(self):
        try:
            if getattr(self, "_auto_backup_stop", None) is not None:
                self._auto_backup_stop.set()
        except Exception:
            pass
        try:
            if getattr(self, "_playing_voice", None):
                self._stop_voice_play(self._playing_voice,
                                      btn=getattr(self, "_playing_btn", None),
                                      bar=getattr(self, "_playing_bar", None))
        except Exception:
            pass
        try:
            self._destroy_hover_bar()
        except Exception:
            pass
        try:
            if getattr(self, "_overlay_hicon", 0):
                import ctypes
                ctypes.windll.user32.DestroyIcon(self._overlay_hicon)
                self._overlay_hicon = 0
            _taskbar3_release()
        except Exception:
            pass
        try:
            # 修复：最大化状态退出会保存全屏几何，下次启动直接全屏——先还原再保存
            if getattr(self, "_maximized", False):
                if getattr(self, "_restore_geo", None):
                    self.root.geometry(self._restore_geo)
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
            self._orig_img = img.copy()   # 原始图（缩放/旋转基于它）
            self._base_w = img.width
            self._base_h = img.height
            self._zoom_lvl = 1.0
            self._rotated = 0
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
        # 预览窗口必须浮在最上层（QQ/微信看图行为），否则点开后可能被主窗口盖住
        try:
            top.attributes("-topmost", True)
            top.lift()
            top.focus_force()
            top.after(30, lambda: (top.lift(), top.focus_force()))
        except Exception:
            pass

        ctk.CTkLabel(top, text=os.path.basename(path), text_color=C("text_2"),
                     font=(FONT, 11)).pack(padx=20, pady=(12, 4))
        self._img_lbl = ctk.CTkLabel(top, image=ctk_img, text="", fg_color="#000000")
        self._img_lbl.pack(padx=24, pady=(0, 4))
        # 缩放百分比指示（点按可在 100% ↔ 适应间切换）
        self._zoom_lbl = ctk.CTkLabel(top, text="100% · 双击切换", text_color=C("text_mute"),
                                      font=(FONT, 9))
        self._zoom_lbl.pack(pady=(0, 2))
        # 操作栏：缩小 / 放大 / 旋转 / 保存副本
        ctrl = ctk.CTkFrame(top, fg_color="transparent")
        ctrl.pack(pady=(0, 6))
        ctk.CTkButton(ctrl, text="−", width=40, height=28, corner_radius=8,
                      fg_color=C("input_bg"), hover_color=C("input_hover"),
                      text_color=C("text_2"), font=(FONT, 13, "bold"),
                      command=lambda: self._zoom(0.8)).pack(side="left", padx=3)
        ctk.CTkButton(ctrl, text="+", width=40, height=28, corner_radius=8,
                      fg_color=C("input_bg"), hover_color=C("input_hover"),
                      text_color=C("text_2"), font=(FONT, 13, "bold"),
                      command=lambda: self._zoom(1.25)).pack(side="left", padx=3)
        ctk.CTkButton(ctrl, text="↻", width=48, height=28, corner_radius=8,
                      fg_color=C("input_bg"), hover_color=C("input_hover"),
                      text_color=C("text_2"), font=(FONT, 12),
                      command=self._rotate).pack(side="left", padx=3)
        ctk.CTkButton(ctrl, text="保存副本", width=80, height=28, corner_radius=8,
                      fg_color=C("input_bg"), hover_color=C("input_hover"),
                      text_color=C("text_2"), font=(FONT, 11),
                      command=lambda: self._save_copy(path)).pack(side="left", padx=3)
        ctk.CTkButton(top, text="关闭", width=90, height=30, corner_radius=8,
                      fg_color=C("input_bg"), hover_color=C("input_hover"),
                      text_color=C("text_2"), font=(FONT, 12), command=top.destroy).pack(pady=(4, 14))
        # 双击图片：100% ↔ 适应窗口 切换（QQ 看图行为）
        self._fit_lvl = 1.0
        try:
            fit = min(1.0, (600.0 / max(1, img.height)), (820.0 / max(1, img.width)))
            self._fit_lvl = max(0.1, fit)
        except Exception:
            pass
        self._img_lbl.bind("<Double-Button-1>", lambda e: self._toggle_fit())

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

    def _zoom(self, factor):
        """缩放预览图片（±按钮，范围 0.2 ~ 4.0）。"""
        try:
            nxt = getattr(self, "_zoom_lvl", 1.0) * factor
            self._zoom_lvl = max(0.2, min(4.0, nxt))
            img = self._orig_img
            w = max(1, int(self._base_w * self._zoom_lvl))
            h = max(1, int(self._base_h * self._zoom_lvl))
            ctk_img = CTkImage(light_image=img, dark_image=img, size=(w, h))
            self._ctk_img = ctk_img
            self._img_lbl.configure(image=ctk_img)
            try:
                self._zoom_lbl.configure(text=f"{int(self._zoom_lvl * 100)}% · 双击切换")
            except Exception:
                pass
        except Exception:
            pass

    def _toggle_fit(self):
        """双击图片：100% ↔ 适应窗口 切换（QQ 看图行为）。"""
        try:
            if getattr(self, "_zoom_lvl", 1.0) > 1.01:
                self._zoom_lvl = getattr(self, "_fit_lvl", 1.0)
            else:
                self._zoom_lvl = 1.0
            img = self._orig_img
            w = max(1, int(self._base_w * self._zoom_lvl))
            h = max(1, int(self._base_h * self._zoom_lvl))
            ctk_img = CTkImage(light_image=img, dark_image=img, size=(w, h))
            self._ctk_img = ctk_img
            self._img_lbl.configure(image=ctk_img)
            try:
                self._zoom_lbl.configure(text=f"{int(self._zoom_lvl * 100)}% · 双击切换")
            except Exception:
                pass
        except Exception:
            pass

    def _rotate(self):
        """旋转预览图片 90°。"""
        try:
            if getattr(self, "_rotated", 0) >= 3:
                self._rotated = 0
            else:
                self._rotated = getattr(self, "_rotated", 0) + 1
            img = self._orig_img.rotate(-90 * self._rotated, expand=True)
            ctk_img = CTkImage(light_image=img, dark_image=img,
                               size=(img.width, img.height))
            self._ctk_img = ctk_img
            self._img_lbl.configure(image=ctk_img)
        except Exception:
            pass

    def _save_copy(self, path):
        """把当前预览图（含缩放/旋转效果）保存为副本。"""
        try:
            from tkinter import filedialog
            base = os.path.basename(path) or "image.png"
            name, ext = os.path.splitext(base)
            dest = filedialog.asksaveasfilename(
                title="保存图片副本", defaultextension=ext or ".png",
                initialfile=(name + "_copy" + (ext or ".png")))
            if not dest:
                return
            img = self._orig_img
            if getattr(self, "_rotated", 0):
                img = img.rotate(-90 * self._rotated, expand=True)
            # 应用当前缩放级别（保存所见即所得）
            z = getattr(self, "_zoom_lvl", 1.0)
            if z and abs(z - 1.0) > 0.01:
                w = max(1, int(img.width * z))
                h = max(1, int(img.height * z))
                img = img.resize((w, h), getattr(__import__("PIL").Image, "LANCZOS", 1))
            img.save(dest)
            try:
                messagebox.showinfo("已保存", f"图片已保存到：\n{dest}")
            except Exception:
                pass
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
        top.geometry("500x580")
        top.resizable(False, False)
        top.transient(master)
        try:
            top.pack_propagate(False)  # 锁定固定尺寸，避免内容撑破底部按钮
        except Exception:
            pass
        top.configure(fg_color=C("app_bg"))
        top.bind("<Escape>", lambda e: top.destroy())
        try:
            top.grab_set()
        except Exception:
            pass

        # 按钮行最先 pack(side=bottom)：按钮永远底部可见，内容填充剩余
        btnrow = ctk.CTkFrame(top, fg_color="transparent")
        btnrow.pack(side="bottom", fill="x", padx=18, pady=(8, 16))
        self.dl_btn = ctk.CTkButton(btnrow, text="下载并安装", height=36, corner_radius=10,
                                    font=(FONT, 12, "bold"), command=self._do_download)
        self.dl_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(btnrow, text="关闭", width=90, height=36, corner_radius=10,
                      fg_color=C("input_bg"), text_color=C("text_2"), hover_color=C("input_hover"),
                      font=(FONT, 12), command=top.destroy).pack(side="right")

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
                                       text_color=C("text_2"), font=(FONT, 11), wrap="word",
                                       height=200)
        self.body_box.pack(fill="both", expand=True, padx=18, pady=8)
        self._render_body()

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
            # 安装包直链缺失：尝试用标签推导直链，不再直接跳 GitHub
            dl = self._derive_download_url(v.get("tag", ""), v.get("html", ""))
            if dl:
                self.top.destroy()
                if self.download_cb:
                    self.download_cb(dl)
                return
            import webbrowser
            webbrowser.open(v.get("html") or
                            f"https://github.com/{UPDATE_OWNER}/{UPDATE_REPO}/releases")
            return
        self.top.destroy()
        if self.download_cb:
            self.download_cb(v["dl"])

    @staticmethod
    def _derive_download_url(tag, html_url=""):
        """用标签推导安装包直链（应对资产列表缺失/解析失败）。"""
        try:
            t = str(tag or "").strip()
            if not t:
                if "/releases/tag/" in str(html_url or ""):
                    t = str(html_url).rsplit("/", 1)[-1].strip()
            if t:
                return (f"https://github.com/{UPDATE_OWNER}/{UPDATE_REPO}"
                        f"/releases/download/{t}/P2PChat-Setup.exe")
        except Exception:
            pass
        return ""

class UpdateDialog:
    """发现新版本时的提示框：展示版本号 / 更新内容 / 跳转下载。"""

    def __init__(self, master, latest, body, notes, download_url="", download_cb=None):
        top = ctk.CTkToplevel(master)
        self.top = top
        top.title("发现新版本")
        top.geometry("500x560")
        top.resizable(False, False)
        top.transient(master)
        try:
            top.pack_propagate(False)
        except Exception:
            pass
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
                                  border_width=0, height=240)
        body_box.pack(fill="x", padx=16, pady=(0, 6))
        body_box.insert("1.0", text)
        body_box.configure(state="disabled")

        btnrow = ctk.CTkFrame(top, fg_color="transparent")
        btnrow.pack(side="bottom", fill="x", padx=24, pady=(0, 24))
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
            # 后台线程异常只写日志不弹窗（弹窗会导致“请不刷新错误窗口”）。
            # 后台线程（网络/直连/备份等）出错应自治复体，不打断用户。
            tb = getattr(args, "exc_traceback", None)
            _write_crash(args.exc_type, args.exc_value, tb)
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


def _resource_path(rel):
    """定位程序资源文件（图标等）：打包后从 PyInstaller 临时目录取，开发时取项目根目录。"""
    try:
        base = sys._MEIPASS
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def _set_window_icon(root):
    """设置运行时窗口图标（标题栏 + 任务栏）为用户自定义图标。

    优先用 iconbitmap（Windows 原生 ICO），再用 iconphoto
    把 ICO 转 PNG 设置一次（同时覆盖标题栏与任务栏）。
    兼容了部分 Windows 环境下 iconbitmap 不刷任务栏的情况。"""
    try:
        ico = _resource_path("P2PChat.ico")
        if not os.path.isfile(ico):
            return False
        try:
            root.iconbitmap(ico)
        except Exception:
            pass
        try:
            from PIL import Image as _PIL
            import tkinter as _tk
            img = _PIL.open(ico)
            # 用 32px 帧（任务栏标准清晰尺寸）转 PNG，iconphoto 设置标题栏+任务栏
            try:
                img.seek(0)
                img = img.convert("RGBA")
                img.thumbnail((32, 32), _PIL.LANCZOS)
            except Exception:
                pass
            import io as _io
            buf = _io.BytesIO()
            img.save(buf, "PNG")
            photo = _tk.PhotoImage(data=buf.getvalue())
            root.iconphoto(True, photo)
            root._p2p_icon_ref = photo  # 防止被垃圾回收
            return True
        except Exception:
            return True  # iconbitmap 已成功
    except Exception:
        return False


def main():
    global _DND_READY
    _patch_focus_guards()
    _install_excepthook()
    if not _ensure_deps():
        return
    # 读取上次保存的主题（默认跟随系统），与 C() 颜色体系保持一致
    _mode = str(_load_settings().get("appearance_mode", "system") or "system").strip()
    _appearance = str(_load_settings().get("appearance", "dark")).strip()
    if _mode == "system":
        _appearance = _detect_system_theme()
    set_appearance(_appearance)
    ctk.set_default_color_theme("blue")
    try:
        root = ctk.CTk()
        _set_window_icon(root)
        if _HAS_DND:
            try:
                TkinterDnD.require(root)
                _DND_READY = True
            except Exception:
                _DND_READY = False
        root.report_callback_exception = _report_callback_exception
        # 单窗口：直接进入主界面，头像/昵称/ID 都在主界面内设置，不再有独立登录层
        profile = _load_profile()
        app = ChatApp(root, profile=profile, name=profile["name"], avatar=profile["avatar"], bio=profile.get("bio", ""))
        app.check_for_update()          # 后台静默检查更新
        root.mainloop()
    except Exception:
        p = _write_crash(*sys.exc_info())
        _notify_crash(p)
        raise


if __name__ == "__main__":
    main()