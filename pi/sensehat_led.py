#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sense HAT 状态灯服务 —— 把树莓派的 Sense HAT 当作 agent 状态指示灯。

状态:
  idle     灭灯
  busy     黄灯低频闪烁    —— 处理中
  done     绿灯常亮        —— 运行完毕
  confirm  红灯常亮        —— 需要确认（强制亮灯，不受开关影响）

摇杆:
  ↑ / ↓     亮度 +/-
  按下      指示灯开 / 关（省得不用的时候一直亮）
  询问模式 (GET /ask 阻塞等待中): ↑ 或 按下 = 同意, ↓ = 拒绝

HTTP API (默认 0.0.0.0:8765):
  GET /                       状态 JSON
  GET /ping
  GET /state/<idle|busy|done|confirm>
  GET /brightness/<1-8|up|down>
  GET /enable/<on|off|toggle|1|0>
  GET /ask?timeout=900        红灯常亮并阻塞等待摇杆回答 -> {"answer":"yes|no|timeout"}
  GET /answer/<yes|no>

不依赖 sense_hat 库：直接写 /dev/fbX（16bpp RGB565，8x8）并读 evdev 摇杆事件。
"""

import argparse
import glob
import json
import mmap
import os
import pwd
import select
import signal
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "1.0"

W = H = 8
NPIX = W * H

BLACK = (0, 0, 0)
YELLOW = (255, 176, 0)
GREEN = (0, 255, 0)
RED = (255, 40, 0)
WHITE = (255, 255, 255)
BLUE = (0, 90, 255)

BLINK_ON = 0.65          # 黄灯亮时长（秒）
BLINK_OFF = 0.65         # 黄灯灭时长（秒）
FPS = 20                 # 渲染频率
REFRESH = 2.0            # 每 N 秒强制重写一次 framebuffer（防止驱动丢帧）

STATES = ("idle", "busy", "done", "confirm")

# 汇总多个会话时谁说了算：需要确认 > 处理中 > 运行完毕
PRIORITY = {"confirm": 3, "busy": 2, "done": 1}
BUSY_TTL = 3600.0        # busy 超过这么久没有新事件就自愈，防止 app 崩了留下常亮黄灯
ASK_KEY = "_ask"         # /ask 阻塞询问占用一个虚拟会话
MANUAL_KEY = "manual"    # 不带 session 的命令（sense busy 之类）

KEY_EV = 1
KEYMAP = {103: "up", 108: "down", 105: "left", 106: "right", 28: "enter"}
EVENT_FMT = "llHHi"      # aarch64: timeval(16) + type(2) + code(2) + value(4)
EVENT_SIZE = struct.calcsize(EVENT_FMT)


def home_dir():
    if os.getuid() == 0:
        return "/root"
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return os.path.expanduser("~")


class Matrix:
    """Sense HAT 8x8 点阵屏 (framebuffer 直写)。"""

    def __init__(self, dev, sysdir, bpp):
        self.dev = dev
        self.sysdir = sysdir
        self.bpp = bpp
        self.fd = os.open(dev, os.O_RDWR)
        self.mem = mmap.mmap(self.fd, self._size(), mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE)
        self.lock = threading.Lock()

    @staticmethod
    def _read(path):
        with open(path) as fh:
            return fh.read().strip()

    def _size(self):
        x, y = (int(v) for v in self._read(os.path.join(self.sysdir, "virtual_size")).split(","))
        stride_path = os.path.join(self.sysdir, "stride")
        if os.path.exists(stride_path):
            stride = int(self._read(stride_path))
        else:
            stride = x * (self.bpp // 8)
        return stride * y

    def _encode(self, pixels):
        out = bytearray()
        if self.bpp == 16:
            for r, g, b in pixels:
                packed = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                out += struct.pack("<H", packed)
        else:
            for r, g, b in pixels:
                out += bytes((b, g, r, 0))
        return bytes(out)

    def draw(self, pixels):
        data = self._encode(pixels)
        with self.lock:
            self.mem[0:len(data)] = data


def find_matrix_paths():
    """按名字找 Sense HAT 的 framebuffer，避免 HDMI 抢占导致 fb 编号漂移。"""
    for sysdir in sorted(glob.glob("/sys/class/graphics/fb*")):
        try:
            name = Matrix._read(os.path.join(sysdir, "name")).lower()
        except OSError:
            continue
        if "sense" in name:
            bpp = int(Matrix._read(os.path.join(sysdir, "bits_per_pixel")))
            dev = "/dev/" + os.path.basename(sysdir)
            if not os.path.exists(dev):
                continue
            return dev, sysdir, bpp
    raise RuntimeError("找不到 Sense HAT 的 framebuffer（检查 /sys/class/graphics/fb*/name == 'RPi-Sense FB'）")


def find_joystick():
    for path in glob.glob("/sys/class/input/event*"):
        try:
            name = Matrix._read(os.path.join(path, "device", "name"))
        except OSError:
            continue
        if name.strip().lower() == "raspberry pi sense hat joystick":
            return os.path.join("/dev/input", os.path.basename(path))
    return None


class Service:
    def __init__(self, matrix, state_file, brightness=8, verbose=False):
        self.matrix = matrix
        self.state_file = state_file
        self.verbose = verbose
        self.lock = threading.RLock()

        self.sessions = {}       # session id -> (state, 最后更新时间)
        self.enabled = True
        self.brightness = max(1, min(8, brightness))

        self.ask_pending = False
        self.ask_answer = None
        self.ask_event = threading.Event()

        self.overlay = None          # (deadline, pixels, respect_brightness)
        self.overlay_token = None
        self.last_key = (None, 0.0)
        self._last_frame = None
        self._last_write = 0.0
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            with open(self.state_file) as fh:
                data = json.load(fh)
            self.enabled = bool(data.get("enabled", True))
            self.brightness = max(1, min(8, int(data.get("brightness", self.brightness))))
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"enabled": self.enabled, "brightness": self.brightness}, fh)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            self.log("保存状态失败: %s" % exc)

    def log(self, msg):
        if self.verbose:
            sys.stderr.write("[sensehat-led] %s\n" % msg)
            sys.stderr.flush()

    # ---------- 状态操作 ----------
    def set_state(self, state, session=None, event=None):
        """记录某个会话的状态。state=idle 表示这个会话退出汇总。

        这里要做两件防竞态的事——PreToolUse / PermissionRequest / PostToolUse
        是并行发出来的，到达顺序不保证：
          1. 授权提示还挂着时（confirm），不允许 busy 把它压回黄灯；
             只有 PostToolUse（工具真的跑起来了 = 授权已处理）或 Stop 才能解除。
          2. 一轮刚结束（done）的头两秒，忽略掉队的 PostToolUse，别把绿灯又染黄。
        """
        if state not in STATES:
            return False
        session = session or MANUAL_KEY
        now = time.monotonic()
        with self.lock:
            current = self.sessions.get(session)
            if state == "busy" and current:
                cur_state, cur_ts = current
                if cur_state == "confirm" and event != "PostToolUse":
                    self.log("state[%s] 保持 confirm，忽略 %s" % (session, event or "busy"))
                    return True
                if cur_state == "done" and event == "PostToolUse" and now - cur_ts < 2.0:
                    return True
            if state == "idle":
                if session == MANUAL_KEY:
                    self.sessions.clear()          # `sense idle` = 全部清空
                else:
                    self.sessions.pop(session, None)
            else:
                self.sessions[session] = (state, now)
        self.log("state[%s] -> %s" % (session, state))
        return True

    def aggregate(self, now=None):
        """把所有会话汇成一个灯效状态，多窗口时按优先级取最高的那个。"""
        now = now or time.monotonic()
        best, best_rank = "idle", 0
        with self.lock:
            for session, (state, ts) in list(self.sessions.items()):
                if state == "busy" and now - ts > BUSY_TTL:
                    self.sessions.pop(session, None)
                    self.log("state[%s] 过期自动清除" % session)
                    continue
                rank = PRIORITY.get(state, 0)
                if rank > best_rank:
                    best, best_rank = state, rank
        return best

    def set_brightness(self, value, feedback=True):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        with self.lock:
            self.brightness = max(0, min(8, value))
            brightness = self.brightness
            self._save()
        if feedback:
            self._flash_brightness(brightness)
        self.log("brightness -> %d" % brightness)
        return brightness

    def set_enabled(self, value, feedback=True):
        with self.lock:
            self.enabled = bool(value)
            enabled = self.enabled
            self._save()
        if feedback:
            self._flash_toggle(enabled)
        self.log("enabled -> %s" % enabled)
        return enabled

    def status(self):
        now = time.monotonic()
        with self.lock:
            sessions = {key: "%s(%.0fs)" % (val[0], now - val[1])
                        for key, val in self.sessions.items()}
            return {
                "ok": True,
                "version": VERSION,
                "state": self.aggregate(now),
                "sessions": sessions,
                "enabled": self.enabled,
                "brightness": self.brightness,
                "ask_pending": self.ask_pending,
                "framebuffer": self.matrix.dev,
                "joystick": self.joystick_path,
            }

    # ---------- 摇杆 ----------
    joystick_path = None

    def on_key(self, key):
        with self.lock:
            ask = self.ask_pending
        if self.verbose:
            self.log("joystick: %s" % key)
        if ask:
            if key in ("up", "enter"):
                self._answer("yes")
            elif key == "down":
                self._answer("no")
            return
        if key == "up":
            with self.lock:
                target = self.brightness + 1
            self.set_brightness(target)
        elif key == "down":
            with self.lock:
                target = self.brightness - 1
            self.set_brightness(target)
        elif key == "enter":
            with self.lock:
                target = not self.enabled
            self.set_enabled(target)

    def _flash(self, pixels, seconds, respect_brightness=True):
        token = object()
        with self.lock:
            self.overlay = (time.monotonic() + seconds, pixels, respect_brightness)
            self.overlay_token = token
        return token

    def _flash_brightness(self, level):
        # 底行按当前亮度点亮若干颗 LED，直观显示档位
        row = []
        for i in range(W):
            row.append(WHITE if i < level else BLACK)
        pixels = [BLACK] * NPIX
        pixels[(H - 1) * W:(H - 1) * W + W] = row
        self._flash(pixels, 0.9)

    def _flash_toggle(self, enabled):
        color = WHITE if enabled else BLACK
        self._flash([color] * NPIX, 0.15, respect_brightness=False)

    def _answer(self, value):
        with self.lock:
            if not self.ask_pending:
                return
            self.ask_answer = value
            self.ask_event.set()

    def ask(self, timeout):
        """红灯常亮并阻塞等待摇杆回答。"""
        token = object()
        with self.lock:
            if self.ask_pending:
                return {"ok": False, "error": "already asking"}
            self.ask_pending = True
            self.sessions[ASK_KEY] = ("confirm", time.monotonic())
            self.ask_answer = None
            self.ask_event = threading.Event()
            event = self.ask_event
            self.overlay_token = token
            self.overlay = None
        answered = event.wait(timeout)
        with self.lock:
            answer = self.ask_answer
            self.ask_pending = False
            self.ask_event = threading.Event()
            self.ask_answer = None
            if answer in ("yes", "no"):
                self.sessions.pop(ASK_KEY, None)
            # 超时：保留红灯，等你有空再按
        if answer == "yes":
            self._flash([GREEN] * NPIX, 0.5)
        elif answer == "no":
            self._flash([RED] * NPIX, 0.5)
        else:
            self._flash([YELLOW] * NPIX, 0.35)
        result = answer if answered else "timeout"
        self.log("ask -> %s" % result)
        return {"ok": True, "answer": result, "state": self.aggregate()}

    # ---------- 渲染 ----------
    def _frame(self, now):
        state = self.aggregate(now)
        with self.lock:
            enabled = self.enabled
            brightness = self.brightness
            ask = self.ask_pending
            overlay = self.overlay
            token = self.overlay_token

        visible = enabled or state == "confirm"   # 需要确认时强制亮灯

        if not visible or state == "idle":
            pixels = [BLACK] * NPIX
        elif state == "busy":
            lit = (now % (BLINK_ON + BLINK_OFF)) < BLINK_ON
            pixels = [YELLOW if lit else BLACK] * NPIX
        elif state == "done":
            pixels = [GREEN] * NPIX
        else:  # confirm
            pixels = [RED] * NPIX

        if overlay:
            deadline, overlay_pixels, respect = overlay
            if deadline > now:
                if respect:
                    scale = brightness / 8.0
                    pixels = [(int(r * scale), int(g * scale), int(b * scale))
                              for r, g, b in overlay_pixels]
                else:
                    pixels = overlay_pixels
                return pixels
            with self.lock:
                if self.overlay_token is token:
                    self.overlay = None
                    self.overlay_token = None

        if brightness < 8 and any(pixels):
            scale = brightness / 8.0
            pixels = [(int(r * scale), int(g * scale), int(b * scale)) for r, g, b in pixels]
        return pixels

    def render_loop(self, stop):
        while not stop.is_set():
            now = time.monotonic()
            try:
                pixels = self._frame(now)
                if pixels != self._last_frame or now - self._last_write > REFRESH:
                    self.matrix.draw(pixels)
                    self._last_frame = pixels
                    self._last_write = now
            except Exception as exc:                      # noqa: BLE001
                self.log("渲染异常: %s" % exc)
            time.sleep(1.0 / FPS)

    def joystick_loop(self, stop):
        while not stop.is_set():
            path = find_joystick()
            if path is None:
                stop.wait(3.0)
                continue
            self.joystick_path = path
            self.log("摇杆设备: %s" % path)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                self.log("打开摇杆失败: %s" % exc)
                stop.wait(3.0)
                continue
            try:
                while not stop.is_set():
                    ready, _, _ = select.select([fd], [], [], 1.0)
                    if not ready:
                        continue
                    data = os.read(fd, EVENT_SIZE * 64)
                    for off in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                        _, _, etype, code, value = struct.unpack_from(EVENT_FMT, data, off)
                        if etype == KEY_EV and value == 1 and code in KEYMAP:
                            self.on_key(KEYMAP[code])
            except OSError as exc:
                self.log("摇杆读取异常: %s" % exc)
            finally:
                os.close(fd)
            stop.wait(1.0)

    def selftest(self):
        for name, color in (("红", RED), ("绿", GREEN), ("黄", YELLOW), ("白", WHITE)):
            self.log("自检: %s" % name)
            self.matrix.draw([color] * NPIX)
            time.sleep(0.5)
            self.matrix.draw([BLACK] * NPIX)
            time.sleep(0.25)


def make_handler(svc, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sensehat-led/" + VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if svc.verbose:
                sys.stderr.write("[http] %s\n" % (fmt % args))

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                                  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if token and query.get("t", [None])[0] != token:
                self._json({"ok": False, "error": "bad token"}, 403)
                return
            parts = [p for p in parsed.path.split("/") if p]
            arg = query.get("v", [None])[0]
            try:
                result = self._dispatch(parts, query, arg)
            except Exception as exc:                       # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, 500)
                return
            if result is None:
                self._json({"ok": False, "error": "not found"}, 404)
            else:
                self._json(result)

        def _dispatch(self, parts, query, arg):
            head = parts[0] if parts else ""
            value = parts[1] if len(parts) > 1 else arg

            if head in ("", "status"):
                return svc.status()
            if head == "ping":
                return {"ok": True, "version": VERSION}
            if head == "state":
                if value in ("working", "processing", "run"):
                    value = "busy"
                elif value in ("finish", "finished", "ok"):
                    value = "done"
                elif value in ("ask", "wait", "waiting"):
                    value = "confirm"
                elif value in ("off", "clear", "none"):
                    value = "idle"
                session = query.get("session", [""])[0].strip()[:40] or None
                event = query.get("event", [""])[0].strip()[:24] or None
                if not svc.set_state(value or "", session, event):
                    return {"ok": False, "error": "state 必须是 %s" % "|".join(STATES)}
                return svc.status()
            if head == "brightness":
                if value in ("up", "+"):
                    target = svc.brightness + 1
                elif value in ("down", "-"):
                    target = svc.brightness - 1
                else:
                    target = value
                level = svc.set_brightness(target)
                if level is None:
                    return {"ok": False, "error": "brightness 需要 0-8 或 up/down"}
                return svc.status()
            if head == "enable":
                if value in ("toggle", "switch"):
                    value = not svc.enabled
                elif value in ("on", "1", "true", "yes"):
                    value = True
                elif value in ("off", "0", "false", "no"):
                    value = False
                else:
                    return {"ok": False, "error": "enable 需要 on|off|toggle"}
                svc.set_enabled(value)
                return svc.status()
            if head == "ask":
                try:
                    timeout = float(query.get("timeout", ["900"])[0])
                except ValueError:
                    timeout = 900.0
                timeout = max(1.0, min(3600.0, timeout))
                return svc.ask(timeout)
            if head == "answer":
                if value in ("yes", "y", "1", "true", "ok"):
                    svc._answer("yes")
                elif value in ("no", "n", "0", "false"):
                    svc._answer("no")
                else:
                    return {"ok": False, "error": "answer 需要 yes|no"}
                return svc.status()
            return None

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Sense HAT 状态灯服务")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--brightness", type=int, default=8, help="启动亮度 1-8（有存档时以存档为准）")
    parser.add_argument("--token", default=os.environ.get("SENSE_TOKEN", ""), help="可选的访问口令")
    parser.add_argument("--state-file",
                        default=os.path.join(home_dir(), ".config", "sensehat-led", "state.json"))
    parser.add_argument("--selftest", action="store_true", help="只做一次红绿黄白自检然后退出")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    dev, sysdir, bpp = find_matrix_paths()
    matrix = Matrix(dev, sysdir, bpp)
    svc = Service(matrix, args.state_file, args.brightness, args.verbose)
    svc.joystick_path = find_joystick()

    if args.selftest:
        svc.selftest()
        return 0

    print("[sensehat-led] %s 就绪：framebuffer=%s (%dbpp) 摇杆=%s 端口=%d"
          % (VERSION, dev, bpp, svc.joystick_path, args.port), flush=True)

    stop = threading.Event()
    threads = [
        threading.Thread(target=svc.render_loop, args=(stop,), daemon=True, name="render"),
        threading.Thread(target=svc.joystick_loop, args=(stop,), daemon=True, name="joystick"),
    ]
    for thread in threads:
        thread.start()

    server = ThreadingHTTPServer((args.bind, args.port), make_handler(svc, args.token))
    server.daemon_threads = True

    def shutdown(signum, _frame):
        stop.set()
        try:
            matrix.draw([BLACK] * NPIX)
        except Exception:                                  # noqa: BLE001
            pass
        threading.Thread(target=server.shutdown, daemon=True).start()
        print("[sensehat-led] 收到信号 %d，退出" % signum, flush=True)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            matrix.draw([BLACK] * NPIX)
        except Exception:                                  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
