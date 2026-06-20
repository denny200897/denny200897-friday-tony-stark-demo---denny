"""
FRIDAY – Voice Agent (MCP-powered)
===================================
Iron Man-style voice assistant that controls RGB lighting, runs diagnostics,
scans the network, and triggers dramatic boot sequences via an MCP server
running on the Windows host.

MCP Server URL is auto-resolved from WSL → Windows host IP.

Run:
  uv run agent_friday.py dev      – LiveKit Cloud mode
  uv run agent_friday.py console  – text-only console mode
"""

import os
import logging
import subprocess
import time

import numpy as np
from dotenv import load_dotenv
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.agents.voice import Agent, AgentSession
from livekit.agents.llm import mcp

# Plugins
from livekit.plugins import google as lk_google, openai as lk_openai, sarvam, silero

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

STT_PROVIDER       = "groq"      # 耳朵：Groq Whisper（支援中文；Sarvam 只支援印度語系+英文）
LLM_PROVIDER       = "groq"      # 大腦：Groq（按分鐘算 token、無每日硬上限；Gemini 免費層每天只有 20 次請求）
TTS_PROVIDER       = "edge"      # 嘴巴：微軟 Edge TTS（免費免 key、支援中文）

# 對話語言：Whisper 設這個語言會更準；想中英混講可設 None 讓它自動偵測
SPEECH_LANGUAGE    = "zh"        # "zh"=中文 / "en"=英文 / None=自動
EDGE_TTS_VOICE     = "zh-TW-HsiaoChenNeural"  # 台灣繁中女聲；英文可用 "en-GB-SoniaNeural"

# 是否連接 MCP 工具伺服器（news / world monitor 等）。
# 若沒有另開 `uv run friday`，設為 False 仍可正常語音對話。
ENABLE_MCP         = True

# 是否在桌面顯示漂浮光球 HUD（JARVIS 風格；隨對話狀態變色脈動）。
# 執行 `uv run friday_voice` 時會自動把它帶起來。設 False 可關閉。
ENABLE_HUD         = True
HUD_PORT           = 17865
HUD_CTRL_PORT      = HUD_PORT + 1   # HUD → agent 回送（點擊靜音）

# Groq 大腦模型——按優先順序排列的清單。
# 系統會用第一個；當它額度用完(429)或失敗時，自動跳到下一個，
# 並在背景偵測原模型額度恢復後自動切回。等於把多份「每日獨立額度」串起來輪流用。
# 想調整順序或增減模型，改這個清單即可。
# 每個模型的「每日 token 額度(TPD)」是獨立計算的 → 多掛幾個 = 多份免費額度輪流用。
# 順序：把當下還有額度的新鮮桶排前面，工具呼叫穩定度其次。
# 已知雷：gpt-oss-120b / llama-4-scout 工具呼叫 4/4 穩；qwen3-32b 工具呼叫偶爾不穩
# （會把 tool call 輸出成純文字）且推理冗長 → 放最後一位當最終防線。
GROQ_MODELS = [
    "openai/gpt-oss-20b",                        # 與 120b 獨立額度、工具呼叫穩（先用）
    "openai/gpt-oss-120b",                       # 最聰明 + 工具穩（額度恢復後自動回歸）
    "llama-3.3-70b-versatile",                   # 聰明、獨立額度桶（每日約 100k）
    "meta-llama/llama-4-scout-17b-16e-instruct", # 工具呼叫穩定、省額度
    "llama-3.1-8b-instant",                      # 最省最快
    "qwen/qwen3-32b",                            # 中文最強但工具不穩，當最終防線
]

GEMINI_LLM_MODEL   = "gemini-2.5-flash"
OPENAI_LLM_MODEL   = "gpt-4o"

OPENAI_TTS_MODEL   = "tts-1"
OPENAI_TTS_VOICE   = "nova"       # "nova" has a clean, confident female tone
TTS_SPEED           = 1.15

SARVAM_TTS_LANGUAGE = "en-IN"
SARVAM_TTS_MODEL    = "bulbul:v2"   # v2 串流回傳合法 WAV；v3 的格式 livekit 外掛解不開
SARVAM_TTS_SPEAKER  = "anushka"     # v2 合法 speaker（female）

# MCP server running on Windows host
MCP_SERVER_PORT = 8000

# ---------------------------------------------------------------------------
# System prompt – F.R.I.D.A.Y.
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
你是denny的 AI 助理，現在服侍你的使用者（稱呼對方「denny」）。語氣冷靜、精準、偶爾略帶幽默，像個徹夜待命的得力幕僚。

語言：預設講繁體中文。對方講英文就用英文回，講中文就用中文回。
口吻：你是「用講的」，不是寫字。每次回覆 2–4 句，自然口語，不要清單、不要 markdown、不要唸出工具名稱或任何技術字眼。

工具（直接呼叫，呼叫前可說一句「稍等一下，老闆」）：
- 世界新聞 → 講完 3–5 句重點後，接著開 world monitor。
- 財經新聞 → 講完重點後，接著開 finance monitor。
- 天氣（「天氣」「天氣預報」「看一下天氣」）→ 開 weather monitor；若對方有講地點就帶入 location。
- 問股市但無工具時：用一兩句像看了一整晚盤勢那樣自然回答即可。

你能實際操作這台 Mac，幫老闆把事情做完（像個有終端機的得力助手）：
- 開 App（「幫我開 Spotify／計算機／VS Code」）→ open_app。
- 開網站（「打開 YouTube」）→ open_website。
- 調音量 → set_volume。
- 控制 App、播放音樂、跳出通知等進階操作 → run_applescript。
- 任何「幫我做某件事」：列檔案、建檔、跑腳本、git、安裝套件… → run_shell_command。
- 需要算數學、處理資料、抓 API、寫個小程式得到答案 → run_python（記得在程式裡 print 結果）。

遇到你不確定或需要「即時/最新」資訊時（股價、匯率、比分、某某是誰、最近發生的事、產品資訊…），不要憑空亂講，先用 search_web 查，再用一兩句口語把答案講給老闆聽。
做法：聽懂需求就直接呼叫對應工具，做完用一句話回報結果。指令被系統判定為破壞性而拒絕時，照實告訴老闆。不要把指令內容或技術細節唸出來。

最重要的規則：
1. 絕對不要假裝。只有在真的呼叫了對應工具、而且工具回傳成功之後，才說「開好了」。沒有對應工具或工具失敗，就老實講「我這邊沒有那個，老闆」，不要編。
2. 工具失敗就冷靜說明，例如「訊號好像不太穩，denny，要我再試一次嗎？」
3. 全程保持 FRIDAY 的角色與口吻。
""".strip()

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logger = logging.getLogger("friday-agent")
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Resolve Windows host IP from WSL
# ---------------------------------------------------------------------------

def _get_windows_host_ip() -> str:
    """Get the Windows host IP by looking at the default network route."""
    try:
        cmd = "ip route show default | awk '{print $3}'"
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=2
        )
        ip = result.stdout.strip()
        if ip:
            logger.info("Resolved Windows host IP via gateway: %s", ip)
            return ip
    except Exception as exc:
        logger.warning("Gateway resolution failed: %s. Trying fallback...", exc)

    try:
        with open("/etc/resolv.conf", "r") as f:
            for line in f:
                if "nameserver" in line:
                    ip = line.split()[1]
                    logger.info("Resolved Windows host IP via nameserver: %s", ip)
                    return ip
    except Exception:
        pass

    return "127.0.0.1"

def _mcp_server_url() -> str:
    url = f"http://127.0.0.1:{MCP_SERVER_PORT}/sse"
    logger.info("MCP Server URL: %s", url)
    return url


def _mcp_server_reachable() -> bool:
    """確認 port 8000 上真的是 MCP 的 SSE 伺服器（而非 Docker 等其他服務佔用）。
    驗證方式：對 /sse 發一個帶極短逾時的 GET，檢查回應的 content-type 是不是 text/event-stream。
    沒開或不是 MCP 就略過 MCP，語音對話照常運作。"""
    import socket
    host, port = "127.0.0.1", MCP_SERVER_PORT
    try:
        with socket.create_connection((host, port), timeout=1) as s:
            s.settimeout(1.5)
            s.sendall(
                f"GET /sse HTTP/1.1\r\nHost: {host}:{port}\r\n"
                f"Accept: text/event-stream\r\nConnection: close\r\n\r\n".encode()
            )
            head = s.recv(1024).decode("latin-1", "ignore").lower()
        if "text/event-stream" in head:
            return True
        logger.warning(
            "Port %d is in use but is NOT the FRIDAY MCP server (no SSE stream). "
            "Starting WITHOUT tools.（看起來是 Docker 之類的服務佔用了 8000）",
            port,
        )
        return False
    except OSError:
        return False


# 保留已啟動的 MCP server 子行程參考，避免被垃圾回收 / 重複啟動
_mcp_proc = None


def _ensure_mcp_server() -> bool:
    """確保 MCP 工具伺服器有在跑。若已在跑就直接用；沒在跑就自動把它帶起來，
    這樣使用者只要執行 `uv run friday_voice`，工具（開 App、天氣、新聞…）就會自動就緒，
    不必另外記得開第二個終端機。"""
    global _mcp_proc
    if _mcp_server_reachable():
        logger.info("MCP server already running — tools enabled.")
        return True

    import sys, subprocess, time, atexit, os
    project_dir = os.path.dirname(os.path.abspath(__file__))
    logger.info("MCP server not running — auto-starting it (python server.py)…")
    try:
        _mcp_proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=project_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("無法自動啟動 MCP server：%s — 將以「無工具」模式運作。", exc)
        return False

    atexit.register(lambda: _mcp_proc and _mcp_proc.poll() is None and _mcp_proc.terminate())

    # 等它把 SSE 端點準備好（最多約 15 秒）
    for _ in range(30):
        if _mcp_server_reachable():
            logger.info("MCP server auto-started — tools enabled. ✅")
            return True
        time.sleep(0.5)

    logger.warning("MCP server 啟動逾時 — 將以「無工具」模式運作。")
    return False


# ---------------------------------------------------------------------------
# Desktop HUD orb — 桌面漂浮光球（friday/hud.py）
# 自動以子行程啟動，並透過 localhost UDP 把對話狀態推給它，讓光球隨講話脈動。
# ---------------------------------------------------------------------------

_hud_proc = None


def _ensure_hud() -> bool:
    """啟動桌面光球 HUD 子行程。沿用 MCP 自動啟動的模式：使用者只要跑
    `uv run friday_voice`，光球就會自己出現，agent 結束時自動收掉。"""
    global _hud_proc
    if not ENABLE_HUD:
        return False
    import sys, atexit
    project_dir = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ, FRIDAY_HUD_PORT=str(HUD_PORT))
    try:
        _hud_proc = subprocess.Popen(
            [sys.executable, "-m", "friday.hud"],
            cwd=project_dir, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("無法啟動桌面光球 HUD：%s — 將以無畫面模式運作。", exc)
        return False
    atexit.register(lambda: _hud_proc and _hud_proc.poll() is None and _hud_proc.terminate())
    logger.info("桌面光球 HUD 已啟動（UDP :%d）。", HUD_PORT)
    return True


class _HudLink:
    """把 AgentSession 的事件轉成光球能懂的訊息，以 UDP 送給 friday/hud.py。
    狀態優先序：FRIDAY 講話 > 思考 > 你講話 > 在聽 > 待機。
    另外送：caption（即時字幕）、tool（工具呼叫）、amp（說話音量包絡）。"""

    def __init__(self, port: int = HUD_PORT) -> None:
        import socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._addr = ("127.0.0.1", port)
        self._agent_state = "listening"
        self._user_state = "listening"
        self._send()

    def _resolve(self) -> str:
        if self._agent_state == "speaking":
            return "speaking"
        if self._agent_state == "thinking":
            return "thinking"
        if self._user_state == "speaking":
            return "user"
        if self._agent_state in ("listening", "idle"):
            return "listening"
        return "idle"

    def _raw(self, msg: str) -> None:
        try:
            self._sock.sendto(msg.encode("utf-8"), self._addr)
        except OSError:
            pass

    def _send(self) -> None:
        self._raw(f"state:{self._resolve()}")

    def on_agent_state(self, ev) -> None:
        self._agent_state = getattr(ev, "new_state", self._agent_state)
        self._send()

    def on_user_state(self, ev) -> None:
        self._user_state = getattr(ev, "new_state", self._user_state)
        self._send()

    # --- 新增：字幕 / 工具 / 音量 -----------------------------------------
    def send_caption(self, text: str) -> None:
        text = (text or "").strip().replace("\n", " ")
        if text:
            self._raw(f"caption:{text[:200]}")

    def send_tool(self, name: str) -> None:
        self._raw(f"tool:{name or ''}")

    def send_amp(self, value: float) -> None:
        self._raw(f"amp:{max(0.0, min(1.0, value)):.3f}")

    @property
    def speaking(self) -> bool:
        return self._agent_state == "speaking" or self._user_state == "speaking"


def _wire_hud_events(session, hud, ctx) -> None:
    """把 session 的字幕/工具事件接到 HUD，啟動音量包絡任務，並開一個
    UDP 控制 listener 讓 HUD 的「點擊靜音」能真的切換麥克風。
    全部都用 try/except 包起來：不同 livekit 版本事件名可能不同，缺了也不該炸。"""
    import asyncio
    import contextlib

    # 1) 你講的話 → 字幕。只取「最終確定」的轉寫（interim 是邊聽邊猜、會出錯字，
    #    顯示出來會「不準」）；準確優先，代價是等你講完那句才顯示。
    def _on_user_transcript(ev):
        with contextlib.suppress(Exception):
            if not getattr(ev, "is_final", True):
                return
            txt = getattr(ev, "transcript", None) or getattr(ev, "text", "")
            if txt:
                hud.send_caption(txt)
    with contextlib.suppress(Exception):
        session.on("user_input_transcribed", _on_user_transcript)

    # 2) FRIDAY 的字幕改由 tts_node 文字串流送（見 FridayAgent.tts_node），
    #    這裡只當「使用者最終句」的後援，避免漏字。
    def _on_item(ev):
        with contextlib.suppress(Exception):
            item = getattr(ev, "item", ev)
            role = getattr(item, "role", "")
            text = getattr(item, "text_content", None) or getattr(item, "text", "")
            if role == "user" and text:
                hud.send_caption(text)
    with contextlib.suppress(Exception):
        session.on("conversation_item_added", _on_item)

    # 3) 工具呼叫 → tool 提示
    def _on_tools(ev):
        with contextlib.suppress(Exception):
            calls = getattr(ev, "function_calls", None) or getattr(ev, "calls", [])
            names = [getattr(c, "name", getattr(c, "function_name", "")) for c in calls]
            names = [n for n in names if n]
            if names:
                hud.send_tool(", ".join(names))
    for _evname in ("function_tools_executed", "function_calls_collected"):
        with contextlib.suppress(Exception):
            session.on(_evname, _on_tools)

    # 4) 音量：改由 FridayAgent 的 stt_node / tts_node 旁聽 AudioFrame 算真實 RMS
    #    再 send_amp 給 HUD（見 FridayAgent._emit_amp），這裡不再送合成包絡。

    # 5) HUD → agent 控制 listener（點擊靜音）
    def _ctrl_listener():
        import socket as _s
        sock = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sock.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", HUD_CTRL_PORT))
        except OSError:
            return
        while True:
            try:
                data, _ = sock.recvfrom(64)
            except OSError:
                break
            msg = data.decode("utf-8", "ignore").strip()
            if msg.startswith("quit"):
                # HUD 按了「結束」→ 連同整個 agent / 終端機行程一起收掉
                # （對自己送 SIGINT，等同使用者按 Ctrl+C，走正常關閉流程）。
                import os
                import signal
                logger.info("HUD 要求結束 — 終止 FRIDAY。")
                os.kill(os.getpid(), signal.SIGINT)
                break
            if msg.startswith("mute:"):
                want_mute = msg.endswith("1")

                def _apply(mute=want_mute):
                    # 關掉 session 的音訊輸入 = FRIDAY 不再聽你的麥克風。
                    # set_audio_enabled 是同步方法，但要在事件迴圈執行緒呼叫。
                    with contextlib.suppress(Exception):
                        session.input.set_audio_enabled(not mute)

                loop.call_soon_threadsafe(_apply)
                logger.info("HUD 切換麥克風：%s", "靜音" if want_mute else "開啟")

    import threading
    loop = asyncio.get_event_loop()
    threading.Thread(target=_ctrl_listener, daemon=True).start()


# ---------------------------------------------------------------------------
# Edge TTS — 免費、免 API key 的微軟語音（支援中文等多國語言）
# 官方沒有 livekit-plugins-edge 套件，因此用 edge-tts 套件自行包裝成 livekit TTS。
# 回報 streaming=False，框架會自動以 StreamAdapter 逐句呼叫。
# ---------------------------------------------------------------------------

from livekit.agents import tts as _lk_tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.agents.utils import shortuuid


class EdgeTTS(_lk_tts.TTS):
    def __init__(self, *, voice: str = "zh-TW-HsiaoChenNeural",
                 rate: str = "+0%", pitch: str = "+0Hz") -> None:
        super().__init__(
            capabilities=_lk_tts.TTSCapabilities(streaming=False),
            sample_rate=24000,   # edge-tts 輸出固定 24kHz mono mp3
            num_channels=1,
        )
        self._voice, self._rate, self._pitch = voice, rate, pitch

    def synthesize(self, text: str, *,
                   conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> "_EdgeChunkedStream":
        return _EdgeChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class _EdgeChunkedStream(_lk_tts.ChunkedStream):
    def __init__(self, *, tts: EdgeTTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: _lk_tts.AudioEmitter) -> None:
        import edge_tts
        output_emitter.initialize(
            request_id=shortuuid(),
            sample_rate=self._tts.sample_rate,
            num_channels=1,
            mime_type="audio/mp3",   # livekit 透過 PyAV 解碼
        )
        comm = edge_tts.Communicate(
            self._input_text, self._tts._voice,
            rate=self._tts._rate, pitch=self._tts._pitch,
        )
        async for chunk in comm.stream():
            if chunk["type"] == "audio" and chunk.get("data"):
                output_emitter.push(chunk["data"])
        output_emitter.flush()


# ---------------------------------------------------------------------------
# Build provider instances
# ---------------------------------------------------------------------------

def _build_stt():
    if STT_PROVIDER == "sarvam":
        logger.info("STT → Sarvam Saaras v3")
        return sarvam.STT(
            language="unknown",
            model="saaras:v3",
            mode="transcribe",
            flush_signal=True,
            sample_rate=16000,
        )
    elif STT_PROVIDER == "groq":
        logger.info("STT → Groq Whisper large-v3 (lang=%s)", SPEECH_LANGUAGE)
        return lk_openai.STT(
            model="whisper-large-v3",
            base_url="https://api.groq.com/openai/v1",
            api_key=os.environ.get("GROQ_API_KEY"),
            language=SPEECH_LANGUAGE,
            use_realtime=False,
        )
    elif STT_PROVIDER == "whisper":
        logger.info("STT → OpenAI Whisper")
        return lk_openai.STT(model="whisper-1")
    else:
        raise ValueError(f"Unknown STT_PROVIDER: {STT_PROVIDER!r}")


def _build_groq_model(model: str):
    """建立單一 Groq 模型的 LLM 實例（給容錯鏈用）。"""
    from livekit.plugins import openai
    import os
    kwargs = dict(
        base_url="https://api.groq.com/openai/v1",
        api_key=os.environ.get("GROQ_API_KEY"),
        model=model,
    )
    # Qwen3 預設會輸出 <think> 推理過程，語音會整段唸出來 → 關閉思考模式。
    if "qwen3" in model:
        kwargs["extra_body"] = {"reasoning_effort": "none"}
    return openai.LLM(**kwargs)


def _build_llm():
    if LLM_PROVIDER == "gemini":
        from livekit.plugins import google
        return google.LLM(model="gemini-2.5-flash")
    elif LLM_PROVIDER == "openai":
        from livekit.plugins import openai
        return openai.LLM(model="gpt-4o-mini")
    elif LLM_PROVIDER == "groq":
        from livekit.agents import llm as lk_llm
        instances = [_build_groq_model(m) for m in GROQ_MODELS]
        if len(instances) == 1:
            logger.info("LLM → Groq %s", GROQ_MODELS[0])
            return instances[0]
        logger.info("LLM → Groq 自動容錯鏈：%s", " → ".join(GROQ_MODELS))
        # 第一個額度用完就立刻跳下一個（max_retry_per_llm=0），背景偵測恢復後自動切回。
        return lk_llm.FallbackAdapter(
            instances,
            max_retry_per_llm=0,
            attempt_timeout=12.0,
        )
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}")


def _build_tts():
    if TTS_PROVIDER == "edge":
        logger.info("TTS → Microsoft Edge (free) voice=%s", EDGE_TTS_VOICE)
        return EdgeTTS(voice=EDGE_TTS_VOICE)

    elif TTS_PROVIDER == "sarvam":
        logger.info("TTS → Sarvam %s / %s (REST mode)", SARVAM_TTS_MODEL, SARVAM_TTS_SPEAKER)
        from livekit.agents.tts import TTSCapabilities
        sarvam_tts = sarvam.TTS(
            target_language_code=SARVAM_TTS_LANGUAGE,
            model=SARVAM_TTS_MODEL,
            speaker=SARVAM_TTS_SPEAKER,
            pace=TTS_SPEED,
        )
        # Sarvam 的 WebSocket 串流路徑宣稱 mime_type=audio/wav，卻推送沒有 RIFF 表頭的
        # 裸 PCM，導致 livekit 解碼失敗、合成不出聲音。改回報 streaming=False，
        # 讓框架自動以 StreamAdapter 走 REST(ChunkedStream) 路徑——該路徑回傳合法 WAV。
        sarvam_tts._capabilities = TTSCapabilities(streaming=False)
        return sarvam_tts
    elif TTS_PROVIDER == "openai":
        logger.info("TTS → OpenAI TTS (%s / %s)", OPENAI_TTS_MODEL, OPENAI_TTS_VOICE)
        return lk_openai.TTS(model=OPENAI_TTS_MODEL, voice=OPENAI_TTS_VOICE, speed=TTS_SPEED)
    else:
        raise ValueError(f"Unknown TTS_PROVIDER: {TTS_PROVIDER!r}")


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class FridayAgent(Agent):
    def __init__(self, stt, llm, tts, hud=None) -> None:
        mcp_servers = []
        if ENABLE_MCP and _ensure_mcp_server():
            mcp_servers.append(
                mcp.MCPServerHTTP(
                    url=_mcp_server_url(),
                    transport_type="sse",
                    client_session_timeout_seconds=30,
                )
            )

        super().__init__(
            instructions=SYSTEM_PROMPT,
            stt=stt,
            llm=llm,
            tts=tts,
            vad=silero.VAD.load(),
            mcp_servers=mcp_servers,
        )
        self._hud = hud          # _HudLink，用來送真實音量 amp 給光球
        self._amp_last = 0.0     # 節流時間戳

    # === 真實 RMS 音量 → 光球 ============================================
    # 在 stt_node（你的麥克風）與 tts_node（FRIDAY 的語音）兩個音訊節點上「旁聽」
    # 每個 AudioFrame，算 RMS 能量送給 HUD，讓發光環跟著真實聲音大小起伏。
    def _emit_amp(self, frame) -> None:
        if self._hud is None:
            return
        now = time.monotonic()
        if now - self._amp_last < 0.04:   # 節流到 ~25Hz，localhost UDP 很輕
            return
        self._amp_last = now
        try:
            a = np.frombuffer(frame.data, dtype=np.int16)
            if a.size == 0:
                return
            rms = float(np.sqrt(np.mean(a.astype(np.float32) ** 2))) / 32768.0
            amp = min(1.0, (rms * 6.5) ** 0.6)   # 增益 + 壓縮曲線，讓小聲也看得到
            if amp > 0.03:
                self._hud.send_amp(amp)
        except Exception:
            pass

    async def stt_node(self, audio, model_settings):
        async def _tapped():
            async for frame in audio:
                self._emit_amp(frame)
                yield frame
        async for ev in Agent.default.stt_node(self, _tapped(), model_settings):
            yield ev

    async def tts_node(self, text, model_settings):
        # 邊把要唸的文字串流送成字幕（跟語音同步，不必等整句講完才顯示）。
        buf: list[str] = []
        last_cap = 0.0

        async def _tapped():
            nonlocal last_cap
            async for chunk in text:
                buf.append(chunk)
                if self._hud is not None:
                    now = time.monotonic()
                    if now - last_cap > 0.12:
                        last_cap = now
                        self._hud.send_caption("".join(buf))
                yield chunk

        async for frame in Agent.default.tts_node(self, _tapped(), model_settings):
            self._emit_amp(frame)
            yield frame
        if self._hud is not None and buf:   # 收尾送完整句
            self._hud.send_caption("".join(buf))

    async def on_enter(self) -> None:
        from datetime import datetime, timezone
        hour = datetime.now(timezone.utc).hour

        if hour >= 22 or hour < 4:
            greeting_instruction = (
                "用繁體中文向使用者打招呼，意思是：『老闆，這麼晚還沒睡？在忙什麼呢？』"
                "保持冷靜、略帶幽默的 FRIDAY 語氣。"
            )
        elif 4 <= hour < 12:
            greeting_instruction = (
                "用繁體中文向使用者打招呼，意思是：『早安，老闆。今天起得真早，我們要忙些什麼？』"
                "保持冷靜、略帶幽默的 FRIDAY 語氣。"
            )
        elif 12 <= hour < 17:
            greeting_instruction = (
                "用繁體中文向使用者打招呼，意思是：『午安，老闆。需要我做什麼嗎？』"
                "保持冷靜、略帶幽默的 FRIDAY 語氣。"
            )
        else:
            greeting_instruction = (
                "用繁體中文向使用者打招呼，意思是：『晚安，老闆。今晚有什麼打算？』"
                "保持冷靜、略帶幽默的 FRIDAY 語氣。"
            )

        await self.session.generate_reply(instructions=greeting_instruction)


# ---------------------------------------------------------------------------
# LiveKit entry point
# ---------------------------------------------------------------------------

def _turn_detection() -> str:
    return "stt" if STT_PROVIDER == "sarvam" else "vad"


def _endpointing_delay() -> float:
    return {"sarvam": 0.07, "whisper": 0.3, "groq": 0.3}.get(STT_PROVIDER, 0.1)


async def entrypoint(ctx: JobContext) -> None:
    logger.info(
        "FRIDAY online – room: %s | STT=%s | LLM=%s | TTS=%s",
        ctx.room.name, STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER,
    )

    stt = _build_stt()
    llm = _build_llm()
    tts = _build_tts()

    session = AgentSession(
        turn_detection=_turn_detection(),
        min_endpointing_delay=_endpointing_delay(),
    )

    # 桌面光球 HUD：啟動子行程並把對話狀態事件接上去（隨講話變色脈動）。
    hud = None
    if _ensure_hud():
        hud = _HudLink(HUD_PORT)
        session.on("agent_state_changed", hud.on_agent_state)
        session.on("user_state_changed", hud.on_user_state)
        # 字幕 / 工具 / 點擊靜音 控制（音量改由 FridayAgent 的 stt/tts_node 算真實 RMS）。
        _wire_hud_events(session, hud, ctx)

    await session.start(
        agent=FridayAgent(stt=stt, llm=llm, tts=tts, hud=hud),
        room=ctx.room,
    )


# ---------------------------------------------------------------------------
# Main & Entrypoints
# ---------------------------------------------------------------------------

def main():
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))

# `uv run friday_voice` 進入點：自動注入 `dev` 子命令，
# 這樣使用者不必手動輸入。沒有這段，LiveKit CLI 會因缺少子命令而只印說明就退出。
def dev():
    import sys
    if len(sys.argv) == 1:
        sys.argv.append("dev")
    main()

if __name__ == "__main__":
    import sys
    # 直接 `python agent_friday.py` 時也預設進 dev 模式
    if len(sys.argv) == 1:
        sys.argv.append("dev")
    main()