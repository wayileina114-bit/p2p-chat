#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P2P 聊天软件 —— 一个文件搞定一切（免费版，无需公网 IP）
========================================================

两种用法（子命令）：

  1) 远程聊天（跨公网，免费：连公共 MQTT 服务器，无需自建服务器、不求公网 IP）
     python chat.py remote --name 小明 --room 我们的房间
     python chat.py remote --name 小红 --room 我们的房间
     （可选指定服务器：--broker broker.emqx.io --port 1883）

  2) 局域网直连模式（无服务器，同一 WiFi/网线下用）
     python chat.py lan --name 小明 --port 9000
     python chat.py lan --name 小红 --port 9001 --connect 127.0.0.1:9000

聊天框命令（两种模式通用，仅 lan 支持 /connect）：
    /connect <host> [port]   连接其他节点（仅局域网模式）
    /peers                   查看在线的人
    /nick <新名字>           修改昵称
    /help                    帮助
    /quit 或 /exit           退出

原理：
    - 局域网模式：TCP 直连 + gossip 洪泛 + 消息 ID 去重 + TTL 限制，全网可达不循环。
    - 远程模式：所有客户端连到免费的公共 MQTT 服务器（broker.emqx.io 等），按「房间名」
      派生一个别人猜不到的私有话题；在同一个房间的人发消息都发到这个话题，服务器负责
      广播给同房间的人。在线状态用 MQTT 的 retained 消息 + 遗嘱实现自动发现，谁上线、
      谁下线，同房间的人都能看到。消息本质上由公共服务器中转（不是直连），但对你来说
      免费、稳定、跨任何网络都能通。
"""

import argparse
import hashlib
import json
import socket
import struct
import threading
import time
import uuid

try:
    import paho.mqtt.client as mqtt
    _MQTT_OK = True
except Exception:
    mqtt = None
    _MQTT_OK = False

MAX_MSG = 64 * 1024 * 1024          # 单条消息 64MB 上限（仅局域网模式使用）
DEFAULT_BROKER = "broker.emqx.io"   # 免费公共 MQTT 服务器，可换成其它
DEFAULT_MQTT_PORT = 1883            # 未加密端口（8883 为 TLS，需额外配置）


def _now():
    return time.strftime("%H:%M:%S")


def _pack(obj: dict) -> bytes:
    """把字典编码成「4 字节长度前缀 + JSON」的字节串（局域网模式用）。"""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return struct.pack(">I", len(data)) + data


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """精确读取 n 字节；连接断开时抛出 ConnectionError。"""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接已关闭")
        buf += chunk
    return buf


def _recv_message(sock: socket.socket) -> dict:
    """读取一条完整消息并解析为字典。"""
    header = _recv_exact(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_MSG:
        raise ValueError("消息长度异常")
    body = _recv_exact(sock, length)
    return json.loads(body.decode("utf-8"))


# ===========================================================================
# 第一部分：局域网直连模式（无服务器，P2P 洪泛）
# ===========================================================================


class PeerConnection:
    """封装与某一个邻居节点的 TCP 连接与接收线程。"""

    def __init__(self, sock: socket.socket, addr, nickname: str, incoming: bool):
        self.sock = sock
        self.addr = addr
        self.nickname = nickname
        self.incoming = incoming  # True=对方连我，False=我连对方
        self.id = uuid.uuid4().hex[:8]
        self.alive = True
        self.lock = threading.Lock()

    def send(self, obj: dict):
        """线程安全地发送一条消息；失败则标记连接已死。"""
        try:
            with self.lock:
                self.sock.sendall(_pack(obj))
        except OSError:
            self.alive = False

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def __repr__(self):
        direction = "←" if self.incoming else "→"
        return f"{self.nickname or self.addr[0]} ({self.addr[0]}:{self.addr[1]} {direction})"


class P2PChat:
    """局域网直连聊天：既能监听、也能主动连别人，地位完全对等。"""

    def __init__(self, nickname: str, port: int):
        self.nickname = nickname
        self.port = port
        self.peers = {}        # peer.id -> PeerConnection
        self.seen = set()      # 已处理过的消息 ID（去重）
        self.lock = threading.Lock()
        self.running = True
        self.server_sock = None

    @staticmethod
    def _print(prefix: str, name: str, text: str):
        name = name or "匿名"
        print(f"[{time.strftime('%H:%M:%S')}] {prefix} {name}: {text}")

    def start_server(self):
        """在后台线程监听端口，接受别人主动连接。"""
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("0.0.0.0", self.port))
        self.server_sock.listen(16)
        self.server_sock.settimeout(0.5)

        def loop():
            while self.running:
                try:
                    sock, addr = self.server_sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._adopt(sock, addr, incoming=True)

        threading.Thread(target=loop, daemon=True, name="acceptor").start()

    def connect(self, host: str, port: int):
        try:
            sock = socket.create_connection((host, port), timeout=5)
        except OSError as e:
            print(f"[!] 无法连接到 {host}:{port}：{e}")
            return
        print(f"[*] 正在连接 {host}:{port} ...")
        self._adopt(sock, (host, port), incoming=False)

    def _adopt(self, sock: socket.socket, addr, incoming: bool):
        peer = PeerConnection(sock, addr, "", incoming)
        try:
            peer.send({"type": "hello", "nickname": self.nickname})
            hello = _recv_message(sock)
            peer.nickname = hello.get("nickname", "")
        except (OSError, ValueError, ConnectionError):
            peer.close()
            print(f"[!] 与 {addr[0]}:{addr[1]} 握手失败")
            return

        with self.lock:
            self.peers[peer.id] = peer
        direction = "对方:" if incoming else "我:"
        print(f"[+] 已{'接受' if incoming else '建立'}连接 {direction} {peer}")
        threading.Thread(target=self._receive_loop, args=(peer,), daemon=True).start()

    def _receive_loop(self, peer: PeerConnection):
        while self.running and peer.alive:
            try:
                msg = _recv_message(peer.sock)
            except (OSError, ValueError, ConnectionError):
                break
            if not msg:
                break
            self._handle(msg, peer)

        with self.lock:
            self.peers.pop(peer.id, None)
        peer.close()
        print(f"[-] 连接断开：{peer.nickname or peer.addr[0]} ({peer.addr[0]}:{peer.addr[1]})")

    def _handle(self, msg: dict, from_peer: PeerConnection):
        mtype = msg.get("type")

        if mtype == "hello":
            with self.lock:
                from_peer.nickname = msg.get("nickname", from_peer.nickname)
            return

        if mtype == "msg":
            mid = msg.get("id")
            if mid in self.seen:
                return
            self.seen.add(mid)

            sender = msg.get("from", "匿名")
            text = msg.get("text", "")
            self._print("💬", sender, text)

            ttl = int(msg.get("ttl", 8))
            if ttl > 1:
                msg["ttl"] = ttl - 1
                self._forward(msg, exclude=from_peer)

    def _forward(self, msg: dict, exclude: PeerConnection):
        with self.lock:
            targets = [p for p in self.peers.values() if p is not exclude and p.alive]
        for p in targets:
            p.send(msg)
            if not p.alive:
                with self.lock:
                    self.peers.pop(p.id, None)

    def broadcast(self, text: str):
        msg = {
            "type": "msg",
            "id": uuid.uuid4().hex,
            "from": self.nickname,
            "text": text,
            "ts": time.time(),
            "ttl": 16,
        }
        self.seen.add(msg["id"])
        self._print("💬", self.nickname, text)
        self._forward(msg, exclude=None)

    def change_nick(self, new: str):
        new = new.strip()
        if not new:
            print("[!] 昵称不能为空")
            return
        self.nickname = new
        print(f"[*] 昵称已改为：{new}")
        with self.lock:
            targets = list(self.peers.values())
        for p in targets:
            if p.alive:
                p.send({"type": "hello", "nickname": new})

    def show_peers(self):
        with self.lock:
            peers = list(self.peers.values())
        if not peers:
            print("[*] 当前没有已连接的邻居。用 /connect <host> <port> 连接其他节点。")
            return
        print(f"[*] 当前共有 {len(peers)} 个邻居：")
        for p in peers:
            print(f"    · {p}")

    def shutdown(self):
        self.running = False
        with self.lock:
            peers = list(self.peers.values())
        for p in peers:
            p.close()
        if self.server_sock:
            try:
                self.server_sock.close()
            except OSError:
                pass


def input_loop(chat: P2PChat):
    print("""
--------------------------------------------------
  P2P Chat（局域网直连 · 无服务器）
  输入 /help 查看命令
--------------------------------------------------
""")
    while chat.running:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt, OSError):
            break
        line = line.rstrip("\n")
        if not line.strip():
            continue

        if line.startswith("/"):
            parts = line.split(None, 2)
            cmd = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []

            if cmd in ("/quit", "/exit"):
                print("[*] 正在退出...")
                chat.running = False
                break
            elif cmd == "/help":
                print(
                    "/connect <host> [port]  连接其他节点\n"
                    "/peers                  查看邻居\n"
                    "/nick <新名字>          修改昵称\n"
                    "/help                   帮助\n"
                    "/quit                   退出"
                )
            elif cmd == "/peers":
                chat.show_peers()
            elif cmd == "/connect":
                if len(args) < 1:
                    print("[!] 用法：/connect <host> [port]")
                    continue
                host = args[0]
                port = int(args[1]) if len(args) > 1 else 9000
                chat.connect(host, port)
            elif cmd == "/nick":
                if len(args) < 1:
                    print("[!] 用法：/nick <新名字>")
                    continue
                chat.change_nick(args[0])
            else:
                print(f"[!] 未知命令：{cmd}，输入 /help 查看帮助。")
        else:
            chat.broadcast(line)

    chat.shutdown()


# ===========================================================================
# 第二部分：远程模式（免费公共 MQTT，跨公网 + 自动发现）
# ===========================================================================


class MqttClient:
    """通过免费的公共 MQTT 服务器通信，实现跨公网聊天 + 自动发现。

    原理：
      - 房间名经 SHA-256 派生出一个私有话题，避免陌生人随便撞见你的房间。
      - 聊天消息发布到 `<话题>/msg`，服务器广播给所有订阅同房间的人。
      - 在线状态用 retained 消息 + 遗嘱（LWT）实现：上线时发布自己的 retained
        在线标记，离线时被自动清除；因此新加入的人能马上看到当前谁在线。
    """

    def __init__(self, nickname: str, broker: str, port: int, room: str):
        self.nickname = nickname
        self.broker = broker
        self.port = port
        self.room = room
        self.client_id = "chat-" + uuid.uuid4().hex[:10]
        digest = hashlib.sha256(room.encode("utf-8")).hexdigest()[:16]
        self.topic_prefix = "p2pchat-opensquilla/" + digest
        self.topic_msg = self.topic_prefix + "/msg"
        self.topic_presence = self.topic_prefix + "/presence"
        self.peers = {}          # client_id -> name（不含自己）
        self.online = False
        self.running = True
        self._client = None

    @staticmethod
    def _msg(name, text):
        print(f"[{time.strftime('%H:%M:%S')}] 💬 {name}: {text}")

    # --------------------------- 连接与回调 ---------------------------

    def _build(self):
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        except AttributeError:
            c = mqtt.Client(client_id=self.client_id)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.on_disconnect = self._on_disconnect
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        # 遗嘱：异常掉线时清掉自己的在线标记
        c.will_set(self.topic_presence + "/" + self.client_id, payload=b"", qos=1, retain=True)
        return c

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if hasattr(rc, "is_failure"):
            ok = not rc.is_failure
        else:
            ok = (rc == 0)
        if ok:
            self.online = True
            client.subscribe(self.topic_prefix + "/#", qos=1)
            self._publish_presence()
            print(f"[+] 已连接，房间「{self.room}」")
        else:
            self.online = False
            print(f"[!] MQTT 连接被拒绝，返回码：{rc}")

    def _on_disconnect(self, client, userdata, *args):
        self.online = False

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8")
        except Exception:
            return

        if topic == self.topic_msg:
            try:
                data = json.loads(payload)
            except ValueError:
                return
            if data.get("cid") == self.client_id:
                return
            self._msg(data.get("name", "匿名"), data.get("text", ""))
        elif topic.startswith(self.topic_presence + "/"):
            cid = topic.rsplit("/", 1)[-1]
            if cid == self.client_id:
                return
            if payload:
                try:
                    data = json.loads(payload)
                    name = data.get("name", "匿名")
                except ValueError:
                    name = "匿名"
                if self.peers.get(cid) == name:
                    return  # 已知，避免重复刷屏
                self.peers[cid] = name
                print(f"[*] {name} 上线了（房间在线 {len(self.peers)} 人）")
            else:
                if cid in self.peers:
                    name = self.peers.pop(cid)
                    print(f"[-] {name} 下线了（房间在线 {len(self.peers)} 人）")

    # --------------------------- 发布 ---------------------------

    def _publish_presence(self):
        if self._client is None:
            return
        payload = json.dumps({"name": self.nickname, "ts": time.time()}, ensure_ascii=False)
        self._client.publish(self.topic_presence + "/" + self.client_id, payload, qos=1, retain=True)

    def _clear_presence(self):
        if self._client is not None:
            self._client.publish(self.topic_presence + "/" + self.client_id, b"", qos=1, retain=True)

    def send_text(self, text: str):
        if not self.online:
            print("[!] 尚未连接到服务器，消息未发送。")
            return
        self._msg(self.nickname, text)
        payload = json.dumps(
            {"name": self.nickname, "text": text, "cid": self.client_id, "ts": time.time()},
            ensure_ascii=False,
        )
        self._client.publish(self.topic_msg, payload, qos=1)

    def change_nick(self, new: str):
        new = new.strip()
        if not new:
            print("[!] 昵称不能为空")
            return
        self.nickname = new
        self._publish_presence()
        print(f"[*] 昵称已改为：{new}")

    def show_peers(self):
        if not self.online:
            print("[*] 尚未连接到服务器。")
            return
        if not self.peers:
            print(f"[*] 房间「{self.room}」里目前只有你，等对方也进入同一房间会自动发现。")
            return
        print(f"[*] 房间「{self.room}」当前在线 {len(self.peers)} 人：")
        for cid, name in self.peers.items():
            print(f"    · {name}")

    # --------------------------- 启动 / 关闭 ---------------------------

    def start(self):
        if not _MQTT_OK:
            print("[!] 本机缺少 paho-mqtt 库，请先安装：")
            print("      pip install paho-mqtt")
            self.running = False
            return
        self._client = self._build()
        print(f"[*] 正在连接免费公共服务器 {self.broker}:{self.port} ...")
        print(f"[*] 房间「{self.room}」")
        try:
            self._client.connect_async(self.broker, self.port, keepalive=30)
            self._client.loop_start()
        except Exception as e:
            print(f"[!] 连接初始化失败：{e}")
            self.running = False

    def shutdown(self):
        self.running = False
        if self._client is not None:
            try:
                self._clear_presence()
            except Exception:
                pass
            try:
                self._client.disconnect()
            except Exception:
                pass
            try:
                self._client.loop_stop()
            except Exception:
                pass


def mqtt_input_loop(client: MqttClient):
    print("""
--------------------------------------------------
  P2P Chat（远程模式 · 免费公共 MQTT，跨公网）
  输入 /help 查看命令
--------------------------------------------------
""")
    while client.running:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt, OSError):
            break
        line = line.rstrip("\n")
        if not line.strip():
            continue

        if line.startswith("/"):
            parts = line.split(None, 2)
            cmd = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []

            if cmd in ("/quit", "/exit"):
                print("[*] 正在退出...")
                client.running = False
                break
            elif cmd == "/help":
                print(
                    "/peers          查看房间内在线成员（自动发现）\n"
                    "/nick <新名字>  修改昵称\n"
                    "/help           帮助\n"
                    "/quit           退出"
                )
            elif cmd == "/peers":
                client.show_peers()
            elif cmd == "/nick":
                if len(args) < 1:
                    print("[!] 用法：/nick <新名字>")
                    continue
                client.change_nick(args[0])
            else:
                print(f"[!] 未知命令：{cmd}，输入 /help 查看帮助。")
        else:
            client.send_text(line)

    client.shutdown()


# ===========================================================================
# 入口：子命令分发
# ===========================================================================


def _parse_host_port(text: str, default_port: int):
    """把 'host' 或 'host:port' 解析成 (host, port)。"""
    text = (text or "").strip()
    if not text:
        return None, None
    if ":" in text:
        host, _, port_s = text.rpartition(":")
        try:
            return host, int(port_s)
        except ValueError:
            print(f"[!] 端口格式错误：{text}")
            return None, None
    return text, default_port


def _run_lan(name, port, connects):
    chat = P2PChat(name, port)
    chat.start_server()
    print(f"[*] 正在监听 0.0.0.0:{port}（昵称：{name}）")
    for target in connects:
        host, p = _parse_host_port(target, 9000)
        if host:
            chat.connect(host, p)
    input_loop(chat)


def _run_remote(name, broker, port, room):
    client = MqttClient(name, broker, port, room)
    client.start()
    if client.running:
        mqtt_input_loop(client)


def _ask(prompt, default=None):
    if default:
        s = input(f"{prompt}（直接回车用默认 {default}）：").strip()
    else:
        s = input(f"{prompt}：").strip()
    return s if s else default


def interactive_menu():
    """双击直接运行（不带参数）时进入的友好菜单，避免因缺参数而闪退。"""
    print("=" * 46)
    print("            P2P 聊天软件（免费版）")
    print("=" * 46)
    print("  [1] 远程聊天（跨公网，免费公共服务器，无需公网 IP）")
    print("  [2] 局域网聊天（同一 WiFi/网线，无需服务器）")
    print("=" * 46)
    choice = input("请输入选项 1 / 2 然后回车：").strip()

    if choice == "1":
        name = _ask("你的昵称") or "匿名"
        room = _ask("房间名（要和对方完全一致）", "默认房间") or "默认房间"
        broker = _ask("服务器地址", DEFAULT_BROKER) or DEFAULT_BROKER
        _run_remote(name, broker, DEFAULT_MQTT_PORT, room)
    elif choice == "2":
        name = _ask("你的昵称") or "匿名"
        port_s = _ask("本机监听端口", "9000") or "9000"
        try:
            port = int(port_s)
        except ValueError:
            print("[!] 端口必须是数字，已退出。")
            return
        _run_lan(name, port, [])
    else:
        print("[!] 无效选项，已退出。")


def main():
    parser = argparse.ArgumentParser(
        prog="chat.py",
        description="P2P 聊天软件（免费版：远程走公共 MQTT，局域网直连无服务器）",
    )
    # 不加 required=True —— 直接双击（无参数）时走 interactive_menu()，避免闪退。
    sub = parser.add_subparsers(dest="mode")

    p_lan = sub.add_parser("lan", help="局域网直连模式（无服务器）")
    p_lan.add_argument("--name", default="匿名", help="昵称")
    p_lan.add_argument("--port", type=int, default=9000, help="监听端口（默认 9000）")
    p_lan.add_argument("--connect", action="append", default=[],
                       help="启动后主动连接的目标，格式 host:port，可多次指定")

    p_remote = sub.add_parser("remote", help="远程模式（跨公网，免费公共 MQTT）")
    p_remote.add_argument("--name", default="匿名", help="昵称")
    p_remote.add_argument("--broker", default=DEFAULT_BROKER,
                          help=f"MQTT 服务器（默认 {DEFAULT_BROKER}）")
    p_remote.add_argument("--port", type=int, default=DEFAULT_MQTT_PORT,
                          help=f"MQTT 端口（默认 {DEFAULT_MQTT_PORT}）")
    p_remote.add_argument("--room", default="默认房间",
                          help="房间名，与对方完全一致才能互通（默认：默认房间）")

    args = parser.parse_args()

    if args.mode == "lan":
        _run_lan(args.name, args.port, args.connect)
    elif args.mode == "remote":
        _run_remote(args.name, args.broker, args.port, args.room)
    else:
        interactive_menu()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] 已退出。")
    except Exception as e:  # 闪退保护：任何错误都停在窗口里，让你看到原因
        print("\n[!] 程序出错：", repr(e))
        try:
            input("按回车键退出...")
        except (EOFError, KeyboardInterrupt):
            pass