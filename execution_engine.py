"""One-shot batch code execution for DevIQ: POST /execute.

Request (all JSON):
    {
        "language": "java",          # REQUIRED, authoritative routing key
        "code": "...",               # REQUIRED, source text
        "stdin": "Saket\\n21\\n90\\n",  # optional, fed to the process stdin
        "timeout": 10,               # optional seconds, default 10, max 60
        "filename": "UserInput.java" # optional, cosmetic (see note)
    }

Routing is keyed ONLY on `language` (see lang_config). The filename never
selects a runtime; for Java it only seeds the source name because javac
itself requires `public class X` to live in `X.java`.

Stdin semantics (the core bug this fixes): the runtime is spawned with a
stdin PIPE, the full payload is written, then the pipe is closed
(`write(input)` + `end()`). stdin is NEVER closed before the process has
had a chance to consume the input, so `Scanner.nextLine()` etc. see the
data instead of EOF.

Response (always this shape):
    {
        "success": true|false,
        "language": "java",
        "stdout": "...",
        "stderr": "",
        "compile_output": "",
        "exit_code": 0,
        "execution_time": 0.42,
        "error_type": null | "compile_error" | "runtime_error" | "timeout"
                      | "invalid_language" | "missing_runtime"
                      | "input_error" | "system_error"
    }

HTTP status: 200 for completed executions (including compile/runtime/
timeout verdicts); 4xx/5xx for rejected requests (bad language, oversize
input, missing toolchain, internal failure).

Isolation per run: fresh temp dir (removed afterwards), argv-only exec
(no shell), minimal secret-free env, whole-process-group kill on timeout,
output caps, optional address-space/file limits via setrlimit on POSIX.
This is defense-in-depth for owned infrastructure, not a hard sandbox:
run it in a disposable container (see Dockerfile).
"""

import os
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from lang_config import (
    LANGS,
    child_env,
    compile_argv_for,
    have,
    normalize_language,
    run_argv_for,
    source_filename,
)

router = APIRouter(tags=["execute"])

EXEC_ENABLED = os.environ.get("EXEC_ENABLED", "1") == "1"
MAX_CODE_BYTES = 100_000
MAX_STDIN_BYTES = 20_000
MAX_OUTPUT_BYTES = 256_000
DEFAULT_TIMEOUT_S = 10
MAX_TIMEOUT_S = 60
COMPILE_TIMEOUT_S = 30
# Address-space cap for children. NOTE: this caps *virtual* memory, and
# managed runtimes (JVM, V8) reserve gigabytes of virtual space at startup
# without using it, so this must stay generous (1GB breaks javac/node).
# Real physical-memory enforcement belongs at the container level
# (Docker --memory / instance size); this only stops runaway virtual maps.
MAX_MEMORY_MB = int(os.environ.get("EXEC_MAX_MEMORY_MB", "8192"))
MAX_FILES_MB = 32


def _result(success: bool, language: str, stdout: str, stderr: str,
            compile_output: str, exit_code: Optional[int],
            elapsed: float, error_type: Optional[str]):
    return {
        "success": success,
        "language": language,
        "stdout": stdout,
        "stderr": stderr,
        "compile_output": compile_output,
        "exit_code": exit_code,
        "execution_time": round(elapsed, 3),
        "error_type": error_type,
    }


def _truncate(s: str) -> tuple:
    if len(s.encode("utf-8", errors="ignore")) <= MAX_OUTPUT_BYTES:
        return s, False
    out = s.encode("utf-8", errors="ignore")[:MAX_OUTPUT_BYTES]
    return out.decode("utf-8", errors="ignore"), True


def _limit_resources():
    """Child-side limits (POSIX only). Runs post-fork/pre-exec: keep it to
    pure setrlimit calls — no locks, no I/O."""
    try:
        import resource
        mem = MAX_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        fsize = MAX_FILES_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    except Exception:
        pass


class ExecuteRequest(BaseModel):
    language: str = ""
    code: str = ""
    stdin: str = ""
    timeout: int = DEFAULT_TIMEOUT_S
    filename: Optional[str] = None


@router.post("/execute")
def execute(req: ExecuteRequest):
    t0 = time.perf_counter()
    elapsed = lambda: time.perf_counter() - t0

    if not EXEC_ENABLED:
        return JSONResponse(status_code=501, content=_input_error(
            normalize_language(req.language), elapsed(),
            "Code execution is disabled on this backend")
            | {"error_type": "system_error"})

    # -- 1. validate language (authoritative routing key) ------------------
    lang = normalize_language(req.language)
    spec = LANGS.get(lang)
    if spec is None:
        return JSONResponse(status_code=400, content={
            "success": False, "language": req.language or "",
            "stdout": "", "stderr": "",
            "compile_output": "",
            "exit_code": None, "execution_time": elapsed(),
            "error_type": "invalid_language",
            "message": f"Unsupported language: {req.language or '(none)'}. "
                       f"Supported: {sorted(LANGS)}",
        })

    # -- 2. validate payload ------------------------------------------------
    code = req.code or ""
    if not code.strip():
        return JSONResponse(status_code=400, content=_input_error(
            lang, elapsed(), "No code provided"))
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return JSONResponse(status_code=400, content=_input_error(
            lang, elapsed(), "Code too large (max 100KB)"))
    stdin_text = req.stdin if isinstance(req.stdin, str) else ""
    try:
        timeout_s = int(req.timeout)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content=_input_error(
            lang, elapsed(), "timeout must be a number of seconds"))
    timeout_s = max(1, min(timeout_s, MAX_TIMEOUT_S))
    if len(stdin_text.encode("utf-8")) > MAX_STDIN_BYTES:
        return JSONResponse(status_code=400, content=_input_error(
            lang, elapsed(), "stdin too large (max 20KB)"))

    # -- 3. toolchain present? ----------------------------------------------
    if not have(*spec.tools):
        return JSONResponse(status_code=501, content={
            "success": False, "language": lang,
            "stdout": "", "stderr": "",
            "compile_output": "",
            "exit_code": None, "execution_time": elapsed(),
            "error_type": "missing_runtime",
            "message": f"No {lang} toolchain on this backend",
        })

    # -- 4. isolated working directory + source file -------------------------
    tmp = tempfile.mkdtemp(prefix="deviq-run-")
    try:
        src = source_filename(lang, code, req.filename)
        with open(os.path.join(tmp, src), "w", encoding="utf-8") as f:
            f.write(code)

        # -- 5. compile (if the language needs it) ---------------------------
        compile_argv = compile_argv_for(lang, src)
        if compile_argv:
            try:
                cp = subprocess.run(
                    compile_argv, cwd=tmp, capture_output=True, text=True,
                    timeout=COMPILE_TIMEOUT_S, env=child_env(tmp),
                    preexec_fn=_limit_resources
                    if hasattr(os, "fork") else None,
                )
            except subprocess.TimeoutExpired:
                return _result(False, lang, "", "",
                               f"Compilation timed out after "
                               f"{COMPILE_TIMEOUT_S}s.",
                               1, elapsed(), "compile_error")
            except OSError as e:
                return _result(False, lang, "", "", f"Toolchain error: {e}",
                               1, elapsed(), "system_error")
            build_out = ((cp.stdout or "") + (cp.stderr or "")).strip()
            if cp.returncode != 0:
                return _result(False, lang, "", "", build_out or
                               "Compilation failed.", cp.returncode,
                               elapsed(), "compile_error")
        else:
            build_out = ""

        # -- 6/7. spawn with stdin pipe, write input, close (write+end) ------
        run_argv = run_argv_for(lang, src, interactive=False)
        try:
            proc = subprocess.Popen(
                run_argv, cwd=tmp,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0, start_new_session=True,
                env=child_env(tmp),
                preexec_fn=_limit_resources
                if hasattr(os, "fork") else None,
            )
        except OSError as e:
            return _result(False, lang, "", "", f"Toolchain error: {e}",
                           1, elapsed(), "system_error")

        try:
            # communicate() feeds ALL of stdin, lets the process consume it
            # at its own pace, and only then sees EOF. This is the fix for
            # Scanner.nextLine() hitting NoSuchElementException: stdin is
            # never closed before the process reads it.
            out_b, err_b = proc.communicate(
                input=stdin_text.encode("utf-8"), timeout=timeout_s)
            rc = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired as e:
            out_b, err_b = e.output or b"", e.stderr or b""
            _kill_tree(proc)
            try:
                rest_out, rest_err = proc.communicate(timeout=5)
                out_b += rest_out or b""
                err_b += rest_err or b""
            except Exception:
                pass
            rc = None
            timed_out = True

        stdout, t1 = _truncate(out_b.decode("utf-8", errors="replace"))
        stderr, t2 = _truncate(err_b.decode("utf-8", errors="replace"))
        if t1 or t2:
            stderr += "\n[output truncated at 256KB]"
        if timed_out:
            stderr = (stderr + ("\n" if stderr else "") +
                      f"Execution timed out after {timeout_s} seconds.")
            return _result(False, lang, stdout, stderr, build_out, rc,
                           elapsed(), "timeout")
        if rc == 0:
            return _result(True, lang, stdout, stderr, build_out, 0,
                           elapsed(), None)
        return _result(False, lang, stdout, stderr, build_out, rc,
                       elapsed(), "runtime_error")
    finally:
        # -- 13. clean up ----------------------------------------------------
        shutil.rmtree(tmp, ignore_errors=True)


def _input_error(lang: str, elapsed_s: float, message: str):
    return {
        "success": False, "language": lang,
        "stdout": "", "stderr": message,
        "compile_output": "",
        "exit_code": None, "execution_time": elapsed_s,
        "error_type": "input_error",
        "message": message,
    }


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
