"""The model behind Abacus, kept behind one small interface so the backend can be swapped.

Everything else in Abacus talks to a `Backend`:

    quick(system, text)   one fast text-in, text-out call with no tools (dictation clean-up,
                          rewriting a selection). Must come back in about a second.
    ask(text, on_event)   one agent turn: the model may use tools (desktop, Chrome, shell) and
                          returns the sentence Abacus will speak. on_event reports progress.
    prepare()             called the moment the wake word is heard, so anything slow (starting
                          a session) overlaps with you speaking.

Today the only backend is ClaudeCodeBackend, which drives the `claude` CLI and therefore runs on
the Claude subscription you are logged in with. It is "real time" because both lanes are
long-lived processes that speak stream-json on stdin/stdout: start-up (about 3 s for the agent,
1 s for the quick lane) is paid once, ahead of time, instead of on every command.

To ship Abacus as a product, write another Backend subclass (for example one that calls your
own server) and select it with config.BACKEND. Nothing else needs to change.
"""
import collections
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import config

HERE = Path(__file__).resolve().parent
log = logging.getLogger("abacus")
CREATE_NO_WINDOW = 0x08000000


class BackendError(RuntimeError):
    pass


class Backend:
    """Interface every backend implements."""
    name = "base"

    def warm(self) -> None:
        """Start whatever can be started ahead of time. Must not block."""

    def prepare(self) -> None:
        """A command is about to arrive (wake word heard). Must not block."""

    def quick(self, system: str, text: str, timeout: float = 8.0) -> str:
        raise NotImplementedError

    def ask(self, text: str, on_event=None) -> str:
        raise NotImplementedError

    def reset(self) -> None:
        """Forget the conversation so far."""

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- claude CLI, kept warm

class _Stream:
    """One long-lived `claude -p` process that takes a user message per line on stdin and
    reports what happens as JSON lines on stdout."""

    def __init__(self, args: list[str], cwd: str, label: str) -> None:
        self.label = label
        self.started = time.time()
        self.turns = 0
        self.session_id: str | None = None
        self.events: queue.Queue = queue.Queue()
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=30)
        env = dict(os.environ, ENABLE_TOOL_SEARCH="false")  # load every tool up front: no
        # "search for the tool first" round trip on each command
        self.proc = subprocess.Popen(
            args, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._read_out, daemon=True, name=f"{label}-out").start()
        threading.Thread(target=self._read_err, daemon=True, name=f"{label}-err").start()

    def _read_out(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.events.put(json.loads(line))
                except json.JSONDecodeError:
                    self.stderr_tail.append(line[:300])
        finally:
            self.events.put(None)  # end of stream

    def _read_err(self) -> None:
        for line in self.proc.stderr:
            self.stderr_tail.append(line.rstrip()[:300])

    def alive(self) -> bool:
        return self.proc.poll() is None

    def turn(self, text: str, timeout: float, on_event=None) -> dict:
        """Send one message, return the final `result` event."""
        if not self.alive():
            raise BackendError(f"{self.label} process is not running")
        try:
            self.proc.stdin.write(json.dumps(
                {"type": "user", "message": {"role": "user", "content": text}},
                ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise BackendError(f"{self.label} stdin closed: {e}") from e
        deadline = time.time() + timeout
        while True:
            try:
                ev = self.events.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                self.close()
                raise TimeoutError(f"{self.label} took longer than {timeout:.0f}s") from None
            if ev is None:
                raise BackendError(f"{self.label} exited: " + " | ".join(self.stderr_tail)[-600:])
            kind = ev.get("type")
            if kind == "result":
                self.turns += 1
                self.session_id = ev.get("session_id") or self.session_id
                return ev
            if kind == "assistant" and on_event:
                for block in ev.get("message", {}).get("content", []):
                    try:
                        if block.get("type") == "tool_use":
                            on_event("tool", block.get("name", ""), block.get("input") or {})
                        elif block.get("type") == "text" and block.get("text", "").strip():
                            on_event("text", block["text"], {})
                    except Exception:
                        log.exception("on_event failed")

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        if self.alive():
            try:
                self.proc.kill()
            except Exception:
                pass


class ClaudeCodeBackend(Backend):
    """Claude Code CLI on the logged-in subscription."""
    name = "claude_code"
    QUICK_RECYCLE_TURNS = 20   # the quick lane restarts after this many calls (keeps it small)

    def __init__(self) -> None:
        self.claude = shutil.which("claude")
        if not self.claude:
            raise SystemExit("Can't find the `claude` CLI on PATH.")
        self.mcp_config = HERE / "mcp.json"
        self.mcp_config.write_text(json.dumps({"mcpServers": {"abacus": {
            "command": sys.executable, "args": [str(HERE / "abacus_mcp.py")],
        }}}, indent=2))
        self.auto_mode_settings = self._auto_mode_settings()
        self.lock = threading.RLock()          # guards the two lane slots below
        self.agent: _Stream | None = None
        self.agent_lock = threading.Lock()     # one agent turn at a time
        self.agent_last_used = 0.0
        self.quick_lanes: dict[str, _Stream] = {}
        self.quick_lock = threading.Lock()

    # ---- command lines
    @staticmethod
    def _auto_mode_settings() -> str | None:
        """No settings files are loaded (see --setting-sources below), but the auto-mode
        classifier reads its `autoMode` block from --settings, so pass just that through."""
        try:
            user = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return json.dumps({"autoMode": user["autoMode"]}) if "autoMode" in user else None

    @staticmethod
    def _persona_file() -> Path:
        """persona.md with {ABACUS_DIR} filled in (the guide it points to lives next to it)."""
        out = HERE / "persona.rendered.md"
        text = (HERE / "persona.md").read_text(encoding="utf-8")
        out.write_text(text.replace("{ABACUS_DIR}", str(HERE)), encoding="utf-8")
        return out

    def _agent_args(self) -> list[str]:
        cmd = [
            self.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", config.PERMISSION_MODE,
            "--permission-prompt-tool", "mcp__abacus__approve",
            "--mcp-config", str(self.mcp_config), "--strict-mcp-config",
            # No settings files: keeps plugins and their hooks (about a second per start, plus
            # a check on every tool call) out of the voice loop.
            "--setting-sources", "",
            "--append-system-prompt-file", str(self._persona_file()),
            "--allowedTools", *config.AUTO_ALLOWED_TOOLS,
        ]
        if getattr(config, "BROWSER", False):
            cmd.insert(2, "--chrome")  # Claude in Chrome: your signed-in browser
        if self.auto_mode_settings:
            cmd += ["--settings", self.auto_mode_settings]
        if getattr(config, "CLAUDE_MODEL", None):
            cmd += ["--model", config.CLAUDE_MODEL]
        if getattr(config, "CLAUDE_EFFORT", None):
            cmd += ["--effort", config.CLAUDE_EFFORT]
        return cmd

    def _quick_args(self, system: str) -> list[str]:
        return [
            self.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--model", getattr(config, "QUICK_MODEL", "haiku"),
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--disable-slash-commands", "--no-session-persistence",
            "--system-prompt", system,
        ]

    # ---- lanes
    def _agent_stream(self) -> _Stream:
        with self.lock:
            if self.agent is None or not self.agent.alive():
                self.agent = _Stream(self._agent_args(), config.WORK_DIR, "agent")
                log.info("(agent session starting)")
            return self.agent

    def _quick_stream(self, system: str) -> _Stream:
        with self.lock:
            lane = self.quick_lanes.get(system)
            if lane is None or not lane.alive():
                lane = self.quick_lanes[system] = _Stream(
                    self._quick_args(system), str(HERE), "quick")
            return lane

    def warm(self, quick_system: str | None = None) -> None:
        def go():
            try:
                self._agent_stream()
                if quick_system:
                    self._quick_stream(quick_system)
            except Exception:
                log.exception("warm-up failed")
        threading.Thread(target=go, daemon=True, name="warm").start()

    def prepare(self) -> None:
        """Wake word heard: if the conversation has gone stale, swap in a fresh session now,
        while the command is still being spoken."""
        stale = (self.agent is not None and self.agent.turns > 0
                 and time.time() - self.agent_last_used > config.SESSION_IDLE_RESET)
        if stale and self.agent_lock.acquire(blocking=False):
            try:
                self.reset()
            finally:
                self.agent_lock.release()
        self.warm()

    def reset(self) -> None:
        with self.lock:
            if self.agent is not None:
                self.agent.close()
                self.agent = None

    def close(self) -> None:
        with self.lock:
            for lane in [self.agent, *self.quick_lanes.values()]:
                if lane is not None:
                    lane.close()
            self.agent, self.quick_lanes = None, {}

    # ---- calls
    def quick(self, system: str, text: str, timeout: float = 8.0) -> str:
        with self.quick_lock:
            last_error: Exception | None = None
            for attempt in (1, 2):
                lane = self._quick_stream(system)
                try:
                    started = time.time()
                    out = lane.turn(text, timeout)
                    log.info("(quick lane %.1fs)", time.time() - started)
                    if out.get("is_error"):
                        raise BackendError(str(out.get("result", ""))[:200])
                    if lane.turns >= self.QUICK_RECYCLE_TURNS:   # fresh one for next time
                        lane.close()
                        threading.Thread(target=self._quick_stream, args=(system,),
                                         daemon=True).start()
                    return out.get("result") or ""
                except TimeoutError:
                    raise
                except BackendError as e:
                    last_error = e
                    log.warning("quick lane failed (%s)%s", e, "; retrying" if attempt == 1 else "")
                    lane.close()
            raise BackendError(str(last_error))

    def ask(self, text: str, on_event=None) -> str:
        with self.agent_lock:
            if (self.agent is not None and self.agent.turns > 0
                    and time.time() - self.agent_last_used > config.SESSION_IDLE_RESET):
                self.reset()  # stale conversation: start clean
            log.info("You: %s", text)
            started = time.time()
            out = None
            for attempt in (1, 2):
                stream = self._agent_stream()
                try:
                    out = stream.turn(text, config.CLAUDE_TIMEOUT, on_event)
                    break
                except TimeoutError:
                    self.agent_last_used = time.time()
                    raise
                except BackendError as e:
                    log.error("agent session failed: %s", e)
                    self.reset()
                    if attempt == 2:
                        return "Sorry, Claude Code returned an error. Details are in the log."
            self.agent_last_used = time.time()
            log.info("(took %.1fs, $%s)", time.time() - started, out.get("total_cost_usd", "?"))
            if out.get("is_error"):
                log.error("claude error: %s", str(out)[:1500])
                self.reset()  # a broken session shouldn't wedge every later command
                return "Sorry, that didn't work. " + str(out.get("result", ""))[:200]
            return out.get("result") or "Done."


BACKENDS = {"claude_code": ClaudeCodeBackend}


def make_backend() -> Backend:
    name = getattr(config, "BACKEND", "claude_code")
    try:
        return BACKENDS[name]()
    except KeyError:
        raise SystemExit(f"Unknown BACKEND {name!r} in config.py; choose from {list(BACKENDS)}") from None
