"""Interactive code-execution sessions for the DevIQ playground terminal.

Why this exists: remote batch executors (e.g. Godbolt) take the whole stdin
upfront and return all output at the end, so a program like::

    System.out.print("Enter your name: ");
    String name = sc.nextLine();

can never pause mid-run and wait for the user to type. These endpoints keep a
real OS process alive per session with piped stdio, so the frontend terminal
can stream prompts out and keystrokes back in while the program runs.

Protocol (all JSON):
    POST /exec/start  {language, code, filename?} -> {session_id, state, ...}
    GET  /exec/poll/{session_id}?so=<n>&se=<m>   -> incremental stdout/stderr
    POST /exec/input  {session_id, line}          -> write one stdin line
    POST /exec/kill   {session_id}                -> terminate the process
    GET  /exec/languages                          -> toolchain availability

States: "running" | "exited" | "failed" (compile error) | "killed" | "timeout".

Safety notes (read before exposing publicly):
  * This runs untrusted code as the server user with NO sandbox. Only deploy
    it on disposable infrastructure you control (the Dockerfile installs the
    toolchains for this purpose).
  * Mitigations applied here: per-run wall-clock timeout, compile timeout,
    output/stdin caps, max concurrent sessions, session TTL with reaping,
    isolated temp dir per session, no shell (argv exec only), minimal env,
    whole process-group kill.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import BaseModel

from lang_config import (
    LANGS,
    child_env,
    compile_argv_for,
    have,
    run_argv_for,
    source_filename,
)

router = APIRouter(prefix="/exec", tags=["exec"])

EXEC_ENABLED = os.environ.get("EXEC_ENABLED", "1") == "1"
MAX_CODE_BYTES = 100_000
MAX_LINE_BYTES = 10_000
MAX_STDIN_TOTAL = 20_000
MAX_OUTPUT_BYTES = 256_000
MAX_POLL_CHUNK = 64_000
RUN_TIMEOUT_S = 60
COMPILE_TIMEOUT_S = 45
SESSION_TTL_S = 300
MAX_SESSIONS = 32

# ---------------------------------------------------------------- sessions

@dataclass
class Session:
    id: str
    language: str
    tmp: str
    state: str = "compiling"  # compiling|running|exited|failed|killed|timeout
    compile_output: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    created: float = field(default_factory=time.time)
    stdin_bytes: int = 0
    proc: Optional[subprocess.Popen] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    truncated: bool = False


_sessions: Dict[str, Session] = {}
_sessions_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _destroy(sess: Session) -> None:
    """Kill the process group and remove the temp dir. Idempotent."""
    try:
        if sess.proc is not None and sess.proc.poll() is None:
            try:
                os.killpg(sess.proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    sess.proc.kill()
                except Exception:
                    pass
    finally:
        for stream in (getattr(sess.proc, "stdin", None),
                       getattr(sess.proc, "stdout", None),
                       getattr(sess.proc, "stderr", None)):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        shutil.rmtree(sess.tmp, ignore_errors=True)


def _reap_loop() -> None:
    while True:
        time.sleep(30)
        cutoff = _now() - SESSION_TTL_S
        with _sessions_lock:
            stale = [s for s in _sessions.values() if s.created < cutoff]
            for s in stale:
                _sessions.pop(s.id, None)
        for s in stale:
            _destroy(s)


_reaper = threading.Thread(target=_reap_loop, daemon=True)
_reaper.start()


def _append(sess: Session, kind: str, text: str) -> None:
    with sess.lock:
        buf = sess.stdout if kind == "out" else sess.stderr
        if len(buf) + len(text) > MAX_OUTPUT_BYTES:
            text = text[: max(0, MAX_OUTPUT_BYTES - len(buf))]
            sess.truncated = True
        if kind == "out":
            sess.stdout += text
        else:
            sess.stderr += text


def _drain(sess: Session, kind: str) -> None:
    assert sess.proc is not None
    stream = sess.proc.stdout if kind == "out" else sess.proc.stderr
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            _append(sess, kind, chunk.decode("utf-8", errors="replace"))
    except Exception:
        pass


def _watchdog(sess: Session) -> None:
    """Wait for exit (bounded); enforce the wall-clock timeout."""
    assert sess.proc is not None
    try:
        rc = sess.proc.wait(timeout=RUN_TIMEOUT_S)
        with sess.lock:
            sess.exit_code = rc
            if sess.state == "running":
                sess.state = "exited"
    except subprocess.TimeoutExpired:
        with sess.lock:
            sess.state = "timeout"
            sess.stderr += f"\n[process killed after {RUN_TIMEOUT_S}s]"
        _destroy(sess)


def _get_session(sid: str) -> Session:
    with _sessions_lock:
        sess = _sessions.get(sid)
    if sess is None:
        raise HTTPException(404, "Unknown or expired session")
    return sess


# ---------------------------------------------------------------- models

class StartRequest(BaseModel):
    language: str = ""
    code: str = ""
    filename: Optional[str] = None


class InputRequest(BaseModel):
    session_id: str = ""
    line: str = ""


class KillRequest(BaseModel):
    session_id: str = ""


# ---------------------------------------------------------------- routes

@router.get("/languages")
def exec_languages():
    avail = {lang: have(*spec.tools) for lang, spec in LANGS.items()}
    return {"enabled": EXEC_ENABLED, "supported": avail,
            "limits": {"run_timeout_s": RUN_TIMEOUT_S,
                       "max_output_bytes": MAX_OUTPUT_BYTES,
                       "max_sessions": MAX_SESSIONS}}


@router.post("/start")
def exec_start(body: StartRequest):
    if not EXEC_ENABLED:
        raise HTTPException(501, "Interactive execution is disabled on this backend")
    lang = (body.language or "").lower().strip()
    spec = LANGS.get(lang)
    if spec is None:
        raise HTTPException(400, f"Unsupported language for interactive run: "
                                 f"{body.language or '(none)'}")
    code = body.code or ""
    if not code.strip():
        raise HTTPException(400, "No code provided")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise HTTPException(400, "Code too large (max 100KB)")
    if not have(*spec.tools):
        raise HTTPException(501, f"No {lang} toolchain on this backend")

    with _sessions_lock:
        live = sum(1 for s in _sessions.values()
                   if s.state in ("compiling", "running"))
        if live >= MAX_SESSIONS:
            raise HTTPException(429, "Execution backend is busy, try again")

    sid = uuid.uuid4().hex[:16]
    tmp = tempfile.mkdtemp(prefix=f"deviq-exec-{sid}-")
    src_name = source_filename(lang, code, body.filename)
    with open(os.path.join(tmp, src_name), "w", encoding="utf-8") as f:
        f.write(code)

    sess = Session(id=sid, language=lang, tmp=tmp)
    with _sessions_lock:
        _sessions[sid] = sess

    # -- compile step (blocking, bounded) -------------------------------
    compile_argv = compile_argv_for(lang, src_name) or []
    if spec.setup:
        try:
            compile_argv += list(spec.setup(tmp) or [])
        except Exception as e:
            sess.state = "failed"
            sess.compile_output = f"setup error: {e}"
            return {"session_id": sid, "state": sess.state,
                    "compile_output": sess.compile_output, "exit_code": 1}
    if compile_argv:
        try:
            cp = subprocess.run(
                compile_argv, cwd=tmp, capture_output=True, text=True,
                timeout=COMPILE_TIMEOUT_S,
                env=child_env(tmp),
            )
            out = ((cp.stdout or "") + (cp.stderr or "")).strip()
            if cp.returncode != 0:
                sess.state = "failed"
                sess.compile_output = out or "Compilation failed."
                sess.exit_code = cp.returncode
                return {"session_id": sid, "state": "failed",
                        "compile_output": sess.compile_output,
                        "exit_code": cp.returncode}
            sess.compile_output = out  # warnings, if any
        except subprocess.TimeoutExpired:
            sess.state = "failed"
            sess.compile_output = (f"Compilation timed out after "
                                   f"{COMPILE_TIMEOUT_S}s.")
            sess.exit_code = 1
            return {"session_id": sid, "state": "failed",
                    "compile_output": sess.compile_output, "exit_code": 1}
        except FileNotFoundError as e:
            sess.state = "failed"
            sess.compile_output = f"Toolchain error: {e.filename or e}"
            sess.exit_code = 1
            return {"session_id": sid, "state": "failed",
                    "compile_output": sess.compile_output, "exit_code": 1}

    # -- spawn ------------------------------------------------------------
    run_argv = run_argv_for(lang, src_name, interactive=True)
    try:
        proc = subprocess.Popen(
            run_argv, cwd=tmp, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, start_new_session=True, env=child_env(tmp),
        )
    except FileNotFoundError as e:
        sess.state = "failed"
        sess.compile_output = f"Toolchain error: {e.filename or e}"
        sess.exit_code = 1
        return {"session_id": sid, "state": "failed",
                "compile_output": sess.compile_output, "exit_code": 1}

    sess.proc = proc
    with sess.lock:
        sess.state = "running"
    threading.Thread(target=_drain, args=(sess, "out"), daemon=True).start()
    threading.Thread(target=_drain, args=(sess, "err"), daemon=True).start()
    threading.Thread(target=_watchdog, args=(sess,), daemon=True).start()
    return {"session_id": sid, "state": "running",
            "runtime": f"{lang} (interactive)",
            "compile_output": sess.compile_output}


@router.get("/poll/{session_id}")
def exec_poll(session_id: str, so: int = 0, se: int = 0):
    sess = _get_session(session_id)
    if so < 0 or se < 0:
        raise HTTPException(400, "Bad offset")
    with sess.lock:
        out, err = sess.stdout, sess.stderr
        state, rc, truncated = sess.state, sess.exit_code, sess.truncated
    return {
        "state": state,
        "stdout": out[so: so + MAX_POLL_CHUNK],
        "stderr": err[se: se + MAX_POLL_CHUNK],
        "so": len(out),
        "se": len(err),
        "exit_code": rc,
        "truncated": truncated,
    }


@router.post("/input")
def exec_input(body: InputRequest):
    sess = _get_session(body.session_id)
    line = body.line if isinstance(body.line, str) else ""
    data = (line + "\n").encode("utf-8")
    if len(data) > MAX_LINE_BYTES + 1:
        raise HTTPException(400, "Input line too long")
    with sess.lock:
        if sess.state != "running":
            raise HTTPException(409, "Session is not running")
        if sess.stdin_bytes + len(data) > MAX_STDIN_TOTAL:
            raise HTTPException(400, "stdin too large (max 20KB per run)")
        sess.stdin_bytes += len(data)
        proc = sess.proc
    try:
        assert proc is not None and proc.stdin is not None
        proc.stdin.write(data)
        proc.stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        return {"ok": False, "state": sess.state}
    return {"ok": True}


@router.post("/kill")
def exec_kill(body: KillRequest):
    sess = _get_session(body.session_id)
    with sess.lock:
        if sess.state in ("running", "compiling"):
            sess.state = "killed"
    _destroy(sess)
    # Keep the record so late polls observe the terminal state until reaped.
    return {"ok": True, "state": sess.state}
