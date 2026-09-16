"""Boot-time toolchain warm-up for the DevIQ execution backend.

Why this exists: in production (Docker on Render) the first runs after a
deploy or cold start pay every cold cost at once — JVM class loading, `go
build` compiling the stdlib, cold filesystem page cache. Measured locally:
a Go "hello world" takes ~2.4s wall / ~9.5s CPU with an empty cache vs
~0.15s with a warm shared cache; on a small production container that gap
is 10-30s vs under a second, and users see it as "running is slow".

`warm_toolchains()` compiles+runs one trivial program per language using the
exact argv builders from `lang_config`, so caches are hot before traffic
arrives. It runs in a background daemon thread from `main.py` startup (never
blocks boot), skips missing toolchains, never raises, and can be disabled
with `DEVIQ_WARMUP=0`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from typing import Dict

from lang_config import (
    LANGS,
    child_env,
    compile_argv_for,
    have,
    run_argv_for,
    source_filename,
)

_WARM_CODE: Dict[str, str] = {
    "python": 'print("warm")\n',
    "javascript": 'console.log("warm");\n',
    "typescript": 'const x: number = 1;\nconsole.log("warm", x);\n',
    "java": (
        "public class Warm {\n"
        '    public static void main(String[] a) {\n        System.out.println("warm");\n'
        "    }\n}\n"
    ),
    "c": '#include <stdio.h>\nint main(void) { printf("warm\\n"); return 0; }\n',
    "cpp": (
        "#include <iostream>\n"
        'int main() { std::cout << "warm" << std::endl; return 0; }\n'
    ),
    "go": 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("warm") }\n',
}

_STEP_TIMEOUT_S = 120


def _quiet_run(argv, cwd: str, timeout: int = _STEP_TIMEOUT_S) -> bool:
    try:
        cp = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=child_env(cwd),
        )
        return cp.returncode == 0
    except Exception:
        return False


def warm_toolchains() -> Dict[str, float]:
    """Warm every available toolchain. Returns {lang: seconds}. Never raises."""
    if os.environ.get("DEVIQ_WARMUP", "1") == "0":
        print("⏭️  Toolchain warm-up skipped (DEVIQ_WARMUP=0)")
        return {}
    timings: Dict[str, float] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="deviq-warmup-") as tmp:
            for lang, code in _WARM_CODE.items():
                spec = LANGS.get(lang)
                if spec is None or not have(*spec.tools):
                    continue
                t0 = time.perf_counter()
                try:
                    src = source_filename(lang, code, None)
                    with open(os.path.join(tmp, src), "w", encoding="utf-8") as f:
                        f.write(code)
                    ok = True
                    compile_argv = compile_argv_for(lang, src)
                    if compile_argv:
                        ok = _quiet_run(compile_argv, tmp)
                    if ok:
                        _quiet_run(
                            run_argv_for(lang, src, interactive=False), tmp)
                    timings[lang] = round(time.perf_counter() - t0, 2)
                except Exception:
                    continue
    except Exception as e:
        print(f"⚠️  Toolchain warm-up failed: {e}")
    if timings:
        total = round(sum(timings.values()), 1)
        detail = ", ".join(f"{k} {v}s" for k, v in sorted(timings.items()))
        print(f"🔥 Toolchain warm-up done in {total}s ({detail})")
    return timings


if __name__ == "__main__":
    sys.exit(0 if warm_toolchains() else 1)
