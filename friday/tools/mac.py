"""
Mac control tools — let FRIDAY actually *do things* on the host Mac:
open apps/websites, adjust volume, run AppleScript, and execute shell commands.

⚠️  These tools run on whatever machine the MCP server (`uv run friday`) is on.
    Shell execution is powerful — a light guard blocks obviously catastrophic
    commands, but treat this like giving the assistant a terminal.
"""

import re
import shlex
import subprocess
from pathlib import Path

# 粗略攔截「一講出來就會出大事」的指令，主要防語音辨識誤判。
# 這不是完整的資安防護，只是基本保險。
_DANGEROUS = re.compile(
    r"""(\brm\s+-[rf]*\s+/(?:\s|$)        # rm -rf /
        |\bmkfs\b                          # 格式化
        |\bdd\b.*\bof=/dev/                # 直接寫磁碟
        |>\s*/dev/(?:sd|disk|rdisk)        # 覆寫磁碟裝置
        |:\(\)\s*\{.*\};:                  # fork bomb
        |\bshutdown\b|\breboot\b|\bhalt\b  # 關機重開
        |\bdiskutil\s+(erase|reformat)     # 抹除磁碟
        )""",
    re.VERBOSE | re.IGNORECASE,
)


def _run(cmd, timeout=30):
    """Run a command list, return trimmed combined output."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(Path.home()),
        )
        out = (p.stdout or "") + (p.stderr or "")
        out = out.strip() or f"(done, exit code {p.returncode})"
        return out[:2000]
    except subprocess.TimeoutExpired:
        return "指令逾時了（超過時間限制），已中止。"
    except Exception as e:  # noqa: BLE001
        return f"執行失敗：{e}"


def register(mcp):

    @mcp.tool()
    def open_app(name: str) -> str:
        """Open a macOS application by name, e.g. "Spotify", "Safari", "Calculator", "Visual Studio Code"."""
        if not name.strip():
            return "沒有指定要開哪個 App。"
        out = _run(["open", "-a", name.strip()])
        if "Unable to find application" in out or "无法找到" in out:
            return f"找不到名為「{name}」的 App。"
        return f"已開啟 {name}。"

    @mcp.tool()
    def open_website(url: str) -> str:
        """Open a URL in the default browser. Accepts bare domains too (adds https://)."""
        u = url.strip()
        if not u:
            return "沒有指定網址。"
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
            u = "https://" + u
        _run(["open", u])
        return f"已在瀏覽器開啟 {u}。"

    @mcp.tool()
    def set_volume(level: int) -> str:
        """Set the Mac output volume. level is 0-100."""
        level = max(0, min(100, int(level)))
        _run(["osascript", "-e", f"set volume output volume {level}"])
        return f"音量已設為 {level}%。"

    @mcp.tool()
    def run_applescript(script: str) -> str:
        """Run an arbitrary AppleScript snippet and return its output. Use for advanced Mac automation
        (controlling apps, windows, Music/Spotify playback, notifications, etc.)."""
        if not script.strip():
            return "沒有提供 AppleScript 內容。"
        return _run(["osascript", "-e", script])

    @mcp.tool()
    def run_python(code: str) -> str:
        """Execute a snippet of Python 3 code and return whatever it prints (stdout+stderr).
        Use this as a general-purpose brain extension — do math, parse/convert data, call web APIs
        with urllib/requests, manipulate files, generate things, etc. The code runs in a fresh
        Python process with a 30s timeout. Remember to print() the result you want back."""
        if not code.strip():
            return "沒有提供程式碼。"
        return _run(["/usr/bin/env", "python3", "-c", code])

    @mcp.tool()
    def run_shell_command(command: str) -> str:
        """Execute a shell command on the host Mac and return its output (stdout+stderr, truncated).
        Use this to actually get things done for the user — listing/creating files, running scripts,
        git operations, brew installs, etc. Runs in the user's home directory with a 30s timeout.
        Obviously destructive commands (rm -rf /, disk formatting, shutdown, fork bombs) are refused."""
        cmd = command.strip()
        if not cmd:
            return "沒有提供指令。"
        if _DANGEROUS.search(cmd):
            return "這個指令看起來有破壞性（可能刪資料/格式化/關機），為了安全我拒絕執行。請換個說法或自己手動操作。"
        return _run(["/bin/zsh", "-lc", cmd])
