# -*- coding: utf-8 -*-
"""
洛谷私信通知助手 (桌面版)
------------------------------------------------
功能：
  1. 首次运行时输入 __client_id / _uid 两个 Cookie 值，保存到程序同目录 config.json
  2. 使用 WebSocket 长连接监听洛谷私信推送（wss://ws.luogu.com.cn/ws）
  3. 收到新私信时弹出 Windows 系统通知
  4. 断线自动重连（指数退避），避免频繁请求

参考文档：https://0f-0b.github.io/luogu-api-docs/chat
"""

import os
import sys
import json
import time
import queue
import threading
import webbrowser
from datetime import datetime

import requests
import certifi

try:
    import websocket  # pip install websocket-client
except ImportError:
    print("缺少依赖 websocket-client，请先运行: pip install -r requirements.txt")
    sys.exit(1)

import tkinter as tk
from tkinter import ttk, messagebox

# Windows 原生 Toast 通知（可选依赖，缺失时自动降级为仅在窗口内提示）
try:
    from win11toast import notify as win_toast_notify
    HAS_TOAST = True
except Exception:
    HAS_TOAST = False


# ======================== 常量配置 ========================

APP_NAME = "洛谷私信通知助手"
APP_VERSION = "1.0"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 LuoguPMNotifier/" + APP_VERSION)

WS_DEFAULT_SERVER = "wss://ws.luogu.com.cn/ws"
WS_CONFIG_URL = "https://www.luogu.com.cn/_lfe/config"
WS_CONFIG_EXPIRE = 24 * 60 * 60  # 24 小时缓存

CHAT_LIST_URL = "https://www.luogu.com.cn/chat"

RECONNECT_BASE_DELAY = 2       # 秒
RECONNECT_MAX_DELAY = 60       # 秒
HEARTBEAT_IGNORE_TYPE = "heartbeat"

AVATAR_CACHE_EXPIRE = 24 * 60 * 60  # 头像本地缓存 24 小时
PRIVACY_PLACEHOLDER = "你有一条新的消息"

# 部分 Windows 环境找不到系统根证书，导致 SSL: CERTIFICATE_VERIFY_FAILED，
# 这里显式指定 certifi 提供的证书包路径来解决。
try:
    CA_BUNDLE = certifi.where()
except Exception:
    CA_BUNDLE = None

# 配色（浅色现代风）
COLOR_BG = "#f5f6fa"
COLOR_HEADER_BG = "#2f3640"
COLOR_HEADER_FG = "#ffffff"
COLOR_ACCENT = "#4a69bd"
COLOR_TEXT = "#2f3542"
COLOR_MUTED = "#718093"
COLOR_OK = "#27ae60"
COLOR_WARN = "#e1b12c"
COLOR_ERR = "#c0392b"
COLOR_CARD_BG = "#ffffff"


# ======================== 配置文件读写 ========================

def base_dir():
    """获取程序所在目录（兼容 PyInstaller 打包后的 exe）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(base_dir(), "config.json")


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(patch: dict):
    cfg = load_config()
    cfg.update(patch)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


# ======================== 洛谷 API 封装 ========================

class LuoguAPIError(Exception):
    pass


def build_session(client_id: str, uid: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://www.luogu.com.cn/",
    })
    if CA_BUNDLE:
        s.verify = CA_BUNDLE
    # 洛谷通过 Cookie 中的 __client_id + _uid 识别登录态
    s.cookies.set("__client_id", client_id, domain="www.luogu.com.cn")
    s.cookies.set("_uid", uid, domain="www.luogu.com.cn")
    return s


def fetch_current_user(session: requests.Session):
    """校验登录态并获取当前用户信息 (uid, name)。失败返回 None。"""
    attempts = [
        {"headers": {"x-luogu-type": "content-only"}, "path": ["currentUser"]},
        {"headers": {"x-lentille-request": "content-only"}, "path": ["user", "currentUser"]},
    ]
    for attempt in attempts:
        try:
            resp = session.get(CHAT_LIST_URL, headers=attempt["headers"], timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for key in attempt["path"]:
                if isinstance(data, dict) and key in data and data[key]:
                    data = data[key]
                else:
                    data = None
                    break
            if isinstance(data, dict) and data.get("uid"):
                return {
                    "uid": data.get("uid"),
                    "name": data.get("name") or data.get("username") or "",
                }
        except Exception:
            continue
    return None


def fetch_ws_server(session: requests.Session):
    """获取 WebSocket 服务器地址，带 24h 本地缓存。失败时回退到默认地址。"""
    cfg = load_config()
    cached = cfg.get("ws_server")
    cached_time = cfg.get("ws_server_time", 0)
    if cached and (time.time() - cached_time < WS_CONFIG_EXPIRE):
        return cached
    try:
        resp = session.get(WS_CONFIG_URL, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            server = (data.get("ws") or {}).get("server")
            if server:
                save_config({"ws_server": server, "ws_server_time": time.time()})
                return server
    except Exception:
        pass
    return WS_DEFAULT_SERVER


# ======================== WebSocket 后台线程 ========================

class ChatListener(threading.Thread):
    """
    在后台线程中维护 WebSocket 连接，收到私信后通过 queue 通知主线程 UI。
    event_queue 中放入 dict:
        {"type": "status", "value": "connected"/"connecting"/"disconnected"/"error", "detail": str}
        {"type": "message", "sender_name": str, "sender_uid": int, "content": str, "time": str}
    """

    def __init__(self, client_id, uid, event_queue: queue.Queue):
        super().__init__(daemon=True)
        self.client_id = client_id
        self.uid = str(uid)
        self.event_queue = event_queue
        self._stop_flag = threading.Event()
        self.ws_app = None
        self.reconnect_attempts = 0
        self.session = build_session(client_id, self.uid)
        self.self_name = ""

    def stop(self):
        self._stop_flag.set()
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass

    def emit(self, event: dict):
        self.event_queue.put(event)

    def run(self):
        # 1) 校验登录态
        user = fetch_current_user(self.session)
        if not user:
            self.emit({"type": "status", "value": "error",
                       "detail": "登录信息校验失败，请检查 __client_id / _uid 是否正确或已过期"})
            return
        self.self_name = user["name"] or str(user["uid"])
        self.emit({"type": "login_ok", "uid": user["uid"], "name": self.self_name})

        # 2) 主循环：连接 + 断线重连
        while not self._stop_flag.is_set():
            self.emit({"type": "status", "value": "connecting", "detail": "正在连接 WebSocket..."})
            server = fetch_ws_server(self.session)
            try:
                self._connect_once(server)
            except Exception as e:
                self.emit({"type": "status", "value": "error", "detail": f"连接异常: {e}"})

            if self._stop_flag.is_set():
                break

            # 断线重连（指数退避）
            delay = min(RECONNECT_MAX_DELAY, RECONNECT_BASE_DELAY * (2 ** self.reconnect_attempts))
            self.reconnect_attempts += 1
            self.emit({"type": "status", "value": "disconnected",
                       "detail": f"连接已断开，{delay} 秒后重试（第 {self.reconnect_attempts} 次）"})
            for _ in range(delay * 10):
                if self._stop_flag.is_set():
                    break
                time.sleep(0.1)

    def _connect_once(self, server_url):
        cookie_header = f"__client_id={self.client_id}; _uid={self.uid}"

        def on_open(ws):
            self.reconnect_attempts = 0
            self.emit({"type": "status", "value": "connected", "detail": "已连接"})
            try:
                ws.send(json.dumps({
                    "channel": "chat",
                    "channel_param": self.uid,
                    "type": "join_channel",
                }))
            except Exception as e:
                self.emit({"type": "status", "value": "error", "detail": f"加入频道失败: {e}"})

        def on_message(ws, raw):
            try:
                data = json.loads(raw)
            except Exception:
                return
            ws_type = data.get("_ws_type")
            if ws_type == HEARTBEAT_IGNORE_TYPE:
                return
            if ws_type == "server_broadcast":
                message = data.get("message") or {}
                sender = message.get("sender") or {}
                sender_uid = str(sender.get("uid", ""))
                if sender_uid and sender_uid != self.uid:
                    self.emit({
                        "type": "message",
                        "sender_name": sender.get("name") or sender.get("username") or "未知用户",
                        "sender_uid": sender.get("uid"),
                        "content": message.get("content") or "(无内容)",
                        "time": datetime.now().strftime("%H:%M:%S"),
                    })
            elif ws_type == "exclusive_kickoff":
                self.emit({"type": "status", "value": "error", "detail": "被其他客户端顶下线，正在重连"})
                try:
                    ws.close()
                except Exception:
                    pass

        def on_error(ws, error):
            self.emit({"type": "status", "value": "error", "detail": f"WebSocket 错误: {error}"})

        def on_close(ws, code, msg):
            pass  # 由外层主循环统一处理重连提示

        self.ws_app = websocket.WebSocketApp(
            server_url,
            header=[f"Cookie: {cookie_header}", f"User-Agent: {USER_AGENT}"],
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        # ping_interval 做心跳保活，30s 一次；显式指定证书包避免部分电脑上的
        # SSL: CERTIFICATE_VERIFY_FAILED 报错
        sslopt = {"ca_certs": CA_BUNDLE} if CA_BUNDLE else None
        self.ws_app.run_forever(ping_interval=30, ping_timeout=10, sslopt=sslopt)


# ======================== 系统通知 ========================

AVATAR_CACHE_DIR = os.path.join(base_dir(), "avatar_cache")


def get_local_avatar_path(sender_uid):
    """
    下载头像到本地并返回绝对路径。
    win11toast 对远程 URL 图标支持不稳定，本地文件路径才能稳定显示。
    """
    if not sender_uid:
        return None
    try:
        os.makedirs(AVATAR_CACHE_DIR, exist_ok=True)
        local_path = os.path.join(AVATAR_CACHE_DIR, f"{sender_uid}.png")
        if os.path.exists(local_path) and (time.time() - os.path.getmtime(local_path) < AVATAR_CACHE_EXPIRE):
            return os.path.abspath(local_path)
        url = f"https://cdn.luogu.com.cn/upload/usericon/{sender_uid}.png"
        resp = requests.get(url, timeout=8, headers={"User-Agent": USER_AGENT},
                             verify=(CA_BUNDLE if CA_BUNDLE else True))
        if resp.status_code == 200 and resp.content:
            with open(local_path, "wb") as f:
                f.write(resp.content)
            return os.path.abspath(local_path)
    except Exception:
        pass
    return None


def show_system_notification(title: str, body: str, sender_uid=None, show_avatar=True):
    def _do():
        icon_path = get_local_avatar_path(sender_uid) if show_avatar else None
        try:
            if HAS_TOAST:
                kwargs = {"on_click": lambda args: open_chat(sender_uid)}
                if icon_path:
                    kwargs["icon"] = icon_path
                win_toast_notify(title, body, **kwargs)
                return
        except Exception:
            pass
        # 兜底：没有 win11toast 或调用失败时，静默忽略（窗口内日志仍会显示）

    threading.Thread(target=_do, daemon=True).start()


def open_chat(uid=None):
    url = f"https://www.luogu.com.cn/chat?uid={uid}" if uid else "https://www.luogu.com.cn/chat"
    webbrowser.open(url)


# ======================== 登录信息输入弹窗 ========================

class LoginDialog(tk.Toplevel):
    """首次运行 / 重新设置账号信息时弹出的输入框"""

    def __init__(self, master, on_saved, existing=None):
        super().__init__(master)
        self.on_saved = on_saved
        self.title("账号信息设置")
        self.geometry("440x360")
        self.resizable(False, False)
        self.configure(bg=COLOR_BG)
        self.transient(master)
        self.grab_set()

        pad = {"padx": 24, "pady": 6}

        tk.Label(self, text="配置洛谷登录信息", bg=COLOR_BG, fg=COLOR_TEXT,
                 font=("微软雅黑", 14, "bold")).pack(anchor="w", **pad)

        tip = ("请在浏览器登录洛谷后，按 F12 打开开发者工具 → Application(应用)/存储\n"
               "→ Cookie → https://www.luogu.com.cn，找到并复制以下两个值：\n"
               "  __client_id  和  _uid")
        tk.Label(self, text=tip, bg=COLOR_BG, fg=COLOR_MUTED, justify="left",
                 font=("微软雅黑", 9)).pack(anchor="w", **pad)

        form = tk.Frame(self, bg=COLOR_BG)
        form.pack(fill="x", **pad)

        tk.Label(form, text="__client_id", bg=COLOR_BG, fg=COLOR_TEXT,
                 font=("微软雅黑", 10)).grid(row=0, column=0, sticky="w", pady=8)
        self.client_id_var = tk.StringVar(value=(existing or {}).get("client_id", ""))
        self.client_id_entry = tk.Entry(form, textvariable=self.client_id_var, width=38,
                                         font=("Consolas", 10))
        self.client_id_entry.grid(row=0, column=1, pady=8, padx=8)

        tk.Label(form, text="_uid", bg=COLOR_BG, fg=COLOR_TEXT,
                 font=("微软雅黑", 10)).grid(row=1, column=0, sticky="w", pady=8)
        self.uid_var = tk.StringVar(value=(existing or {}).get("uid", ""))
        self.uid_entry = tk.Entry(form, textvariable=self.uid_var, width=38, font=("Consolas", 10))
        self.uid_entry.grid(row=1, column=1, pady=8, padx=8)

        self.error_label = tk.Label(self, text="", bg=COLOR_BG, fg=COLOR_ERR, font=("微软雅黑", 9))
        self.error_label.pack(anchor="w", padx=24)

        warn = "⚠ 这两个值等同于登录凭证，请勿泄露给他人，也不要分享 config.json 文件。"
        tk.Label(self, text=warn, bg=COLOR_BG, fg=COLOR_WARN, wraplength=390,
                 justify="left", font=("微软雅黑", 8)).pack(anchor="w", padx=24, pady=(4, 0))

        btn_frame = tk.Frame(self, bg=COLOR_BG)
        btn_frame.pack(fill="x", padx=24, pady=20)

        save_btn = tk.Button(btn_frame, text="保存并连接", bg=COLOR_ACCENT, fg="white",
                              activebackground="#3c5aa6", activeforeground="white",
                              relief="flat", font=("微软雅黑", 10, "bold"),
                              padx=16, pady=8, command=self._on_save)
        save_btn.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_save(self):
        client_id = self.client_id_var.get().strip()
        uid = self.uid_var.get().strip()
        if not client_id or not uid:
            self.error_label.config(text="两个字段都不能为空")
            return
        if not uid.isdigit():
            self.error_label.config(text="_uid 应为纯数字")
            return
        save_config({"client_id": client_id, "uid": uid})
        self.destroy()
        self.on_saved(client_id, uid)

    def _on_close(self):
        # 如果之前没有任何配置，用户直接关闭窗口则退出整个程序
        if not load_config().get("client_id"):
            self.master.destroy()
        else:
            self.destroy()


# ======================== 主窗口 ========================

class NotifierApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("560x660")
        self.minsize(480, 420)
        self.configure(bg=COLOR_BG)

        self.event_queue = queue.Queue()
        self.listener: ChatListener | None = None
        self.msg_count = 0

        cfg0 = load_config()
        self.privacy_mode = tk.BooleanVar(value=bool(cfg0.get("privacy_mode", False)))
        # 是否接收消息的开关，仅存在于本次运行的内存中，不写入 config.json，
        # 每次重新打开程序都恢复为默认"接收"状态
        self.receive_enabled = tk.BooleanVar(value=True)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        cfg = load_config()
        if cfg.get("client_id") and cfg.get("uid"):
            self._start_listener(cfg["client_id"], cfg["uid"])
        else:
            self.after(200, self._open_login_dialog)

        self.after(200, self._poll_queue)

    # ---------- UI 构建 ----------

    def _build_ui(self):
        # 顶部标题栏
        header = tk.Frame(self, bg=COLOR_HEADER_BG, height=64)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(header, text=APP_NAME, bg=COLOR_HEADER_BG, fg=COLOR_HEADER_FG,
                 font=("微软雅黑", 15, "bold")).pack(side="left", padx=20)

        self.status_dot = tk.Canvas(header, width=14, height=14, bg=COLOR_HEADER_BG,
                                     highlightthickness=0)
        self.status_dot.pack(side="right", pady=25, padx=(0, 8))
        self._status_circle = self.status_dot.create_oval(2, 2, 12, 12, fill=COLOR_MUTED, outline="")

        self.status_text = tk.Label(header, text="未连接", bg=COLOR_HEADER_BG, fg=COLOR_HEADER_FG,
                                     font=("微软雅黑", 10))
        self.status_text.pack(side="right", padx=(0, 4), pady=25)

        # 底部按钮栏：优先 pack 到窗口底部（side="bottom"），无论窗口被拉多矮，
        # 这一整条都会保持贴底可见，中间的日志区会自动收缩让位，而不会把按钮挤没。
        bottom = tk.Frame(self, bg=COLOR_BG, height=52)
        bottom.pack(fill="x", side="bottom", padx=16, pady=(4, 12))
        bottom.pack_propagate(False)

        btn_row = tk.Frame(bottom, bg=COLOR_BG)
        btn_row.pack(fill="x", expand=True)

        tk.Button(btn_row, text="测试通知", bg="#dcdde1", fg=COLOR_TEXT, relief="flat",
                  font=("微软雅黑", 9), padx=10, pady=6,
                  command=self._test_notification).pack(side="left")

        tk.Button(btn_row, text="清空日志", bg="#dcdde1", fg=COLOR_TEXT, relief="flat",
                  font=("微软雅黑", 9), padx=10, pady=6,
                  command=self._clear_log).pack(side="left", padx=6)

        self.receive_btn = tk.Button(btn_row, text="⏸ 暂停接收消息", bg="#dcdde1", fg=COLOR_TEXT,
                                      relief="flat", font=("微软雅黑", 9), padx=10, pady=6,
                                      command=self._toggle_receive)
        self.receive_btn.pack(side="left", padx=6)

        self.count_label = tk.Label(btn_row, text="共收到 0 条私信", bg=COLOR_BG, fg=COLOR_MUTED,
                                     font=("微软雅黑", 9))
        self.count_label.pack(side="right")

        # 一条分隔线，让底部按钮栏和日志区在视觉上分开
        tk.Frame(self, bg="#dcdde1", height=1).pack(fill="x", side="bottom")

        # 用户信息卡片
        info_card = tk.Frame(self, bg=COLOR_CARD_BG, bd=0)
        info_card.pack(fill="x", padx=16, pady=(16, 8))
        self.user_label = tk.Label(info_card, text="尚未登录", bg=COLOR_CARD_BG, fg=COLOR_TEXT,
                                    font=("微软雅黑", 11, "bold"), anchor="w")
        self.user_label.pack(side="left", padx=14, pady=12)

        self.relogin_btn = tk.Button(info_card, text="重新设置账号", bg="#dcdde1", fg=COLOR_TEXT,
                                      relief="flat", font=("微软雅黑", 9), padx=10, pady=4,
                                      command=self._open_login_dialog)
        self.relogin_btn.pack(side="right", padx=14, pady=10)

        # 隐私模式开关
        privacy_bar = tk.Frame(self, bg=COLOR_CARD_BG)
        privacy_bar.pack(fill="x", padx=16, pady=(0, 8))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Privacy.TCheckbutton", background=COLOR_CARD_BG, foreground=COLOR_TEXT,
                         font=("微软雅黑", 10))
        style.map("Privacy.TCheckbutton", background=[("active", COLOR_CARD_BG)])

        self.privacy_check = ttk.Checkbutton(
            privacy_bar, text="隐私模式（通知和日志只显示“你有一条新的消息”，不显示发送人和内容）",
            variable=self.privacy_mode, style="Privacy.TCheckbutton",
            command=self._on_privacy_toggle
        )
        self.privacy_check.pack(side="left", padx=14, pady=(6, 10))

        # 消息日志区（放在最后 pack，自动伸缩填满剩余空间；窗口变矮时它会先收缩，
        # 而不会影响上面已固定好位置的头部信息和底部按钮栏）
        log_frame = tk.Frame(self, bg=COLOR_BG)
        log_frame.pack(fill="both", expand=True, padx=16, pady=8)

        tk.Label(log_frame, text="消息记录", bg=COLOR_BG, fg=COLOR_MUTED,
                 font=("微软雅黑", 9)).pack(anchor="w")

        text_wrap = tk.Frame(log_frame, bg="#dcdde1", bd=1)
        text_wrap.pack(fill="both", expand=True, pady=(4, 0))

        scrollbar = tk.Scrollbar(text_wrap)
        scrollbar.pack(side="right", fill="y")

        self.log_text = tk.Text(text_wrap, bg=COLOR_CARD_BG, fg=COLOR_TEXT, wrap="word",
                                 font=("微软雅黑", 10), relief="flat", bd=0,
                                 yscrollcommand=scrollbar.set, state="disabled", padx=10, pady=8)
        self.log_text.pack(fill="both", expand=True)
        scrollbar.config(command=self.log_text.yview)

        self.log_text.tag_config("time", foreground=COLOR_MUTED, font=("Consolas", 9))
        self.log_text.tag_config("sender", foreground=COLOR_ACCENT, font=("微软雅黑", 10, "bold"))
        self.log_text.tag_config("content", foreground=COLOR_TEXT)
        self.log_text.tag_config("system", foreground=COLOR_MUTED, font=("微软雅黑", 9, "italic"))

        if not HAS_TOAST:
            self._append_log_system("提示：未安装 win11toast，系统通知将不可用（仍会记录到日志）。"
                                     "可运行 pip install win11toast 后重启程序启用。")

    # ---------- 登录流程 ----------

    def _open_login_dialog(self):
        cfg = load_config()
        LoginDialog(self, on_saved=self._on_login_saved, existing=cfg)

    def _toggle_receive(self):
        self.receive_enabled.set(not self.receive_enabled.get())
        if self.receive_enabled.get():
            self.receive_btn.config(text="⏸ 暂停接收消息", bg="#dcdde1", fg=COLOR_TEXT)
            self._append_log_system("已恢复接收消息")
        else:
            self.receive_btn.config(text="▶ 已暂停，点击恢复接收", bg=COLOR_WARN, fg="white")
            self._append_log_system("已暂停接收消息（期间不会弹通知，也不会记录日志）")

    def _on_privacy_toggle(self):
        enabled = self.privacy_mode.get()
        save_config({"privacy_mode": enabled})
        self._append_log_system(f"隐私模式已{'开启' if enabled else '关闭'}")

    def _on_login_saved(self, client_id, uid):
        if self.listener:
            self.listener.stop()
        self._append_log_system("账号信息已更新，正在重新连接...")
        self._start_listener(client_id, uid)

    def _start_listener(self, client_id, uid):
        self.listener = ChatListener(client_id, uid, self.event_queue)
        self.listener.start()

    # ---------- 队列轮询（后台线程 -> UI 线程） ----------

    def _poll_queue(self):
        try:
            while True:
                event = self.event_queue.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def _handle_event(self, event):
        etype = event.get("type")
        if etype == "login_ok":
            self.user_label.config(text=f"已登录：{event['name']}（UID: {event['uid']}）")
        elif etype == "status":
            value = event.get("value")
            detail = event.get("detail", "")
            color_map = {
                "connected": (COLOR_OK, "已连接"),
                "connecting": (COLOR_WARN, "连接中..."),
                "disconnected": (COLOR_WARN, "已断开"),
                "error": (COLOR_ERR, "错误"),
            }
            color, text = color_map.get(value, (COLOR_MUTED, value))
            self.status_dot.itemconfig(self._status_circle, fill=color)
            self.status_text.config(text=text)
            if detail:
                self._append_log_system(detail)
        elif etype == "message":
            if not self.receive_enabled.get():
                return  # 已暂停接收，静默忽略（不弹通知、不记日志、不计数）

            self.msg_count += 1
            self.count_label.config(text=f"共收到 {self.msg_count} 条私信")
            privacy = self.privacy_mode.get()

            if privacy:
                self._append_log_message(event["time"], None, PRIVACY_PLACEHOLDER, privacy=True)
                show_system_notification(
                    title=APP_NAME,
                    body=PRIVACY_PLACEHOLDER,
                    sender_uid=None,
                    show_avatar=False,
                )
            else:
                self._append_log_message(event["time"], event["sender_name"], event["content"])
                show_system_notification(
                    title=f"新私信：来自 {event['sender_name']}",
                    body=event["content"],
                    sender_uid=event.get("sender_uid"),
                    show_avatar=True,
                )

    # ---------- 日志显示 ----------

    def _append_log_message(self, time_str, sender, content, privacy=False):
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{time_str}] ", "time")
        if privacy:
            self.log_text.insert("end", f"{content}\n", "system")
        else:
            self.log_text.insert("end", f"{sender}: ", "sender")
            self.log_text.insert("end", f"{content}\n", "content")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _append_log_system(self, text):
        self.log_text.config(state="normal")
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {text}\n", "system")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _test_notification(self):
        if self.privacy_mode.get():
            show_system_notification(APP_NAME, PRIVACY_PLACEHOLDER, sender_uid=None, show_avatar=False)
            self._append_log_system("已触发测试通知（隐私模式）")
        else:
            show_system_notification("测试通知：来自 test", "test", sender_uid=None, show_avatar=False)
            self._append_log_system("已触发测试通知")

    # ---------- 关闭 ----------

    def _on_close(self):
        if self.listener:
            self.listener.stop()
        self.destroy()


# ======================== 入口 ========================

def main():
    app = NotifierApp()
    app.mainloop()


if __name__ == "__main__":
    main()