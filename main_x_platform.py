import os
import re
import uuid
import signal
import socket
import getpass
import asyncio
import platform
import subprocess
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
load_dotenv()
X_PLATFORM = os.getenv("X_PLATOFORM", "false").lower() == "true"

frontend = "index_x_platform.html" if X_PLATFORM else "index.html"

print(f"X_PLATFORM ENABLED: {X_PLATFORM}", frontend)
app = FastAPI(title="Smart Cross-Platform Terminal (Windows & macOS)")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


def format_os_prompt(cwd: str, venv_name: str = "") -> str:
    """Formats the prompt authentically for Windows CMD or macOS/Linux."""
    venv_prefix = f"({venv_name}) " if venv_name else ""
    if IS_WINDOWS:
        return f"{venv_prefix}{cwd}>"
    else:
        # Authentic macOS / Linux style prompt
        user = getpass.getuser()
        host = socket.gethostname().split(".")[0]
        home = os.path.expanduser("~")
        display_cwd = cwd
        if display_cwd == home:
            display_cwd = "~"
        elif display_cwd.startswith(home + "/"):
            display_cwd = "~" + display_cwd[len(home):]
        symbol = "%" if IS_MAC else "$"
        return f"{venv_prefix}{user}@{host} {display_cwd} {symbol} "


class PersistentTerminalSession:
    def __init__(self):
        self.cwd = os.getcwd()
        self.venv_name = ""
        self.prompt = format_os_prompt(self.cwd)
        self.process: subprocess.Popen | None = None
        self.current_marker: str | None = None
        self.reader_task: asyncio.Task | None = None

        # Clean environment + Unbuffered Python output for interactive scripts
        self.clean_env = os.environ.copy()
        self.clean_env.pop("VIRTUAL_ENV", None)
        self.clean_env.pop("VIRTUAL_ENV_PROMPT", None)
        self.clean_env["PYTHONUNBUFFERED"] = "1"
        self.clean_env["PYTHONIOENCODING"] = "utf-8"
        if not IS_WINDOWS:
            self.clean_env["TERM"] = "dumb"
            self.clean_env["PS1"] = ""
            self.clean_env["PROMPT"] = ""

    def start_shell(self):
        """Starts a persistent shell process (cmd.exe on Windows, bash/zsh on macOS)."""
        if IS_WINDOWS:
            self.process = subprocess.Popen(
                ["cmd.exe", "/Q", "/V:ON", "/K", "prompt $S"],
                cwd=self.cwd,
                env=self.clean_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
        else:
            # Use bash --norc --noprofile on macOS/Linux for clean non-echoing pipes
            # (fully supports `source venv/bin/activate`, `cd`, `export`, etc.)
            shell_bin = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
            self.process = subprocess.Popen(
                [shell_bin, "--norc", "--noprofile"],
                cwd=self.cwd,
                env=self.clean_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,  # Creates process group so Ctrl+C kills child tree
                bufsize=0,
            )

    def restart_shell(self):
        """Terminates running command tree and restarts the shell at the last known CWD."""
        self.close()
        self.start_shell()

    def close(self):
        if self.process and self.process.poll() is None:
            try:
                if IS_WINDOWS:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    # Kill entire POSIX process group
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except Exception:
                pass
        self.process = None


# ==========================================
# CROSS-PLATFORM TAB AUTOCOMPLETE ENGINE
# ==========================================
def get_tab_completions(text: str, cwd: str) -> dict:
    in_quotes = False
    last_space_idx = -1
    for i, ch in enumerate(text):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ' ' and not in_quotes:
            last_space_idx = i

    prefix_cmd = text[: last_space_idx + 1]
    raw_token = text[last_space_idx + 1 :]
    clean_token = raw_token.strip('"').strip("'")

    sep = "\\" if IS_WINDOWS else "/"

    # Support `~` home directory expansion on macOS/Linux
    expanded_token = os.path.expanduser(clean_token) if not IS_WINDOWS else clean_token

    if "\\" in clean_token or "/" in clean_token:
        normalized = clean_token.replace("\\", sep).replace("/", sep)
        dir_part, file_prefix = normalized.rsplit(sep, 1)
        dir_prefix_display = (dir_part + sep) if dir_part else sep

        expanded_dir_part = os.path.expanduser(dir_part) if not IS_WINDOWS else dir_part
        if not IS_WINDOWS and dir_part == "":
            search_dir = "/"
        else:
            search_dir = os.path.abspath(os.path.join(cwd, expanded_dir_part))
    else:
        dir_prefix_display = ""
        file_prefix = expanded_token
        search_dir = cwd

    if not os.path.isdir(search_dir):
        return {"matches": [], "prefix_cmd": prefix_cmd}

    try:
        entries = os.listdir(search_dir)
    except Exception:
        return {"matches": [], "prefix_cmd": prefix_cmd}

    matches = []
    for entry in sorted(entries, key=lambda s: s.lower()):
        # Hide dotfiles on macOS/Linux unless the user explicitly typed `.`
        if not IS_WINDOWS and entry.startswith(".") and not file_prefix.startswith("."):
            continue

        if entry.lower().startswith(file_prefix.lower()):
            full_entry_path = os.path.join(search_dir, entry)
            candidate = f"{dir_prefix_display}{entry}"
            if os.path.isdir(full_entry_path):
                candidate += sep
            if " " in candidate:
                candidate = f'"{candidate}"'
            matches.append(candidate)

    return {"matches": matches, "prefix_cmd": prefix_cmd}


# ==========================================
# MODULAR FEATURE SYSTEM (ASYNC)
# ==========================================
async def handle_markdown_module(cmd: str, session: PersistentTerminalSession, ws: WebSocket) -> bool:
    """Module 1: Markdown Renderer (Supports `md`, `mdview`, `type *.md`, and `cat *.md`)"""
    if cmd.startswith("md "):
        raw_md = cmd[3:].replace("\\n", "\n")
        await ws.send_json({"type": "markdown", "data": raw_md})
        await ws.send_json({"type": "complete", "cwd": session.cwd, "prompt": session.prompt})
        return True

    cmd_lower = cmd.lower()
    is_mdview = cmd_lower.startswith("mdview ")
    is_type_md = cmd_lower.startswith("type ") and cmd_lower.endswith(".md")
    is_cat_md = cmd_lower.startswith("cat ") and cmd_lower.endswith(".md")

    if is_mdview or is_type_md or is_cat_md:
        prefix_len = 7 if is_mdview else (5 if is_type_md else 4)
        filename = cmd[prefix_len:].strip().strip('"').strip("'")
        if not IS_WINDOWS:
            filename = os.path.expanduser(filename)
        filepath = os.path.join(session.cwd, filename)

        if os.path.exists(filepath) and os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                await ws.send_json({"type": "markdown", "data": content})
            except Exception as e:
                await ws.send_json({"type": "error", "data": f"Error reading file: {repr(e)}\n"})
        else:
            await ws.send_json({"type": "error", "data": f"File not found: {filename}\n"})

        await ws.send_json({"type": "complete", "cwd": session.cwd, "prompt": session.prompt})
        return True

    return False


MODULES = [
    handle_markdown_module,
]


# ==========================================
# BACKGROUND STDOUT STREAM READER
# ==========================================
async def shell_stdout_reader(session: PersistentTerminalSession, ws: WebSocket):
    """Continuously streams stdout from the persistent shell (Windows or macOS)."""
    buffer = ""
    while session.process and session.process.poll() is None:
        try:
            chunk = await asyncio.to_thread(session.process.stdout.read, 256)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")

            if not session.current_marker:
                continue

            buffer += text
            marker = session.current_marker
            start_tag = f"{marker}_START"
            mid_tag = f"{marker}_MID"
            end_tag = f"{marker}_END"

            if end_tag in buffer:
                if start_tag in buffer and mid_tag in buffer:
                    before_start, remainder = buffer.split(start_tag, 1)
                    cwd_block, remainder2 = remainder.split(mid_tag, 1)
                    venv_block, _ = remainder2.split(end_tag, 1)

                    clean_output = before_start.lstrip(" ") if IS_WINDOWS else before_start
                    if clean_output:
                        await ws.send_json({"type": "stream", "data": clean_output})

                    # 1. Extract live post-command CWD
                    candidate_cwd = cwd_block.strip()
                    if candidate_cwd and os.path.isdir(candidate_cwd):
                        session.cwd = candidate_cwd

                    # 2. Extract live VIRTUAL_ENV
                    venv_val = venv_block.strip()
                    session.venv_name = ""
                    if venv_val and venv_val not in ("!VIRTUAL_ENV!", "$VIRTUAL_ENV", "ECHO is off.", "ECHO is on."):
                        session.venv_name = os.path.basename(venv_val.rstrip("\\/"))

                    session.prompt = format_os_prompt(session.cwd, session.venv_name)

                session.current_marker = None
                buffer = ""

                await ws.send_json({
                    "type": "complete",
                    "cwd": session.cwd,
                    "prompt": session.prompt
                })
            else:
                if "__M_" in buffer:
                    continue
                if buffer.strip(" ") if IS_WINDOWS else buffer:
                    await ws.send_json({"type": "stream", "data": buffer})
                buffer = ""

        except asyncio.CancelledError:
            break
        except Exception:
            break


# ==========================================
# ROUTES & WEBSOCKET ENDPOINT
# ==========================================
@app.get("/")
async def get_terminal(request: Request):
    cwd = os.getcwd()
    os_banner = (
        "Microsoft Windows [Version 10.0.22631.SmartCMD-Persistent]"
        if IS_WINDOWS
        else f"macOS Darwin [{platform.mac_ver()[0] or platform.release()} - SmartTerminal-Persistent]"
    )
    return templates.TemplateResponse(
        request=request,
        name=frontend,
        context={
            "initial_cwd": cwd,
            "initial_prompt": format_os_prompt(cwd),
            "os_banner": os_banner,
        }
    )


@app.websocket("/ws")
async def terminal_websocket(ws: WebSocket):
    await ws.accept()
    session = PersistentTerminalSession()
    session.start_shell()
    session.reader_task = asyncio.create_task(shell_stdout_reader(session, ws))

    try:
        while True:
            message = await ws.receive_json()
            action = message.get("action")

            # 1. Handle Tab Autocomplete Request
            if action == "autocomplete":
                text = message.get("text", "")
                result = get_tab_completions(text, session.cwd)
                await ws.send_json({
                    "type": "autocomplete",
                    "matches": result["matches"],
                    "prefix_cmd": result["prefix_cmd"]
                })
                continue

            # 2. Handle Ctrl+C Interrupt Signal
            if action == "kill":
                if session.current_marker:
                    session.current_marker = None
                    if session.reader_task:
                        session.reader_task.cancel()
                    session.restart_shell()
                    session.reader_task = asyncio.create_task(shell_stdout_reader(session, ws))
                    await ws.send_json({"type": "error", "data": "^C\n"})
                    await ws.send_json({
                        "type": "complete",
                        "cwd": session.cwd,
                        "prompt": session.prompt
                    })
                continue

            # 3. Handle Interactive STDIN while a command is running
            if action == "stdin":
                if session.process and session.process.stdin:
                    line_ending = "\r\n" if IS_WINDOWS else "\n"
                    user_input = (message.get("data", "") + line_ending).encode("utf-8")
                    await asyncio.to_thread(session.process.stdin.write, user_input)
                    await asyncio.to_thread(session.process.stdin.flush)
                continue

            # 4. Handle Command Execution inside Persistent Shell
            if action == "execute":
                cmd = message.get("command", "").strip()

                if not cmd:
                    await ws.send_json({"type": "complete", "cwd": session.cwd, "prompt": session.prompt})
                    continue

                # Check Custom Modular Features First
                intercepted = False
                for module in MODULES:
                    if await module(cmd, session, ws):
                        intercepted = True
                        break
                if intercepted:
                    continue

                marker = f"__M_{uuid.uuid4().hex[:12]}__"
                session.current_marker = marker

                if IS_WINDOWS:
                    cmd_lower = cmd.lower()
                    is_cd_or_drive = (
                        re.match(r"^(cd|chdir)([\s\\./]|$)", cmd_lower) is not None
                        or re.match(r"^[a-z]:$", cmd_lower) is not None
                    )
                    first_word = cmd_lower.split()[0] if cmd_lower.split() else ""
                    cmd_builtins = {
                        "cd", "chdir", "dir", "echo", "set", "cls", "type", "copy", "del",
                        "md", "mkdir", "rd", "rmdir", "ren", "move", "path", "ver", "vol",
                        "assoc", "ftype", "color", "title", "mklink", "pushd", "popd"
                    }

                    if is_cd_or_drive and len(cmd) > 3 and cmd.endswith("\\") and not cmd.endswith(":\\"):
                        cmd = cmd.rstrip("\\")

                    exec_part = cmd if (is_cd_or_drive or first_word in cmd_builtins) else f"call {cmd}"

                    single_line_cmd = (
                        f"@{exec_part} & "
                        f"@prompt $S & "
                        f"@echo {marker}_START& "
                        f"@cd & "
                        f"@echo {marker}_MID& "
                        f"@echo !VIRTUAL_ENV!& "
                        f"@echo {marker}_END\r\n"
                    )
                else:
                    # macOS / Linux single-line execution + state capture
                    # Also support typing `cd..` on macOS as a friendly alias for `cd ..`
                    if cmd == "cd..":
                        cmd = "cd .."
                    single_line_cmd = (
                        f"{cmd} ; "
                        f'printf "\\n{marker}_START\\n" ; '
                        f"pwd ; "
                        f'printf "{marker}_MID\\n%s\\n{marker}_END\\n" "$VIRTUAL_ENV"\n'
                    )

                await asyncio.to_thread(session.process.stdin.write, single_line_cmd.encode("utf-8"))
                await asyncio.to_thread(session.process.stdin.flush)

    except WebSocketDisconnect:
        if session.reader_task:
            session.reader_task.cancel()
        session.close()