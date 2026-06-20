"""
FRIDAY – Desktop HUD Orb
========================
一個無邊框、置頂、透明背景的桌面漂浮光球（JARVIS 風格）。
它綁一個 localhost UDP port 收「狀態」字串，依狀態變色並脈動：

    idle       – 待機，微弱青光，緩慢呼吸
    listening  – 在聽你講，青色，輕脈動
    user       – 你正在講話，綠色，較大脈動
    thinking   – 思考中，琥珀色，快速旋律脈動
    speaking   – FRIDAY 正在講話，亮青色，強烈脈動

由 agent_friday.py 自動啟動（也可單獨跑 `python -m friday.hud` 做視覺測試）。
與本檔通訊的協定極簡：UDP datagram，內容就是上面其中一個狀態字串。

操作：
    拖曳        – 移動光球
    Esc / 右鍵 – 關閉

繪製：若有 Pillow，使用抗鋸齒的高斯光暈 + 旋轉反應爐光環（漂亮、平滑）；
      沒有 Pillow 時退回純 Tk Canvas 同心圓（堪用但有鋸齒）。
"""

from __future__ import annotations

import io
import math
import os
import socket
import sys
import time


def _fix_tcl_paths() -> None:
    """uv 的標準版 Python 內含 Tcl/Tk 函式庫，但沒設好 TCL_LIBRARY/TK_LIBRARY，
    導致 tk.Tk() 找不到 init.tcl。這裡從 base_prefix 自動偵測並補上。"""
    import glob
    import sys

    base = sys.base_prefix
    for var, prefix, marker in (
        ("TCL_LIBRARY", "tcl", "init.tcl"),
        ("TK_LIBRARY", "tk", "tk.tcl"),
    ):
        if os.environ.get(var):
            continue
        for cand in sorted(glob.glob(os.path.join(base, "lib", prefix + "[0-9]*")), reverse=True):
            if os.path.exists(os.path.join(cand, marker)):
                os.environ[var] = cand
                break


_fix_tcl_paths()

import tkinter as tk  # noqa: E402  （必須在 _fix_tcl_paths() 之後 import）

try:  # 漂亮版需要 Pillow；沒有就退回純 Tk 畫法。
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk  # noqa: E402

    _HAS_PIL = True
except Exception:  # pragma: no cover - 視環境而定
    _HAS_PIL = False

# macOS 原生透明視窗後端（PyObjC）。Tk 9 的 -transparent 在部分 macOS 版本壞掉，
# 透明區會被畫成黑底；改用 NSWindow（setOpaque:NO + clearColor）才有真正透明。
try:  # pragma: no cover - 視環境而定
    import objc  # noqa: E402,F401
    from AppKit import (  # noqa: E402
        NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
        NSBitmapFormatAlphaNonpremultiplied, NSBitmapImageRep, NSColor,
        NSDeviceRGBColorSpace, NSEvent, NSEventMaskKeyDown, NSEventModifierFlagCommand,
        NSEventModifierFlagShift, NSImage, NSImageScaleNone,
        NSImageScaleProportionallyUpOrDown, NSImageView, NSMenu, NSMenuItem, NSScreen,
        NSStatusWindowLevel, NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
    )
    from Foundation import NSMakeRect, NSMakeSize, NSObject, NSTimer, NSData  # noqa: E402

    _HAS_COCOA = sys.platform == "darwin"
except Exception:
    _HAS_COCOA = False

# agent 端會用同一個 port 把狀態推進來；可用環境變數覆蓋。
HUD_PORT = int(os.environ.get("FRIDAY_HUD_PORT", "17865"))
# HUD → agent 的回送 port（目前用於「點一下切靜音」通知 agent）。
HUD_CTRL_PORT = int(os.environ.get("FRIDAY_HUD_CTRL_PORT", str(HUD_PORT + 1)))
# 記住上次位置（拖到哪、下次開在哪）。
_POS_FILE = os.path.expanduser("~/.friday_hud_pos")

# 字幕泡泡尺寸（顯示像素）。
CAPTION_W = 240
CAPTION_H = 40
CAPTION_TTL = 6.0     # 字幕顯示幾秒後自動淡出消失

# 視窗 / 光球尺寸（像素）。刻意做「小而精緻」：小徽章浮在桌面、背景全透明。
# macOS 的 Tk 透明視窗不會平滑混合大面積淡 alpha（會被壓成有色帶的硬圓盤），
# 所以這裡刻意把外暈收得很緊、很淡，避免大片漸層 → 不再有色帶。
CANVAS = 72           # 畫布邊長（正方形）— 小巧
CENTER = CANVAS / 2
SS = 3                # 超取樣倍率：先在 3x 解析度繪製再縮小 → 抗鋸齒更乾淨
FPS = 30

# 光球幾何（以「顯示像素」為單位；繪製時會乘上 SS）
# 走「圓角方塊徽章」風（Alexa / Cortana 感）：深色 squircle 底板 +
# 內圈一圈會流動旋轉的藍色發光環（有一段最亮的掃掠亮點繞圈跑）。
PLATE_INSET = 7       # 圓角方塊距畫布邊的內縮（留空間給貼身光暈）
PLATE_RADIUS = 16     # 圓角半徑（squircle 感）
PLATE_FILL = (0x17, 0x1a, 0x21)  # 底板填色（深炭黑、帶一點藍）
PLATE_FILL_A = 242    # 底板不透明度（近實體）
RING_R = 10           # 內圈發光環半徑
RING_W = 2            # 發光環線寬
RING_GLOW_BLUR = 3    # 發光環的外溢光暈模糊
HALO_R = 19           # 極緊的貼身外暈半徑
HALO_BLUR = 6         # 外暈柔化程度

# 每個狀態的：主色(RGB)、目標脈動強度(0~1)、脈動頻率(Hz)、光環轉速(turns/s)
STATES = {
    "idle":      ((0x2e, 0x9d, 0xc4), 0.14, 0.40, 0.04),
    "listening": ((0x3a, 0xd0, 0xf0), 0.32, 0.90, 0.10),
    "user":      ((0x46, 0xe0, 0x7a), 0.85, 1.70, 0.18),
    "thinking":  ((0xf0, 0xb0, 0x36), 0.55, 2.40, 0.45),
    "speaking":  ((0x4c, 0xe6, 0xff), 1.00, 2.10, 0.30),
}
DEFAULT_STATE = "idle"


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


class FrameRenderer:
    """用 Pillow 把目前狀態畫成一張 CANVAS×CANVAS 的 RGBA 影像（含動畫狀態）。
    Tk 與原生 macOS 兩個後端共用：每幀呼叫 render(...) 取得新影像。

    render 參數：
        state    – 目前對話狀態（決定顏色 / 基礎脈動 / 轉速）
        dt       – 距上一幀秒數
        ext_amp  – 外部真實音量包絡（0~1）；>0 時環會跟著聲音大小起伏
        mute     – 麥克風靜音：環轉灰並畫一道斜槓
    """

    def __init__(self) -> None:
        self.S = CANVAS * SS               # 超取樣畫布邊長
        self._center = self.S / 2
        self._halo_mask = self._soft_disc(HALO_R * SS, HALO_BLUR * SS)
        ins = PLATE_INSET * SS
        self._plate_box = (ins, ins, self.S - ins, self.S - ins)
        # 動畫狀態
        self.intensity = 0.0
        self.phase = 0.0
        self.spin = 0.0
        self.rgb = [float(v) for v in STATES[DEFAULT_STATE][0]]  # 平滑插值中的顏色
        self.amp_env = 0.0                 # 平滑後的外部音量包絡

    def _soft_disc(self, radius: float, blur: float) -> "Image.Image":
        """實心白圓 + 高斯模糊 → 邊緣平滑的灰階遮罩。"""
        m = Image.new("L", (self.S, self.S), 0)
        c = self.S / 2
        ImageDraw.Draw(m).ellipse((c - radius, c - radius, c + radius, c + radius), fill=255)
        return m.filter(ImageFilter.GaussianBlur(blur))

    def _sweep_ring(self, box, rgb, amp, dual: bool) -> "Image.Image":
        """藍色發光環，沿圓周有掃掠亮點跟著 spin 轉、其餘漸暗。
        dual=True 時兩個對稱反相亮點交織（speaking 用，更像 Cortana）。"""
        ring = Image.new("RGBA", (self.S, self.S), (0, 0, 0, 0))
        rd = ImageDraw.Draw(ring)
        w = int(RING_W * SS)
        base = tuple(min(255, v + 20) for v in rgb)
        bright = tuple(min(255, v + 130) for v in rgb)
        peaks = (self.spin, self.spin + 180.0) if dual else (self.spin,)
        step = 6
        for a in range(0, 360, step):
            t = 0.0
            for pk in peaks:
                d = abs(((a - pk + 180) % 360) - 180)
                tt = max(0.0, 1.0 - d / 150.0)
                t = max(t, tt * tt)
            col = tuple(int(base[i] + (bright[i] - base[i]) * t) for i in range(3))
            alpha = int(110 + 145 * t * (0.7 + 0.3 * amp))
            rd.arc(box, a, a + step + 1, fill=col + (alpha,), width=w)
        return ring

    def render(self, state: str, dt: float, ext_amp: float = 0.0,
               mute: bool = False) -> "Image.Image":
        target_rgb, target_amp, freq, spin_rate = STATES[state]

        # 顏色平滑插值（狀態切換不再瞬間跳色）。
        kc = min(1.0, dt * 5.0)
        self.rgb = [self.rgb[i] + (target_rgb[i] - self.rgb[i]) * kc for i in range(3)]
        rgb = tuple(int(round(v)) for v in self.rgb)

        # 外部音量包絡：上升快、下降稍慢 → 有殘響感。
        ka = (dt * 14.0) if ext_amp > self.amp_env else (dt * 7.0)
        self.amp_env += (ext_amp - self.amp_env) * min(1.0, ka)

        self.intensity += (target_amp - self.intensity) * min(1.0, dt * 6.0)
        self.phase += dt * freq * 2 * math.pi
        self.spin = (self.spin + dt * spin_rate * 360.0) % 360.0
        pulse = 0.5 + 0.5 * math.sin(self.phase)
        # 有真實音量時以它為主、否則用內建呼吸脈動（取大者，兩者並存自然）。
        amp = max(self.intensity * pulse, self.amp_env)

        if mute:  # 靜音：去飽和（轉灰）並壓暗
            lum = 0.30 * rgb[0] + 0.59 * rgb[1] + 0.11 * rgb[2]
            rgb = tuple(int(lum * 0.55 + v * 0.20) for v in rgb)
            amp *= 0.35

        c = self._center
        img = Image.new("RGBA", (self.S, self.S), (0, 0, 0, 0))

        # 1) 極淡、貼身的外暈（呼吸感；音量大時更亮）。
        g_halo = (0.10 + 0.26 * self.intensity + 0.30 * self.amp_env) * (0.82 + 0.18 * pulse)
        halo_alpha = self._halo_mask.point(lambda v: int(v * min(0.9, g_halo)))
        halo = Image.new("RGBA", (self.S, self.S), rgb + (0,))
        halo.putalpha(halo_alpha)
        img = Image.alpha_composite(img, halo)

        # 2) 圓角方塊底板：深炭黑實體面板（squircle）。
        plate = Image.new("RGBA", (self.S, self.S), (0, 0, 0, 0))
        ImageDraw.Draw(plate).rounded_rectangle(
            self._plate_box, radius=PLATE_RADIUS * SS,
            fill=PLATE_FILL + (PLATE_FILL_A,),
        )
        img = Image.alpha_composite(img, plate)

        # 3) 發光環：模糊外溢光暈 + 掃掠流動亮環（speaking 雙亮點）。
        rr = RING_R * SS
        box = (c - rr, c - rr, c + rr, c + rr)
        glow = Image.new("RGBA", (self.S, self.S), (0, 0, 0, 0))
        glow_col = tuple(min(255, v + 50) for v in rgb) + (int(120 + 90 * amp),)
        ImageDraw.Draw(glow).ellipse(box, outline=glow_col, width=int((RING_W + 3) * SS))
        glow = glow.filter(ImageFilter.GaussianBlur(RING_GLOW_BLUR * SS))
        img = Image.alpha_composite(img, glow)

        img = Image.alpha_composite(img, self._sweep_ring(box, rgb, amp, dual=(state == "speaking")))

        # 4) 靜音斜槓（覆蓋在環上）。
        if mute:
            slash = Image.new("RGBA", (self.S, self.S), (0, 0, 0, 0))
            d = (RING_R + 3) * SS
            ImageDraw.Draw(slash).line(
                (c - d * 0.7, c - d * 0.7, c + d * 0.7, c + d * 0.7),
                fill=(235, 235, 235, 230), width=int(RING_W * SS),
            )
            img = Image.alpha_composite(img, slash)

        return img.resize((CANVAS, CANVAS), Image.LANCZOS)


_FONT_CACHE: dict = {}


def _caption_font(size: int):
    f = _FONT_CACHE.get(size)
    if f is None:
        for path in ("/System/Library/Fonts/PingFang.ttc",
                     "/System/Library/Fonts/STHeiti Light.ttc",
                     "/System/Library/Fonts/Helvetica.ttc"):
            try:
                f = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
        if f is None:
            f = ImageFont.load_default()
        _FONT_CACHE[size] = f
    return f


def render_caption(text: str, alpha: float = 1.0) -> "Image.Image":
    """把一行字幕畫成圓角深色泡泡（超取樣抗鋸齒）。過長自動截斷加省略號。"""
    W, H = CAPTION_W * SS, CAPTION_H * SS
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=14 * SS,
                        fill=(0x12, 0x15, 0x1b, int(228 * alpha)))
    font = _caption_font(int(17 * SS))
    text = " ".join(text.split())
    maxw = (CAPTION_W - 22) * SS
    if d.textlength(text, font=font) > maxw:
        # 保留「結尾」最新的字（串流字幕才跟得上），前面以省略號帶過。
        while text and d.textlength("…" + text, font=font) > maxw:
            text = text[1:]
        text = "…" + text
    tw = d.textlength(text, font=font)
    d.text(((W - tw) / 2, H / 2 - 11 * SS), text, font=font,
           fill=(236, 240, 247, int(255 * alpha)))
    return img.resize((CAPTION_W, CAPTION_H), Image.LANCZOS)


class OrbHUD:
    def __init__(self, port: int = HUD_PORT) -> None:
        self.root = tk.Tk()
        self.root.title("FRIDAY")

        self.root.wm_attributes("-topmost", True)
        # === macOS Tk 9 透明浮層的關鍵 ===
        # overrideredirect(True) 會把 NSWindow 換成不支援透明合成的型態 → 透明
        # 區域被畫成「黑底」。所以這裡【不用】overrideredirect；改成保留一個正常
        # （可透明）的 NSWindow，再用「清空 stylemask」拿掉標題列/外框 → 既無框
        # 又能真正透出桌面、四周不再黑。
        try:
            self.root.wm_attributes("-transparent", True)
            self.bg = "systemTransparent"
        except tk.TclError:
            self.bg = "#05080d"  # 不支援透明時退回深色面板
        try:
            self.root.wm_attributes("-stylemask", "")  # 無標題列、無外框
        except tk.TclError:
            # 真的不支援 stylemask 時，退回 overrideredirect（會有黑底，但至少無框）
            self.root.overrideredirect(True)
        self.root.config(bg=self.bg)

        self.canvas = tk.Canvas(
            self.root, width=CANVAS, height=CANVAS,
            bg=self.bg, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        # 兩種繪製後端：Pillow（漂亮）/ 純 Tk（退回）
        if _HAS_PIL:
            self._init_pil()
        else:
            self._init_tk()

        # 預設擺右下角
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{CANVAS}x{CANVAS}+{sw - CANVAS - 40}+{sh - CANVAS - 120}")

        # 狀態
        self.state = DEFAULT_STATE
        self.intensity = 0.0   # 平滑後的目前脈動強度
        self.phase = 0.0       # 脈動相位
        self.spin = 0.0        # 反應爐光環角度
        self._t_prev = time.monotonic()

        # 互動：拖曳移動 / Esc 或右鍵關閉
        self._drag = (0, 0)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.root.bind("<Escape>", lambda e: self._quit())
        self.canvas.bind("<Button-2>", lambda e: self._quit())
        self.canvas.bind("<Button-3>", lambda e: self._quit())

        # UDP 接收 socket（非阻塞，於動畫迴圈裡 drain）
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("127.0.0.1", port))
        except OSError:
            # port 被佔用就改用任意 port，並把實際 port 寫到 stdout 讓父行程知道
            self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        print(f"[hud] listening udp 127.0.0.1:{self.sock.getsockname()[1]}", flush=True)

    # === Pillow 後端：委派給共用的 FrameRenderer，貼到 Tk canvas ==========
    def _init_pil(self) -> None:
        self._renderer = FrameRenderer()
        self._img_id = self.canvas.create_image(CENTER, CENTER)
        self._tk_img = None  # 保留參照避免被 GC
        self._draw = self._draw_pil

    def _draw_pil(self, dt: float) -> None:
        frame = self._renderer.render(self.state, dt)
        self._tk_img = ImageTk.PhotoImage(frame)
        self.canvas.itemconfig(self._img_id, image=self._tk_img)

    # === 純 Tk 後端（無 Pillow 時的退回畫法） ==============================
    def _init_tk(self) -> None:
        self._gsteps = 26
        self._rings = [
            self.canvas.create_oval(0, 0, 0, 0, width=0, fill=self.bg)
            for _ in range(self._gsteps)
        ]
        self._core = self.canvas.create_oval(0, 0, 0, 0, width=0, fill=self.bg)
        self._draw = self._draw_tk

    def _draw_tk(self, dt: float) -> None:
        rgb, target_amp, freq, _spin = STATES[self.state]
        self.intensity += (target_amp - self.intensity) * min(1.0, dt * 6.0)
        self.phase += dt * freq * 2 * math.pi
        pulse = 0.5 + 0.5 * math.sin(self.phase)
        amp = self.intensity * pulse
        base_r = 34
        r = base_r * (1.0 + 0.28 * amp)
        glow_r = r + 38 * (0.4 + 0.6 * self.intensity)

        cr, cg, cb = rgb
        for i, ring in enumerate(self._rings):
            f = i / (self._gsteps - 1)
            rr = glow_r - (glow_r - r) * f
            k = 0.06 + 0.94 * (f ** 2)
            col = _hex((int(cr * k), int(cg * k), int(cb * k)))
            self.canvas.coords(ring, CENTER - rr, CENTER - rr, CENTER + rr, CENTER + rr)
            self.canvas.itemconfig(ring, fill=col)

        hi = _hex((min(255, cr + 80), min(255, cg + 80), min(255, cb + 80)))
        self.canvas.coords(self._core, CENTER - r * 0.5, CENTER - r * 0.5,
                           CENTER + r * 0.5, CENTER + r * 0.5)
        self.canvas.itemconfig(self._core, fill=hi)

    # --- 互動 -------------------------------------------------------------
    def _on_press(self, e: "tk.Event") -> None:
        self._drag = (e.x, e.y)

    def _on_drag(self, e: "tk.Event") -> None:
        x = self.root.winfo_x() + (e.x - self._drag[0])
        y = self.root.winfo_y() + (e.y - self._drag[1])
        self.root.geometry(f"+{x}+{y}")

    def _quit(self) -> None:
        try:
            self.sock.close()
        finally:
            self.root.destroy()

    # --- 網路 -------------------------------------------------------------
    def _drain_udp(self) -> None:
        latest = None
        while True:
            try:
                data, _ = self.sock.recvfrom(64)
            except (BlockingIOError, OSError):
                break
            latest = data.decode("ascii", "ignore").strip().lower()
        if latest in STATES:
            self.state = latest

    # --- 主迴圈 -----------------------------------------------------------
    def _tick(self) -> None:
        now = time.monotonic()
        dt = now - self._t_prev
        self._t_prev = now
        self._drain_udp()
        self._draw(dt)
        self.root.after(int(1000 / FPS), self._tick)

    def run(self) -> None:
        self._tick()
        self.root.mainloop()


def _make_udp_socket(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    print(f"[hud] listening udp 127.0.0.1:{sock.getsockname()[1]}", flush=True)
    return sock


def _load_pos() -> "tuple[float, float] | None":
    try:
        with open(_POS_FILE) as fh:
            x, y = fh.read().split(",")
            return float(x), float(y)
    except Exception:
        return None


def _save_pos(x: float, y: float) -> None:
    try:
        with open(_POS_FILE, "w") as fh:
            fh.write(f"{x:.0f},{y:.0f}")
    except Exception:
        pass


if _HAS_COCOA and _HAS_PIL:

    class _OrbWindow(NSWindow):  # 無邊框視窗預設不能成為 key window，覆寫之。
        def canBecomeKeyWindow(self) -> bool:
            return True

    class _OrbView(NSImageView):
        """承載光球影像，並處理：拖曳移動、單擊切靜音、Esc 關閉、右鍵選單。"""

        def acceptsFirstMouse_(self, event) -> bool:
            return True

        def acceptsFirstResponder(self) -> bool:
            return True

        # --- 滑鼠：手動拖曳，並用「沒移動 = 單擊」來觸發靜音切換 ---
        def mouseDown_(self, event) -> None:
            self._down = NSEvent.mouseLocation()
            self._origin = self.window().frame().origin
            self._moved = False

        def mouseDragged_(self, event) -> None:
            now = NSEvent.mouseLocation()
            dx = now.x - self._down.x
            dy = now.y - self._down.y
            if abs(dx) > 2 or abs(dy) > 2:
                self._moved = True
            self.window().setFrameOrigin_((self._origin.x + dx, self._origin.y + dy))

        def mouseUp_(self, event) -> None:
            if getattr(self, "_moved", False):
                cb = getattr(self, "_on_moved", None)
                if cb:
                    cb()
            else:
                cb = getattr(self, "_on_click", None)
                if cb:
                    cb()

        def keyDown_(self, event) -> None:
            if event.keyCode() == 53:  # Esc → 關閉
                cb = getattr(self, "_on_quit", None)
                if cb:
                    cb()

        def rightMouseDown_(self, event) -> None:  # 右鍵 → 選單
            cb = getattr(self, "_on_menu", None)
            if cb:
                cb(event)

    class _TimerTarget(NSObject):
        def initWithCallback_(self, cb):
            self = objc.super(_TimerTarget, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        def tick_(self, timer) -> None:
            self._cb()

    class _MenuTarget(NSObject):
        def initWithController_(self, ctrl):
            self = objc.super(_MenuTarget, self).init()
            if self is None:
                return None
            self._ctrl = ctrl
            return self

        def menuAction_(self, sender) -> None:
            self._ctrl._on_menu_action(sender.representedObject())

    def _pil_to_nsimage(pil_img: "Image.Image") -> "NSImage":
        """PIL RGBA → NSImage。優先用 NSBitmapImageRep 直接吃 raw bytes（免 PNG
        編解碼，30fps 省 CPU）；任何異常退回 PNG 路徑確保不會壞畫面。"""
        try:
            if pil_img.mode != "RGBA":
                pil_img = pil_img.convert("RGBA")
            w, h = pil_img.size
            raw = pil_img.tobytes("raw", "RGBA")
            rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bitmapFormat_bytesPerRow_bitsPerPixel_(
                None, w, h, 8, 4, True, False, NSDeviceRGBColorSpace,
                NSBitmapFormatAlphaNonpremultiplied, w * 4, 32,
            )
            rep.bitmapData()[:] = raw  # 直接寫入像素緩衝
            img = NSImage.alloc().initWithSize_(NSMakeSize(w, h))
            img.addRepresentation_(rep)
            return img
        except Exception:
            buf = io.BytesIO()
            pil_img.save(buf, "PNG")
            data = NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))
            return NSImage.alloc().initWithData_(data)

    def _borderless_window(x, y, w, h):
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False,
        )
        win.setOpaque_(False)
        win.setBackgroundColor_(NSColor.clearColor())
        win.setLevel_(NSStatusWindowLevel)
        win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
        )
        return win

    class OrbHUDMac:
        """原生 macOS 透明浮層：真透明、無框、無黑底，可拖曳、點擊靜音、右鍵選單、
        記住位置、字幕泡泡、全域熱鍵顯示/隱藏。"""

        def __init__(self, port: int = HUD_PORT) -> None:
            self.renderer = FrameRenderer()
            self.state = DEFAULT_STATE
            self.muted = False
            self.hidden = False
            self.size = CANVAS
            self.cur_amp = 0.0
            self._amp_ts = 0.0
            self._cap_text = ""
            self._cap_ts = 0.0
            self._t_prev = time.monotonic()
            self.sock = _make_udp_socket(port)
            # 回送給 agent 的 socket（靜音切換通知）。
            self.ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.ctrl_addr = ("127.0.0.1", HUD_CTRL_PORT)

            self.app = NSApplication.sharedApplication()
            self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

            scr = NSScreen.mainScreen().frame()
            pos = _load_pos()
            if pos is None:
                pos = (scr.size.width - CANVAS - 40, 120)
            win = _OrbWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(pos[0], pos[1], CANVAS, CANVAS),
                NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False,
            )
            win.setOpaque_(False)
            win.setBackgroundColor_(NSColor.clearColor())   # ← 真正透明的關鍵
            win.setHasShadow_(True)                         # 貼桌軟陰影（懸浮感）
            win.setLevel_(NSStatusWindowLevel)
            win.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
            )

            view = _OrbView.alloc().initWithFrame_(NSMakeRect(0, 0, CANVAS, CANVAS))
            view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
            view._on_quit = self._quit
            view._on_click = self._toggle_mute
            view._on_moved = self._on_moved
            view._on_menu = self._show_menu
            win.setContentView_(view)
            win.makeKeyAndOrderFront_(None)
            win.makeFirstResponder_(view)
            self.app.activateIgnoringOtherApps_(True)
            self.win, self.view = win, view

            # 字幕泡泡視窗（延後到第一次有字幕才真的顯示）。
            self.cap_win = _borderless_window(pos[0], pos[1] - CAPTION_H - 6,
                                              CAPTION_W, CAPTION_H)
            cap_view = NSImageView.alloc().initWithFrame_(
                NSMakeRect(0, 0, CAPTION_W, CAPTION_H))
            cap_view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
            self.cap_win.setContentView_(cap_view)
            self.cap_view = cap_view
            self._cap_visible = False
            self._cap_cache_key = None   # (文字, alpha) 沒變就不重畫字幕影像

            # 右鍵選單
            self._menu_target = _MenuTarget.alloc().initWithController_(self)
            self.menu = self._build_menu()

            # 全域熱鍵 Cmd+Shift+F → 顯示/隱藏
            self._install_hotkey()

            self._target = _TimerTarget.alloc().initWithCallback_(self._tick)
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / FPS, self._target, b"tick:", None, True,
            )

        # --- 選單 ---------------------------------------------------------
        def _build_menu(self):
            menu = NSMenu.alloc().init()
            for title, key in (("靜音麥克風", "mute"), ("－", None),
                               ("小", "size56"), ("中", "size72"), ("大", "size96"),
                               ("－", None), ("重設位置", "reset"), ("結束", "quit")):
                if key is None:
                    menu.addItem_(NSMenuItem.separatorItem())
                    continue
                it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    title, b"menuAction:", "")
                it.setTarget_(self._menu_target)
                it.setRepresentedObject_(key)
                menu.addItem_(it)
            return menu

        def _show_menu(self, event) -> None:
            self.menu.itemAtIndex_(0).setTitle_("取消靜音" if self.muted else "靜音麥克風")
            NSMenu.popUpContextMenu_withEvent_forView_(self.menu, event, self.view)

        def _on_menu_action(self, key) -> None:
            if key == "mute":
                self._toggle_mute()
            elif key == "reset":
                scr = NSScreen.mainScreen().frame()
                self.win.setFrameOrigin_((scr.size.width - self.size - 40, 120))
                self._on_moved()
            elif key == "quit":
                self._quit()
            elif key and key.startswith("size"):
                self._set_size(int(key[4:]))

        def _set_size(self, px: int) -> None:
            f = self.win.frame()
            self.win.setContentSize_(NSMakeSize(px, px))
            self.view.setFrame_(NSMakeRect(0, 0, px, px))
            self.win.setFrameOrigin_((f.origin.x, f.origin.y))
            self.size = px

        # --- 靜音 ---------------------------------------------------------
        def _toggle_mute(self) -> None:
            self.muted = not self.muted
            try:  # 通知 agent 端切換麥克風
                self.ctrl_sock.sendto(
                    f"mute:{1 if self.muted else 0}".encode(), self.ctrl_addr)
            except OSError:
                pass

        # --- 位置記憶 ------------------------------------------------------
        def _on_moved(self) -> None:
            o = self.win.frame().origin
            _save_pos(o.x, o.y)

        # --- 全域熱鍵 ------------------------------------------------------
        def _install_hotkey(self):
            def handler(event):
                mods = event.modifierFlags()
                if (event.keyCode() == 3                       # 'F'
                        and mods & NSEventModifierFlagCommand
                        and mods & NSEventModifierFlagShift):
                    self._toggle_visible()
                return event
            try:
                NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                    NSEventMaskKeyDown, lambda e: handler(e))
                NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                    NSEventMaskKeyDown, handler)
            except Exception:
                pass

        def _toggle_visible(self) -> None:
            self.hidden = not self.hidden
            if self.hidden:
                self.win.orderOut_(None)
                self.cap_win.orderOut_(None)
                self._cap_visible = False
            else:
                self.win.orderFrontRegardless()

        # --- 字幕 ---------------------------------------------------------
        def _set_caption(self, text: str) -> None:
            self._cap_text = text
            self._cap_ts = time.monotonic()

        def _update_caption(self, now: float) -> None:
            if self.hidden or not self._cap_text:
                if self._cap_visible:
                    self.cap_win.orderOut_(None)
                    self._cap_visible = False
                return
            age = now - self._cap_ts
            if age > CAPTION_TTL:
                self._cap_text = ""
                return
            alpha = 1.0 if age < CAPTION_TTL - 1.0 else max(0.0, CAPTION_TTL - age)
            # 只在文字 / 淡出透明度改變時才重畫字幕影像（省 CPU、不卡）。
            key = (self._cap_text, round(alpha, 2))
            if key != self._cap_cache_key:
                self._cap_cache_key = key
                self.cap_view.setImage_(_pil_to_nsimage(render_caption(self._cap_text, alpha)))
            f = self.win.frame()
            cx = f.origin.x + (self.size - CAPTION_W) / 2.0
            cy = f.origin.y - CAPTION_H - 6
            self.cap_win.setFrameOrigin_((cx, cy))
            if not self._cap_visible:
                self.cap_win.orderFrontRegardless()
                self._cap_visible = True

        # --- 網路：解析 state / amp / caption / tool ----------------------
        def _drain_udp(self) -> None:
            while True:
                try:
                    data, _ = self.sock.recvfrom(512)
                except (BlockingIOError, OSError):
                    break
                msg = data.decode("utf-8", "ignore").strip()
                if not msg:
                    continue
                if ":" in msg:
                    key, _, val = msg.partition(":")
                    key, val = key.strip().lower(), val.strip()
                    if key == "state" and val.lower() in STATES:
                        self.state = val.lower()
                    elif key == "amp":
                        try:
                            self.cur_amp = max(self.cur_amp, min(1.0, float(val)))
                            self._amp_ts = time.monotonic()
                        except ValueError:
                            pass
                    elif key == "caption":
                        self._set_caption(val)
                    elif key == "tool":
                        self._set_caption(("⚙ " + val) if val else "")
                elif msg.lower() in STATES:
                    self.state = msg.lower()

        # --- 主迴圈 -------------------------------------------------------
        def _tick(self) -> None:
            now = time.monotonic()
            dt = now - self._t_prev
            self._t_prev = now
            self._drain_udp()
            if now - self._amp_ts > 0.15:   # 沒有新的音量封包就自然衰減
                self.cur_amp *= 0.82
            if not self.hidden:
                frame = self.renderer.render(
                    self.state, dt, ext_amp=self.cur_amp, mute=self.muted)
                self.view.setImage_(_pil_to_nsimage(frame))
            self._update_caption(now)

        def _quit(self) -> None:
            self._on_moved()
            try:  # 通知 agent 一起結束（連終端機行程一併收掉）
                self.ctrl_sock.sendto(b"quit:1", self.ctrl_addr)
            except OSError:
                pass
            try:
                self.sock.close()
            finally:
                self.app.terminate_(None)

        def run(self) -> None:
            self.app.run()


def main() -> None:
    # macOS：用原生透明視窗（Tk 9 透明在某些版本壞掉、四周會黑）。
    if _HAS_COCOA and _HAS_PIL and os.environ.get("FRIDAY_HUD_FORCE_TK") != "1":
        OrbHUDMac().run()
    else:
        OrbHUD().run()


if __name__ == "__main__":
    main()
