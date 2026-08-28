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

# 拖拽功能是否在运行时真正可用（成功后才会注册 drop target，避免 tkdnd 加载失败导致崩溃）
_DND_READY = False

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

APP_VERSION = "1.8.1"            # 程序版本（每次更新时 +1）
UPDATE_OWNER = "wayileina114-bit"  # GitHub 仓库所有者（自动检查更新用）
UPDATE_REPO = "p2p-chat"           # GitHub 仓库名（自动检查更新用）

DEFAULT_BROKER = "broker.emqx.io"
DEFAULT_PORT = 1883
CHUNK_SIZE = 256 * 1024          # 每个分片 256KB（二进制直传，无 base64 开销）
MAX_FILE = 200 * 1024 * 1024     # 单文件上限 200MB
OFFER_TIMEOUT = 60.0             # 送文件请求 60 秒无人应答则取消

FONT = "Microsoft YaHei UI"
HINT = "输入文字，回车发送；也可直接把图片 / 文件拖到这里"


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


def set_appearance(mode):
    """切换全局主题；mode 取 dark / light，非法值回退 dark。"""
    global _APPEARANCE
    if mode not in THEMES:
        mode = "dark"
    _APPEARANCE = mode
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


def collect_env_report():
    """实时检测运行环境（pip 包名, 说明, 是否可用）。独立于启动时的缓存标志。"""
    import importlib
    import platform
    checks = [
        ("paho-mqtt", "paho.mqtt.client", "文字 / 文件传输"),
        ("Pillow", "PIL", "图片显示与预览"),
        ("customtkinter", "customtkinter", "圆角现代界面"),
        ("tkinterdnd2", "tkinterdnd2", "拖拽发送文件/图片"),
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
    if m.get("ts"):
        msg["ts"] = m["ts"]
    return msg


def _group_path(room):
    return os.path.join(DATA_DIR, "group_" + _safe_name(room) + ".json")


def _dm_path(cid):
    return os.path.join(DATA_DIR, "dm_" + (cid or "x") + ".json")


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

    def __init__(self, nickname, cid, broker=DEFAULT_BROKER, port=DEFAULT_PORT,
                 on_text=None, on_peers=None, on_file=None, on_status=None, on_dm=None):
        self.nickname = nickname or "匿名"
        self.cid = cid
        self.broker = broker
        self.port = port

        self.on_text = on_text
        self.on_peers = on_peers
        self.on_file = on_file
        self.on_status = on_status
        self.on_dm = on_dm

        self.online = False
        self.running = False
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
            # 已加入的所有房间
            for room in list(self.rooms.keys()):
                self._subscribe_room(room)
            self._publish_presence()
            self._fire_status(True, "已连接")
        else:
            self.online = False
            self._fire_status(False, f"连接失败（{rc}）")

    def _on_disconnect(self, client, userdata, *args):
        self.online = False
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

    # --------------------------- 文本 / 在场 / 私聊 ---------------------------

    def _handle_text(self, room, raw):
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return
        if data.get("cid") == self.cid:
            return
        self._fire_text(room, str(data.get("name", "匿名")), str(data.get("text", "")), False)

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
            name = str(data.get("name", "匿名"))
            rooms = data.get("rooms") or []
            self.presence[cid] = {"name": name, "rooms": [str(r) for r in rooms]}
        else:
            self.presence.pop(cid, None)
        self._fire_peers()

    def _handle_dm(self, topic, raw):
        target = topic.rsplit("/", 1)[-1]
        if target != self.cid:
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return
        sender = data.get("cid", "")
        if not sender or sender == self.cid:
            return
        if self.on_dm:
            self.on_dm(sender, str(data.get("name", "匿名")), str(data.get("text", "")))

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
        data["room"] = room
        self._offers[tid] = data
        self._fire_file(room, "offer", {
            "tid": tid, "name": data.get("name", "file"), "size": size,
            "mime": data.get("mime", ""), "sname": data.get("sname", "匿名"),
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
        if idx in r["chunks"]:
            return
        r["chunks"][idx] = raw[17:]
        r["got"] += 1
        if r["got"] >= r["total"]:
            self._finish(tid)

    def _finish(self, tid):
        r = self._receivers.pop(tid)
        try:
            blob = b"".join(r["chunks"][i] for i in range(r["total"]))
        except (KeyError, IndexError):
            self._fire_file(r["room"], "error", {"name": r["name"], "msg": "分片缺失，接收失败"})
            return
        if len(blob) != r["size"] or hashlib.md5(blob).hexdigest() != r["md5"]:
            self._fire_file(r["room"], "error", {"name": r["name"], "msg": "校验失败，文件可能损坏"})
            return
        d = DOWNLOADS_DIR
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, r["name"])
        base, ext = os.path.splitext(path)
        k = 1
        while os.path.exists(path):
            path = f"{base}({k}){ext}"
            k += 1
        with open(path, "wb") as f:
            f.write(blob)
        self._fire_file(r["room"], "done", {
            "name": r["name"], "path": path, "size": r["size"],
            "mime": r["mime"], "sname": r.get("sname", "对方"),
        })

    # --------------------------- 对外方法 ---------------------------

    def start(self):
        if not _MQTT_OK:
            self._fire_status(False, "缺少 paho-mqtt 库（pip install paho-mqtt）")
            return
        self.running = True
        self._client = self._build()
        self._client.loop_start()
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
        self._receivers.clear()
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
        self._client.publish(self._topic_room(room) + "/file/ctrl",
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
        payload = json.dumps({"name": self.nickname, "text": text, "cid": self.cid}, ensure_ascii=False)
        self._client.publish(self._topic_room(room) + "/msg", payload, qos=1)
        self._fire_text(room, self.nickname, text, True)
        return True

    def send_dm(self, target_cid, text):
        text = (text or "").strip()
        if not text or not self.online or self._client is None or not target_cid:
            return False
        payload = json.dumps({"name": self.nickname, "text": text, "cid": self.cid}, ensure_ascii=False)
        self._client.publish(f"{self.NS}/dms/{target_cid}", payload, qos=1)
        return True

    def send_file(self, room, path):
        if room not in self.rooms or not self.online or self._client is None:
            return False
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return False
        size = os.path.getsize(path)
        if size > MAX_FILE:
            self._fire_file(room, "error", {"name": os.path.basename(path), "msg": "超过 200MB 上限"})
            return False
        with open(path, "rb") as f:
            blob = f.read()
        name = os.path.basename(path)
        mime = guess_mime(name)
        total = max(1, math.ceil(size / CHUNK_SIZE))
        md5 = hashlib.md5(blob).hexdigest()
        tid = uuid.uuid4().hex[:12]

        self._pending[tid] = {
            "name": name, "size": size, "mime": mime, "total": total,
            "md5": md5, "blob": blob, "path": path, "room": room,
            "accepted": False, "evt": threading.Event(),
        }
        offer = {"kind": "offer", "id": tid, "from": self.cid, "sname": self.nickname,
                 "name": name, "size": size, "mime": mime, "total": total, "md5": md5}
        self._publish_ctrl(room, offer)
        threading.Thread(target=self._watch_send, args=(tid,), daemon=True).start()
        self._fire_file(room, "waiting", {"name": name, "size": size})
        return True

    def accept_file(self, tid):
        data = self._offers.pop(tid, None)
        if data is None:
            return
        room = data.get("room")
        self._receivers[tid] = {
            "name": data["name"], "size": data["size"], "mime": data["mime"],
            "total": data["total"], "md5": data["md5"],
            "chunks": {}, "got": 0, "sname": data.get("sname", "对方"), "room": room,
        }
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
        blob, total, name = p["blob"], p["total"], p["name"]
        room = p["room"]
        data_topic = self._topic_room(room) + "/file/data"
        for i in range(total):
            if not self.online or self._client is None:
                self._pending.pop(tid, None)
                self._fire_file(room, "error", {"name": name, "msg": "发送中断，连接已断开"})
                return
            piece = blob[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
            frame = b"C" + tid.encode("ascii") + struct.pack(">I", i) + piece
            try:
                self._client.publish(data_topic, frame, qos=1)
            except Exception:
                self._pending.pop(tid, None)
                self._fire_file(room, "error", {"name": name, "msg": "发送出错"})
                return
            if i % 8 == 0 or i + 1 == total:
                self._fire_file(room, "progress", {"name": name, "percent": int((i + 1) / total * 100)})
        self._pending.pop(tid, None)
        self._fire_file(room, "sent", {"name": name, "size": p["size"], "mime": p["mime"], "path": p["path"]})

    # --------------------------- 回调触发 ---------------------------

    def _fire_text(self, room, name, text, mine=False):
        if self.on_text:
            self.on_text(room, name, text, mine)

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


class ChatApp:
    FEED_MAX = 400         # 每个会话持久化的历史消息上限
    RENDER_MAX = 150       # 切换会话时最多立即渲染的消息条数（更早的折叠）

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
        self._search_after = None   # 搜索防抖 timer id

        self.root.title("P2P 聊天")
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

    # --------------------------- UI 构建 ---------------------------

    def _build_ui(self):
        # 顶部工具条：头像 / 昵称 / ID / 房间 / 加入 / 连接 / 主题
        top = ctk.CTkFrame(self.root, corner_radius=0, fg_color=C("panel"))
        top.pack(fill="x")

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

        self.theme_btn = ctk.CTkButton(top, text=("☀️" if self.appearance == "dark" else "🌙"),
                                       width=36, height=32, corner_radius=8,
                                       fg_color=C("input_bg"), hover_color=C("input_hover"),
                                       text_color=C("text_2"), font=(FONT, 14),
                                       command=self._toggle_theme)
        self.theme_btn.pack(side="right", padx=(0, 12), pady=12)

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
        self.input_box.bind("<FocusIn>", self._on_input_focus_in)
        self.input_box.bind("<FocusOut>", self._on_input_focus_out)
        if _DND_READY:
            try:
                self.input_box.drop_target_register(DND_FILES)
                self.input_box.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        btncol = ctk.CTkFrame(ibar, fg_color="transparent")
        btncol.pack(side="right", padx=(0, 14), pady=14)
        ctk.CTkButton(btncol, text="发送", width=88, height=34, corner_radius=8,
                      font=(FONT, 12, "bold"), fg_color=C("accent"),
                      hover_color=C("accent_hover"), command=self._send_text).pack(fill="x", pady=2)
        ctk.CTkButton(btncol, text="📎 文件/图片", width=88, height=30, corner_radius=8,
                      fg_color=C("input_bg"), text_color=C("text_2"), hover_color=C("input_hover"),
                      font=(FONT, 12), command=self._pick_file).pack(fill="x", pady=3)

    # --------------------------- 菜单 / 环境检测 ---------------------------

    def _build_menu(self):
        try:
            menubar = tk.Menu(self.root)
            view_menu = tk.Menu(menubar, tearoff=0)
            view_menu.add_command(label="深色主题", command=lambda: self._set_theme("dark"))
            view_menu.add_command(label="浅色主题", command=lambda: self._set_theme("light"))
            menubar.add_cascade(label="视图", menu=view_menu)
            help_menu = tk.Menu(menubar, tearoff=0)
            help_menu.add_command(label="检查更新", command=self._manual_check_update)
            help_menu.add_command(label="环境检测 / 关于", command=self._show_about)
            help_menu.add_separator()
            help_menu.add_command(label="清空当前会话记录", command=self._clear_current_history)
            help_menu.add_command(label="打开收件文件夹", command=self._open_downloads)
            menubar.add_cascade(label="帮助", menu=help_menu)
            self.root.config(menu=menubar)
        except Exception:
            pass

    # --------------------------- 主题切换 ---------------------------

    def _toggle_theme(self):
        self._set_theme("light" if self.appearance == "dark" else "dark")

    def _set_theme(self, mode):
        if mode not in THEMES or mode == self.appearance:
            return
        self.appearance = mode
        set_appearance(mode)
        _save_settings({"appearance": mode})
        self._rebuild_ui()

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
        self._search_after = self.root.after(120, self._apply_session_list)

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
            self.top_avatar.configure(image=None, text="👤", fg_color=C("input_bg"),
                                      text_color=C("text_mute"), font=(FONT, 16))

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

    def check_for_update(self):
        """后台静默检查 GitHub Releases 是否有新版本，有则弹窗提示。"""
        import urllib.request

        def work():
            try:
                url = (f"https://api.github.com/repos/{UPDATE_OWNER}/{UPDATE_REPO}"
                       f"/releases/latest")
                req = urllib.request.Request(url, headers={
                    "User-Agent": "P2PChat-App",
                    "Accept": "application/vnd.github+json",
                })
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                latest = str(data.get("tag_name", "")).lstrip("vV").strip()
                if not latest:
                    return
                if self._is_newer(latest, APP_VERSION):
                    body = (str(data.get("body", "")).strip()
                            .replace("\r\n", "\n").replace("\r", "\n"))
                    notes = data.get("html_url", "")
                    dl = self._pick_asset_url(data)
                    self.root.after(0, lambda: self._prompt_update(latest, body, notes, dl))
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
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
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
        """手动检查更新：带明确结果反馈（已最新 / 发现新版 / 失败）。"""
        import urllib.request

        def work():
            try:
                url = (f"https://api.github.com/repos/{UPDATE_OWNER}/{UPDATE_REPO}"
                       f"/releases/latest")
                req = urllib.request.Request(url, headers={
                    "User-Agent": "P2PChat-App",
                    "Accept": "application/vnd.github+json",
                })
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                latest = str(data.get("tag_name", "")).lstrip("vV").strip()
                if latest and self._is_newer(latest, APP_VERSION):
                    body = (str(data.get("body", "")).strip()
                            .replace("\r\n", "\n").replace("\r", "\n"))
                    notes = data.get("html_url", "")
                    dl = self._pick_asset_url(data)
                    self.root.after(0, lambda: self._prompt_update(latest, body, notes, dl))
                else:
                    self.root.after(0, lambda: self._set_status(
                        f"已是最新版本 v{APP_VERSION}", "ok"))
            except Exception:
                self.root.after(0, lambda: self._set_status(
                    "检查更新失败，请稍后重试", "err"))

        self._set_status("正在检查更新…", "mute")
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
                     key=lambda s: (0 if s["online"] else 1, s["name"]))
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

    def _add_group_item(self, room):
        key = self._group_key(room)
        selected = key == self._current
        s = self._sessions.get(key)
        unread = (s.get("unread") or 0) if s else 0
        n = sum(1 for p in self._peers.values() if room in (p.get("rooms") or []))
        row = ctk.CTkFrame(self.session_frame, corner_radius=8,
                           fg_color=(C("selected_bg") if selected else "transparent"))
        row.pack(fill="x", pady=1)
        hash_lbl = ctk.CTkLabel(row, text="#", width=18, anchor="w",
                                text_color=(C("accent") if selected else C("text_mute")),
                                font=(FONT, 13, "bold"), cursor="hand2")
        hash_lbl.pack(side="left", padx=(10, 0), pady=7)
        lbl = ctk.CTkLabel(row, text=room + (f"  {n}" if n else ""), anchor="w",
                           text_color=(C("selected_text") if selected else C("text")),
                           font=(FONT, 12, "bold" if selected else "normal"), cursor="hand2")
        lbl.pack(side="left", fill="x", expand=True, pady=7)
        if unread:
            self._unread_badge(row, unread).pack(side="right", padx=(0, 6))
        cross = ctk.CTkLabel(row, text="✕", width=24, text_color=C("text_mute"), cursor="hand2")
        cross.pack(side="right", padx=(0, 6))
        for w in (row, lbl, hash_lbl):
            w.bind("<Button-1>", lambda e, k=key: self._switch_to(k))
        cross.bind("<Button-1>", lambda e, r=room: self._remove_room(r))
        self._bind_row_hover(row, selected)

    def _add_dm_item(self, s):
        key = s["key"]
        selected = key == self._current
        unread = s.get("unread") or 0
        row = ctk.CTkFrame(self.session_frame, corner_radius=8,
                           fg_color=(C("selected_bg") if selected else "transparent"))
        row.pack(fill="x", pady=1)
        dot = ctk.CTkLabel(row, text="●" if s["online"] else "○", width=18, anchor="w",
                           text_color=(C("online") if s["online"] else C("text_mute")),
                           font=(FONT, 11, "bold"), cursor="hand2")
        dot.pack(side="left", padx=(10, 0), pady=7)
        lbl = ctk.CTkLabel(row, text=s["name"], anchor="w",
                           text_color=(C("selected_text") if selected else C("text")),
                           font=(FONT, 12, "bold" if (selected or unread) else "normal"),
                           cursor="hand2")
        lbl.pack(side="left", fill="x", expand=True, pady=7)
        if unread:
            self._unread_badge(row, unread).pack(side="right", padx=(0, 10))
        for w in (row, lbl, dot):
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

    def _append_message(self, key, name, text, mine, img_path=None):
        s = self._sessions.get(key)
        if s is None:
            return
        msg = {"name": name, "text": text, "mine": mine, "ts": time.time()}
        if img_path:
            msg["img_path"] = img_path
        s["messages"].append(msg)
        if len(s["messages"]) > self.FEED_MAX:
            s["messages"] = s["messages"][-self.FEED_MAX:]
        self._save_session(s)
        if key == self._current:
            if img_path and os.path.isfile(img_path):
                self._add_image_bubble(name, img_path, mine, msg.get("ts"))
            else:
                self._add_bubble(name, text, mine, msg.get("ts"))
        else:
            s["unread"] = s.get("unread", 0) + 1
            self._apply_session_list()
            self._update_window_title()

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
                self._show_system("发送失败，请检查连接。")
        else:
            self.backend.send_text(s["room"], text)

    def _on_enter(self, event):
        if event.state & 0x0001:     # Shift+回车 = 换行
            return None
        self._send_text()
        return "break"

    def _pick_file(self):
        path = filedialog.askopenfilename(title="选择要发送的文件或图片")
        if path:
            self._do_send_file(path)

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
        if s is None or s["kind"] != "group":
            self._show_system("请在群聊里发送文件 / 图片。")
            return
        self.backend.send_file(s["room"], path)

    # --------------------------- 回调（切回主线程） ---------------------------

    def _cb_text(self, room, name, text, mine):
        self.root.after(0, lambda: self._append_message(self._group_key(room), name, text, mine))

    def _cb_peers(self, peers):
        self.root.after(0, lambda: self._refresh_peers(peers))

    def _cb_file(self, room, event, info):
        self.root.after(0, lambda: self._show_file_event(room, event, info))

    def _cb_status(self, online, msg):
        self.root.after(0, lambda: self._set_status(msg, "ok" if online else "err"))

    def _cb_dm(self, from_cid, name, text):
        self.root.after(0, lambda: self._receive_dm(from_cid, name, text))

    def _receive_dm(self, from_cid, name, text):
        s = self._ensure_dm_session(from_cid, name)
        s["online"] = True
        self._append_message(s["key"], name, text, False)
        self._apply_session_list()

    def _refresh_peers(self, peers):
        self._peers = peers or {}
        for s in self._sessions.values():
            if s["kind"] == "dm":
                s["online"] = s["cid"] in self._peers
        self._apply_session_list()
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
        # 窗口标题实时反映连接状态 + 未读消息数
        try:
            if self.backend and self.backend.online:
                base = f"P2P 聊天 · 已连接 · {len(self._peers)} 人在线"
            else:
                base = "P2P 聊天 · 未连接"
            unread = sum(s.get("unread", 0) for s in self._sessions.values())
            if unread:
                base = f"● {base}  [{unread} 条未读]"
            self.root.title(base)
        except Exception:
            pass

    def _scroll_bottom(self):
        try:
            self.feed._parent_canvas.yview_moveto(1.0)
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
        self._scroll_bottom()
        self._trim_feed()

    def _add_bubble(self, name, text, mine, ts=None):
        tstr = _fmt_time(ts) if ts else ""
        bubble = ctk.CTkFrame(self.feed, corner_radius=14,
                              fg_color=(C("mine_bubble") if mine else C("other_bubble")))
        bubble.pack(anchor="e" if mine else "w", padx=12, pady=3)
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
        body.pack(anchor="w", padx=12, pady=(2, 8))
        body.bind("<Button-3>", lambda e, t=text: self._copy_text_menu(e, t))
        self._scroll_bottom()
        self._trim_feed()

    def _add_image_bubble(self, name, path, mine, ts=None):
        tstr = _fmt_time(ts) if ts else ""
        if not (_HAS_PIL and path and os.path.isfile(path)):
            self._add_bubble(name, "🖼 一张图片", mine, ts)
            return
        try:
            # 缩略图缓存：按 路径+mtime 复用已解码的 CTkImage，避免每次切会话都重新解码整张图
            cache_key = (path, int(os.path.getmtime(path)))
            ctk_img = self._thumb_cache.get(cache_key)
            if ctk_img is None:
                from PIL import Image  # 惰性加载
                img = Image.open(path)
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
            bubble = ctk.CTkFrame(self.feed, corner_radius=14,
                                  fg_color=(C("mine_bubble") if mine else C("other_bubble")))
            bubble.pack(anchor="e" if mine else "w", padx=12, pady=3)
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
            _img.bind("<Button-3>", lambda e, p=path: self._copy_text_menu(e, p))
            self._scroll_bottom()
            self._trim_feed()
        except Exception:
            self._add_bubble(name, "🖼 一张图片（无法预览）", mine, ts)

    def _copy_text_menu(self, event, text):
        try:
            menu = tk.Menu(self.root, tearoff=0)
            menu.add_command(label="复制", command=lambda: self._copy_to_clipboard(text))
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _copy_to_clipboard(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(text))
            self._set_status("已复制到剪贴板", "ok")
        except Exception:
            pass

    def _show_system(self, text, target_key=None):
        target_key = target_key or self._current
        if target_key is None or target_key != self._current:
            return
        ctk.CTkLabel(self.feed, text=text, text_color=C("text_mute"), wraplength=560,
                     justify="center", font=(FONT, 10)).pack(pady=6)
        self._scroll_bottom()
        self._trim_feed()

    def _trim_feed(self):
        try:
            kids = self.feed.winfo_children()
            if len(kids) > ChatApp.FEED_MAX:
                for w in kids[:len(kids) - ChatApp.FEED_MAX]:
                    w.destroy()
        except Exception:
            pass

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
        if len(msgs) > self.RENDER_MAX:
            ctk.CTkLabel(self.feed, text=f"… 更早的 {len(msgs) - self.RENDER_MAX} 条消息",
                         text_color=C("text_mute"), font=(FONT, 10),
                         justify="center").pack(pady=4)
            msgs = msgs[-self.RENDER_MAX:]
        last_day = None
        for m in msgs:
            ts = m.get("ts")
            dlabel = _day_label(ts) if ts else ""
            if dlabel and dlabel != last_day:
                ctk.CTkLabel(self.feed, text=f"── {dlabel} ──", text_color=C("text_mute"),
                             font=(FONT, 10)).pack(pady=(8, 2))
                last_day = dlabel
            if m.get("img_path") and os.path.isfile(m["img_path"]):
                self._add_image_bubble(m["name"], m["img_path"], m["mine"], ts)
            else:
                self._add_bubble(m["name"], m["text"], m["mine"], ts)
        self._scroll_bottom()

    def _show_file_event(self, room, event, info):
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
                self._append_message(key, my, f"📎 已发送文件：{name}（{fmt_size(size)}）", True)
        elif event == "offer":
            self._add_file_offer_card(key, room, info)
        elif event == "rejected":
            self._show_system(f"⚠️ 对方拒绝接收：{name}", key)
        elif event == "done":
            sname = info.get("sname", "对方")
            path = info.get("path", "")
            if is_image(mime):
                self._append_message(key, sname, f"🖼 图片：{name}", False, img_path=path)
            else:
                self._append_message(key, sname, f"📎 已收到文件：{name}（{fmt_size(size)}）", False)
            self._show_system(f"✅ 已保存到：{path}", key)
        elif event == "error":
            self._show_system(f"⚠️ {name}：{info.get('msg', '失败')}", key)

    def _on_close(self):
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
            img = Image.open(path)
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
        top.configure(fg_color="#15171f")
        top.resizable(False, False)

        ctk.CTkLabel(top, text=os.path.basename(path), text_color="#aeb4c0",
                     font=(FONT, 11)).pack(padx=20, pady=(12, 4))
        ctk.CTkLabel(top, image=ctk_img, text="").pack(padx=24, pady=(0, 4))
        ctk.CTkButton(top, text="关闭", width=90, height=30, corner_radius=8,
                      fg_color="#3a4150", hover_color="#2c323e",
                      font=(FONT, 12), command=top.destroy).pack(pady=(4, 14))

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

        self.log_box = ctk.CTkTextbox(top, height=96, corner_radius=10, fg_color="#0f1220",
                                      text_color="#c7f9cc", font=("Consolas", 11), wrap="word")
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


def _report_callback_exception(self, etype, value, tb):
    # 替换 tkinter 的默认回调异常处理，落日志而不是静默闪退
    p = _write_crash(etype, value, tb)
    _notify_crash(p)


def main():
    global _DND_READY
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