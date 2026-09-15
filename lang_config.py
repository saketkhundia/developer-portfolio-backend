"""Authoritative multi-language execution configuration for DevIQ.

This is the SINGLE source of truth for language routing, shared by the batch
engine (POST /execute) and the interactive sessions (/exec/*).

Root causes this fixes:
  1. Java (or any language) executed by the wrong runtime (e.g. `node`
     on a .java file). Routing is keyed ONLY on the normalized `language`
     field of the request. Filenames are cosmetic (except where a compiler
     itself demands a name, e.g. javac + public class) and are NEVER used
     to pick a runtime.
  2. Scattered if/else dispatch across the codebase. Every supported
     language is exactly one LangSpec row: source file, required tools,
     compile argv, run argv. Adding/removing a language is one row.

Conventions:
  * `compile_argv` / `run_argv` are argv lists (never shell strings).
  * `runner` overrides the run command for interactive sessions (auto-flush
    wrappers). `batch_runner` overrides it for one-shot /execute runs.
    When None, `run_argv` is used for both.
"""

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# Forces prompt bytes out of the JVM immediately. `System.out.print("...")`
# with no newline otherwise sits in PrintStream buffers while the program
# blocks on stdin, so an interactive user never sees the prompt.
RUNNER_JAVA = """public class __Runner {
    public static void main(String[] args) throws Exception {
        System.setOut(new java.io.PrintStream(
            new java.io.FileOutputStream(java.io.FileDescriptor.out), true));
        System.setErr(new java.io.PrintStream(
            new java.io.FileOutputStream(java.io.FileDescriptor.err), true));
        Class<?> c = Class.forName(args[0]);
        c.getMethod("main", String[].class).invoke(null, (Object) new String[0]);
    }
}
"""

_JAVA_CLASS_RE = re.compile(
    r"public\s+(?:abstract\s+|final\s+|static\s+|sealed\s+|non-sealed\s+)*"
    r"class\s+([A-Za-z_]\w*)"
)


def java_main_class(code: str) -> str:
    """The single public class name, else the conventional fallback.

    NOTE: this only names the file for javac (which requires
    `public class X` to live in `X.java`). It is NOT routing: the runtime
    still comes from the request's `language` field.
    """
    names = sorted(set(_JAVA_CLASS_RE.findall(code or "")))
    return names[0] if len(names) == 1 else "Main"


def normalize_language(raw: object) -> str:
    return (raw or "").lower().strip() if isinstance(raw, str) else ""


def have(*tools: str) -> bool:
    return all(shutil.which(t) for t in tools)


def safe_source_name(name: object, fallback: str) -> str:
    if isinstance(name, str):
        base = os.path.basename(name.strip()) or fallback
    else:
        base = fallback
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9]{1,10}", base):
        return fallback
    return base[:64]


def child_env(tmp: str) -> Dict[str, str]:
    """Minimal, secret-free environment for child processes."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": tmp,
        "TMPDIR": tmp,
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GOTOOLCHAIN": "local",  # never phone home for toolchains
        "GOPROXY": "off",        # stdlib-only builds work offline
        "GOFLAGS": "-mod=mod",
        "GOCACHE": os.path.join(tmp, ".gocache"),
    }
    try:
        os.makedirs(env["GOCACHE"], exist_ok=True)
    except Exception:
        pass
    return env


@dataclass
class LangSpec:
    """One row = one language. The router looks up by id, nothing else."""
    id: str
    filename: str
    tools: List[str] = field(default_factory=list)
    compile_argv: Optional[List[str]] = None
    run_argv: Optional[List[str]] = None
    # fn(code, filename) -> source filename (javac needs X.java for class X)
    namer: Optional[Callable[[str, Optional[str]], str]] = None
    # fn(source_file) -> argv overrides
    runner: Optional[Callable[[str], List[str]]] = None        # sessions
    batch_runner: Optional[Callable[[str], List[str]]] = None  # /execute
    # fn(tmpdir) -> extra files/argv for the compile step (sessions)
    setup: Optional[Callable[[str], List[str]]] = None


def _java_setup(tmp: str) -> List[str]:
    with open(os.path.join(tmp, "__Runner.java"), "w", encoding="utf-8") as f:
        f.write(RUNNER_JAVA)
    return ["__Runner.java"]


def _java_batch_run(src: str) -> List[str]:
    return ["java", "-cp", ".", os.path.splitext(os.path.basename(src))[0]]


def _java_session_run(src: str) -> List[str]:
    return ["java", "-cp", ".", "__Runner",
            os.path.splitext(os.path.basename(src))[0]]


LANGS: Dict[str, LangSpec] = {
    "python": LangSpec(
        id="python",
        filename="main.py",
        tools=[],
        run_argv=[sys.executable, "-u", "main.py"],
    ),
    "javascript": LangSpec(
        id="javascript",
        filename="main.js",
        tools=["node"],
        run_argv=["node", "main.js"],
    ),
    "typescript": LangSpec(
        id="typescript",
        filename="main.ts",
        tools=["tsc", "node"],
        compile_argv=["tsc", "main.ts", "--target", "es2020",
                      "--module", "commonjs", "--outDir", "."],
        run_argv=["node", "main.js"],
    ),
    "java": LangSpec(
        id="java",
        filename="Main.java",
        tools=["javac", "java"],
        namer=lambda code, _fn: f"{java_main_class(code)}.java",
        setup=_java_setup,  # sessions only; batch compiles the single file
        runner=_java_session_run,
        batch_runner=_java_batch_run,
    ),
    "c": LangSpec(
        id="c",
        filename="main.c",
        tools=["gcc", "stdbuf"],
        compile_argv=["gcc", "main.c", "-o", "main"],
        run_argv=["stdbuf", "-o0", "-e0", "./main"],
    ),
    "cpp": LangSpec(
        id="cpp",
        filename="main.cpp",
        tools=["g++", "stdbuf"],
        compile_argv=["g++", "main.cpp", "-o", "main"],
        run_argv=["stdbuf", "-o0", "-e0", "./main"],
    ),
    "go": LangSpec(
        id="go",
        filename="main.go",
        tools=["go"],
        compile_argv=["go", "build", "-o", "main", "main.go"],
        run_argv=["./main"],
    ),
}


def source_filename(lang: str, code: str, filename: Optional[str]) -> str:
    spec = LANGS[lang]
    raw = spec.namer(code, filename) if spec.namer else (filename or spec.filename)
    fallback = (spec.namer(code, None) if spec.namer
                else spec.filename)
    return safe_source_name(raw, safe_source_name(fallback, spec.filename))


def compile_argv_for(lang: str, src: str) -> Optional[List[str]]:
    """Batch/session-shared compile command. Java always compiles the user
    file by its (possibly class-derived) name; session extras are separate."""
    spec = LANGS[lang]
    if spec.id == "java":
        return ["javac", src]
    return list(spec.compile_argv) if spec.compile_argv else None


def run_argv_for(lang: str, src: str, interactive: bool) -> List[str]:
    spec = LANGS[lang]
    if interactive and spec.runner:
        return spec.runner(src)
    if not interactive and spec.batch_runner:
        return spec.batch_runner(src)
    assert spec.run_argv, f"no run command configured for {lang}"
    return list(spec.run_argv)
