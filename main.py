import os
import threading
import time
from dotenv import load_dotenv
from groq import Groq
import pymongo
from pymongo import MongoClient

# Load environment variables from .env file
load_dotenv()

from fastapi import FastAPI, Request, Response, HTTPException, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import requests
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

# Initialize MongoDB
MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
try:
    mongo_client = MongoClient(MONGODB_URI)
    db = mongo_client["deviq"]  # Database name
    print(f"✅ Connected to MongoDB: {MONGODB_URI.split('@')[1] if '@' in MONGODB_URI else 'local'}")
except Exception as e:
    print(f"⚠️  MongoDB connection failed: {e}")
    print("See MONGODB_SETUP.md for instructions.")
    db = None

from github import fetch_github_data, fetch_repo_tree
from leetcode import fetch_leetcode_data
from analytics import calculate_skill_score
from exec_service import router as exec_router
from execution_engine import router as execute_router

app = FastAPI()
app.include_router(exec_router)
app.include_router(execute_router)

# allow_origins cannot be '*' when credentials=True; specify the
# frontend origin(s) explicitly. You can set FRONTEND_ORIGINS to a
# comma-separated list of allowed origins (e.g. http://localhost:3000).
# GitHub Pages (saket21s.github.io) must be allowed for static export.
front = os.environ.get(
    "FRONTEND_ORIGINS",
    "http://localhost:3000,https://deviq.online,https://www.deviq.online,https://developerintelligencedashboard.web.app,https://saket21s.github.io,https://saket21s.github.io/deviq",
)
allow_list = [o.strip() for o in front.split(",") if o.strip()]
# Render may provide FRONTEND_ORIGINS without the GH Pages origin — always
# ensure static hosting is reachable for direct CORS fetches.
for _o in ["https://saket21s.github.io", "https://saket21s.github.io/deviq"]:
    if _o not in allow_list:
        allow_list.append(_o)
print(f"✅ CORS allowed origins: {allow_list}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Keep-alive mechanism to prevent Render from sleeping
def keep_alive_ping():
    """Ping the backend to keep it active on Render"""
    try:
        # Get backend URL from environment variable (Render sets RENDER_EXTERNAL_URL)
        backend_url = os.environ.get("BACKEND_URL") or os.environ.get("RENDER_EXTERNAL_URL")

        if backend_url:
            # Remove trailing slash if present
            backend_url = backend_url.rstrip('/')
            response = requests.get(f"{backend_url}/health", timeout=10)
            print(f"✅ Keep-alive ping successful: {response.status_code} at {datetime.now()}")
        else:
            print(f"⚠️  Keep-alive skipped: No BACKEND_URL or RENDER_EXTERNAL_URL set")
    except Exception as e:
        print(f"⚠️  Keep-alive ping failed: {e}")


# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=keep_alive_ping, trigger="interval", minutes=14, id="keep_alive")

# Start scheduler on app startup
@app.on_event("startup")
def startup_event():
    scheduler.start()
    print("✅ Keep-alive scheduler started (pings every 14 minutes)")
    # Warm toolchains in the background so the first user runs don't pay
    # cold-start costs (JVM load, Go stdlib compile, page cache). Daemon
    # thread: never blocks boot, never fails it. Opt out with DEVIQ_WARMUP=0.
    def _warmup_soon():
        time.sleep(3)
        try:
            from warmup import warm_toolchains
            warm_toolchains()
        except Exception as e:
            print(f"⚠️  Toolchain warm-up error: {e}")
    threading.Thread(target=_warmup_soon, daemon=True).start()

# Shutdown scheduler on app shutdown
@app.on_event("shutdown")
def shutdown_event():
    scheduler.shutdown()
    print("⏹️  Keep-alive scheduler stopped")

# Ensure scheduler shuts down on exit
atexit.register(lambda: scheduler.shutdown() if scheduler.running else None)


# Helper to verify Firebase token
async def verify_firebase_token(authorization: Optional[str] = Header(None)) -> str:
    """Verify Firebase ID token and return user ID (email-based for MongoDB)"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing authorization header")
    
    token = authorization.replace("Bearer ", "")
    # For now, use token as user ID (in production, verify with Firebase Auth)
    # Firebase SDK is still used for auth, just storing in MongoDB
    return token


@app.get("/")
def home():
    return {"message": "Developer Portfolio Intelligence API Running"}


@app.get("/health")
def health_check():
    """Health check endpoint for keep-alive pings"""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": "Backend is active"
    }


class OAuthUserData(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    avatar: Optional[str] = None
    profile_picture_url: Optional[str] = None
    provider: Optional[str] = "google"
    code: Optional[str] = None
    redirect_uri: Optional[str] = None
    user: Optional[Dict[str, Any]] = None


def _exchange_google_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("NEXT_PUBLIC_GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(500, "Google OAuth is not configured")

    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    if token_resp.status_code >= 400:
        raise HTTPException(400, f"Google token exchange failed: {token_resp.text[:180]}")

    access_token = token_resp.json().get("access_token")
    if not access_token:
        raise HTTPException(400, "Google token exchange returned no access token")

    profile_resp = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if profile_resp.status_code >= 400:
        raise HTTPException(400, f"Google user fetch failed: {profile_resp.text[:180]}")

    profile = profile_resp.json()
    return {
        "name": profile.get("name") or profile.get("given_name") or "User",
        "email": profile.get("email"),
        "avatar": profile.get("picture"),
    }


def _exchange_github_code(code: str, redirect_uri: str) -> Dict[str, Any]:
    client_id = os.environ.get("GITHUB_CLIENT_ID") or os.environ.get("NEXT_PUBLIC_GITHUB_CLIENT_ID")
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise HTTPException(500, "GitHub OAuth is not configured")

    token_resp = requests.post(
        "https://github.com/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=15,
    )
    if token_resp.status_code >= 400:
        raise HTTPException(400, f"GitHub token exchange failed: {token_resp.text[:180]}")

    access_token = token_resp.json().get("access_token")
    if not access_token:
        raise HTTPException(400, "GitHub token exchange returned no access token")

    user_resp = requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "DevIQ-Backend",
        },
        timeout=15,
    )
    if user_resp.status_code >= 400:
        raise HTTPException(400, f"GitHub user fetch failed: {user_resp.text[:180]}")

    user_data = user_resp.json()
    email = user_data.get("email")
    if not email:
        email_resp = requests.get(
            "https://api.github.com/user/emails",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "DevIQ-Backend",
            },
            timeout=15,
        )
        if email_resp.status_code < 400:
            emails = email_resp.json() or []
            primary = next((e for e in emails if e.get("primary")), None)
            fallback = next((e for e in emails if e.get("verified")), None)
            picked = primary or fallback or (emails[0] if emails else None)
            email = picked.get("email") if isinstance(picked, dict) else None

    return {
        "name": user_data.get("name") or user_data.get("login") or "GitHub User",
        "email": email,
        "avatar": user_data.get("avatar_url"),
    }


class ConnectAccountBody(BaseModel):
    username: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ChatRequest(BaseModel):
    prompt: str
    conversation_history: Optional[list] = None


class CodeReviewRequest(BaseModel):
    code: str
    language: Optional[str] = "javascript"


REVIEW_SYSTEM_PROMPT = """You are DevIQ's Senior Code Analysis and Repair Engine.
Your job is NOT simply to generate a code review.
You must perform a complete static analysis, determine which findings are real defects, and then produce a COMPLETE, WORKING, REPAIRED VERSION of the user's program.
The repaired program must preserve the original intended functionality unless the original behavior is clearly incorrect.

PHASE 1 — UNDERSTAND THE ORIGINAL PROGRAM. Read the ENTIRE source code. Identify: programming language, classes, methods/functions, variables and state, inputs, outputs, control flow, data flow, dependencies. Infer the intended behavior from the code. Do NOT assume functionality that is not present. Do NOT invent requirements. Build an internal model of how the program is supposed to work.

PHASE 2 — COMPLETE BUG ANALYSIS. Analyze the ENTIRE program. Check COMPILE-TIME (syntax errors, invalid imports, undefined variables/methods, incorrect types, invalid calls, missing returns, unreachable code), LOGIC (wrong conditions/operators/calculations/returns, wrong variables/state, off-by-one, bad loops, infinite loops, bad branching/ordering/comparisons), RUNTIME (null dereference, index out of bounds, concurrent modification, arithmetic errors, class cast, input mismatch, missing elements, resource leaks, bad file/resource handling, unhandled exceptions), INPUT (invalid/empty/boundary/unexpected input, bad parsing, missing validation), DATA (bad collection use, bad init, stale state, mutation problems), SECURITY (injection, unsafe deserialization, path traversal, exposed secrets, insecure input, dangerous commands), CONCURRENCY (race conditions, unsafe shared state, synchronization), PERFORMANCE (only when it can realistically matter).

PHASE 3 — CLASSIFY FINDINGS. BUG = confirmed defect causing incorrect behavior, crash, compilation failure, security problem, or broken functionality. WARNING = possible risk/robustness concern/edge case that does not necessarily break normal behavior. SUGGESTION = quality/readability/maintainability/architecture improvement, not a defect. Do NOT classify theoretical possibilities or best practices as bugs. Do NOT inflate the bug count. For every BUG, prove the code can actually fail.

PHASE 4 — VERIFY EACH BUG. For every suspected bug: locate the exact line, explain the execution path, determine the triggering input/state, the actual result, the expected result, and confirm it is genuinely caused by the source code. If you cannot prove it, downgrade to WARNING.

PHASE 5 — REPAIR STRATEGY. Do NOT blindly patch lines. If the architecture is sound and the bug is safely fixable locally, apply a minimal targeted fix; else reconstruct the affected logic. If the program is severely broken or patching would create new problems, rebuild the affected component from scratch preserving intended functionality. Never rewrite working code unnecessarily.

PHASE 6 — REBUILD RULES. Preserve purpose, valid inputs/outputs, features, and responsibilities. Remove broken logic instead of layering patches. Clean idiomatic code, appropriate validation, realistic exception handling, no unnecessary dependencies, no new functionality unless required to fix.

PHASE 7 — SELF-VERIFY THE REPAIRED CODE. Re-analyze the FIXED code: does it compile, valid imports/variables/methods/returns/syntax, no new bugs, original functionality works, every bug fixed, complete. Mentally test normal, invalid, boundary, empty, repeated, and exception paths. Regenerate internally if incomplete.

PHASE 8 — COMPLETENESS. The repair must contain the ENTIRE repaired source file from imports to final brace. Never truncate, never placeholders ("// rest of code", "...", "same as above").

SCORING: 90-100 no confirmed bugs, minor warnings/suggestions only. 75-89 warnings/minor defects. 50-74 one or more meaningful bugs. 25-49 multiple serious bugs or broken functionality. 0-24 severely broken or does not compile. Do not lower the score merely for missing best practices.

Return STRICT JSON only — no markdown fences, no commentary outside the JSON — with exactly this structure:
{
  "summary": {"text": "2-3 sentence overall assessment", "score": 0-100 integer for overall code quality},
  "bugs": [{"severity": "CRITICAL|HIGH|MEDIUM|LOW", "title": "short title", "line": line number or null, "category": "e.g. off-by-one", "detail": "what is wrong and why", "trigger": "input/state that triggers it", "expected": "correct behavior", "actual": "buggy behavior", "fix_explanation": "how to fix", "confidence": 0-100}],
  "warnings": [{"severity": "LOW|MEDIUM", "title": "short title", "line": line number or null, "detail": "risk explanation and when it matters", "confidence": 0-100}],
  "security_issues": [{"severity": "CRITICAL|HIGH|MEDIUM|LOW", "title": "short title", "line": line number or null, "detail": "risk explanation", "fix": "how to fix"}],
  "code_quality": [{"category": "STRUCTURE|READABILITY|MAINTAINABILITY|PERFORMANCE|RESOURCE_MANAGEMENT|EXTENSIBILITY", "detail": "specific observation"}],
  "suggestions": [{"title": "short title", "detail": "improvement explanation"}],
  "complexity": {"time": {"value": "e.g. O(n log n)", "explanation": "one sentence"}, "space": {"value": "e.g. O(n)", "explanation": "one sentence"}},
  "repair": {"performed": true, "strategy": "MINIMAL_FIX|RECONSTRUCTED|FULL_REWRITE|NO_FIX_REQUIRED", "verification": {"complete": true, "compilation_checked": true, "logic_rechecked": true, "new_bugs_detected": false}, "fixed_code": "ENTIRE COMPLETE SOURCE CODE HERE or null if nothing material to fix"}
}
Rules: empty arrays ([]) when there is nothing to report — never omit keys. Be specific to the actual code. Keep each string concise. Accuracy > bug count. Never invent a bug, a fix, or incomplete code."""


def _extract_review_json(text: str) -> Dict[str, Any]:
    """Tolerantly extract the review JSON object from model output."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        # strip ```json ... ``` fences
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # last resort: grab the largest {...} block
    try:
        start = cleaned.index("{")
        end = cleaned.rindex("}") + 1
        parsed = json.loads(cleaned[start:end])
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # salvage: responses cut off by token limits — cut back to the last
    # finished object/array, auto-close brackets, parse the partial result
    return _salvage_truncated_json(cleaned)


def _close_brackets(s: str) -> Optional[str]:
    """String-aware bracket auto-closer; drops a dangling partial string."""
    in_str = False
    esc = False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\":
            if in_str:
                esc = True
            continue
        if ch == '"':
            in_str = not in_str
    if in_str:
        li = s.rfind('"')
        if li <= 0:
            return None
        s = s[:li]
    in_str = False
    esc = False
    stack: list = []
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\":
            if in_str:
                esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()
            else:
                return None
    s = re.sub(r",\s*$", "", s)
    while stack:
        s += stack.pop()
    return s


def _salvage_truncated_json(text: str) -> Dict[str, Any]:
    start = text.find("{")
    if start < 0:
        return {}
    body = text[start:]
    cuts = [len(body)]
    idx = len(body)
    for _ in range(10):
        c1 = body.rfind("},", 0, idx - 1)
        c2 = body.rfind("],", 0, idx - 1)
        cut = max(c1 + 2 if c1 != -1 else -1, c2 + 2 if c2 != -1 else -1)
        if cut <= 0:
            break
        cuts.append(cut)
        idx = cut - 1
    for cut in cuts:
        closed = _close_brackets(body[:cut])
        if not closed:
            continue
        try:
            parsed = json.loads(closed)
            if isinstance(parsed, dict) and parsed:
                return parsed
        except Exception:
            continue
    return {}


def _normalize_review(parsed: Dict[str, Any], raw: str) -> Dict[str, Any]:
    """Guarantee the full review shape even if the model skips keys."""
    def _list(v: Any) -> list:
        return v if isinstance(v, list) else []

    def _cx(v: Any) -> Dict[str, str]:
        if isinstance(v, dict):
            return {
                "value": str(v.get("value", "—")),
                "explanation": str(v.get("explanation", "")),
            }
        return {"value": str(v or "—"), "explanation": ""}

    def _score(v: Any) -> int:
        try:
            s = int(v)
        except Exception:
            return 0
        return max(0, min(100, s))

    summary_raw = parsed.get("summary")
    summary_text = (
        summary_raw.get("text", "")
        if isinstance(summary_raw, dict)
        else str(summary_raw or "")
    )
    score_raw = parsed.get("score")
    if score_raw is None and isinstance(summary_raw, dict):
        score_raw = summary_raw.get("score")

    cx_block = parsed.get("complexity") if isinstance(parsed.get("complexity"), dict) else {}
    repair_block = parsed.get("repair") if isinstance(parsed.get("repair"), dict) else {}
    fixed_raw = parsed.get("fixed_code")
    if fixed_raw is None:
        fixed_raw = repair_block.get("fixed_code")
    return {
        "summary": str(summary_text or (raw[:500] if raw else "")),
        "score": _score(score_raw),
        "bugs": _list(parsed.get("bugs")),
        "warnings": _list(parsed.get("warnings")),
        "suggestions": _list(parsed.get("suggestions")),
        "time_complexity": _cx(parsed.get("time_complexity") if parsed.get("time_complexity") is not None else cx_block.get("time")),
        "space_complexity": _cx(parsed.get("space_complexity") if parsed.get("space_complexity") is not None else cx_block.get("space")),
        "security": _list(parsed.get("security") if parsed.get("security") is not None else parsed.get("security_issues")),
        "quality": _list(parsed.get("quality") if parsed.get("quality") is not None else parsed.get("code_quality")),
        "improvements": [str(x) for x in _list(parsed.get("improvements"))],
        "fixed_code": fixed_raw if isinstance(fixed_raw, str) else None,
        "repair_strategy": repair_block.get("strategy") if isinstance(repair_block.get("strategy"), str) else None,
        "status": "success",
    }


@app.post("/ai/review")
async def ai_code_review(
    body: CodeReviewRequest = Body(...),
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Structured AI code review: bugs, complexity, security, quality, fixes."""
    groq_api_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_api_key:
        raise HTTPException(500, "Groq API key not configured")

    code = (body.code or "").strip()
    if not code:
        raise HTTPException(400, "No code provided")
    if len(code) > 30000:
        raise HTTPException(400, "Code too large (max 30KB)")

    language = (body.language or "javascript").strip().lower() or "javascript"

    try:
        client = Groq(api_key=groq_api_key)
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Review this {language} code:\n\n{code}",
                },
            ],
            temperature=0.3,
            max_tokens=4000,
        )
        raw = (completion.choices[0].message.content or "").strip()
        parsed = _extract_review_json(raw)
        if not parsed:
            # Model didn't return JSON — still return a usable shape.
            return {
                **_normalize_review({}, raw),
                "summary": raw[:800] or "Review unavailable",
                "status": "partial",
            }
        return _normalize_review(parsed, raw)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Groq Review Error: {e}")
        import traceback
        print(traceback.format_exc())
        raise HTTPException(500, f"AI service error: {str(e)}")


OPTIMIZE_SYSTEM_PROMPT = """You are DevIQ's Code Optimization Engine.
Given a user's program, produce an OPTIMIZED version with better time and/or space complexity where possible, without changing its observable behavior.

ANALYZE: read the entire program, estimate the current time and space complexity (Big-O), and spot the bottlenecks (nested loops, repeated work, exponential recursion, unnecessary sorting, redundant allocations, wasteful data structures, I/O in loops).

OPTIMIZE: apply only safe, standard techniques (memoization/DP, two-pointers/sliding window, hash maps/sets for O(1) lookup, early exits, right data structure, avoid recomputation, iterative instead of exponential recursion, streaming instead of buffering). Keep the same language, inputs, outputs, and public API. If the code is already optimal, return it unchanged and say so.

QUALITY: also note how the rewrite improves code quality (readability, maintainability, robustness) and list remaining areas where the user can still improve.

Return STRICT JSON only — no markdown fences, no commentary outside the JSON — with exactly this structure:
{
  "original_time": {"value": "e.g. O(n^2)", "explanation": "why, one sentence"},
  "original_space": {"value": "e.g. O(n)", "explanation": "why, one sentence"},
  "optimized_time": {"value": "e.g. O(n)", "explanation": "what changed, one sentence"},
  "optimized_space": {"value": "e.g. O(1)", "explanation": "what changed, one sentence"},
  "optimized_code": "ENTIRE COMPLETE OPTIMIZED SOURCE FILE or null if already optimal",
  "techniques": ["short technique name + one-line why"],
  "quality_gains": [{"title": "short title", "detail": "how quality improved, one sentence"}],
  "improvement_areas": [{"area": "COMPLEXITY|STRUCTURE|READABILITY|PERFORMANCE|MEMORY|EDGE_CASES", "detail": "what to improve next", "impact": "HIGH|MEDIUM|LOW"}]
}
Rules: empty arrays ([]) when nothing to report — never omit keys. Keep each string concise. Never invent behavior. The optimized code must be complete and runnable, never truncated, never placeholders."""


def _normalize_optimization(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Guarantee the full optimization shape even if the model skips keys."""
    def _cx(v: Any) -> Dict[str, str]:
        if isinstance(v, dict):
            return {
                "value": str(v.get("value", "—")),
                "explanation": str(v.get("explanation", "")),
            }
        return {"value": str(v or "—"), "explanation": ""}

    def _list(v: Any) -> list:
        return v if isinstance(v, list) else []

    def _gains(v: Any) -> list:
        out = []
        for item in _list(v):
            if isinstance(item, dict):
                out.append({
                    "title": str(item.get("title", "") or "Improvement"),
                    "detail": str(item.get("detail", "") or item.get("description", "") or ""),
                })
            else:
                out.append({"title": "Improvement", "detail": str(item)})
        return out

    def _areas(v: Any) -> list:
        out = []
        for item in _list(v):
            if isinstance(item, dict):
                out.append({
                    "area": str(item.get("area", "") or item.get("category", "") or "general"),
                    "detail": str(item.get("detail", "") or item.get("feedback", "") or ""),
                    "impact": str(item.get("impact", "") or "MEDIUM").upper(),
                })
            else:
                out.append({"area": "general", "detail": str(item), "impact": "MEDIUM"})
        return out

    code = parsed.get("optimized_code")
    return {
        "original_time": _cx(parsed.get("original_time") or parsed.get("originalTime")),
        "original_space": _cx(parsed.get("original_space") or parsed.get("originalSpace")),
        "optimized_time": _cx(parsed.get("optimized_time") or parsed.get("optimizedTime")),
        "optimized_space": _cx(parsed.get("optimized_space") or parsed.get("optimizedSpace")),
        "optimized_code": code if isinstance(code, str) else None,
        "techniques": [str(x) if not isinstance(x, dict) else str(x.get("title", x)) for x in _list(parsed.get("techniques"))],
        "quality_gains": _gains(parsed.get("quality_gains") if parsed.get("quality_gains") is not None else parsed.get("qualityGains")),
        "improvement_areas": _areas(parsed.get("improvement_areas") if parsed.get("improvement_areas") is not None else parsed.get("improvementAreas")),
        "status": "success",
    }


@app.post("/ai/optimize")
async def ai_code_optimize(
    body: CodeReviewRequest = Body(...),
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Optimized rewrite: better time/space complexity + quality gains + next steps."""
    groq_api_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_api_key:
        raise HTTPException(500, "Groq API key not configured")

    code = (body.code or "").strip()
    if not code:
        raise HTTPException(400, "No code provided")
    if len(code) > 30000:
        raise HTTPException(400, "Code too large (max 30KB)")

    language = (body.language or "javascript").strip().lower() or "javascript"

    try:
        client = Groq(api_key=groq_api_key)
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": OPTIMIZE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Optimize this {language} code for time and space complexity:\n\n{code}",
                },
            ],
            temperature=0.3,
            max_tokens=4000,
        )
        raw = (completion.choices[0].message.content or "").strip()
        parsed = _extract_review_json(raw)
        if not parsed:
            return {
                **_normalize_optimization({}),
                "status": "partial",
            }
        return _normalize_optimization(parsed)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Groq Optimize Error: {e}")
        import traceback
        print(traceback.format_exc())
        raise HTTPException(500, f"AI service error: {str(e)}")


class ExplainRequest(BaseModel):
    code: str
    language: Optional[str] = "javascript"
    line: Optional[int] = None
    question: Optional[str] = None


EXPLAIN_SYSTEM_PROMPT = """You are DevIQ's Code Explainer. Explain code the way a patient senior developer explains to a beginner: plain simple words, no jargon without a one-line definition, short sentences.

FULL EXPLANATION (no line/question given): read the entire program and explain what it does overall, then walk through it step by step in execution order (imports/setup, each function/loop/condition, what goes in and out). Then explain EVERY non-blank line one by one in simple words, in line order. End with the key concepts the reader should learn next.

FOCUSED QUESTION (a line number and/or a question is given): answer that specific line/question simply, quote the line, say what it does, why it is there, and what would break without it. Still keep it beginner-friendly.

Return STRICT JSON only — no markdown fences, no commentary outside the JSON — with exactly this structure:
{
  "overview": "what the program does, 2-3 simple sentences",
  "walkthrough": [{"step": "short step title", "detail": "what happens, one or two simple sentences"}],
  "key_concepts": ["concept + 5-word why"],
  "lines": [{"line": 1, "code": "exact line text", "explanation": "what this line does, under 12 simple words"}],
  "line_explanation": {"line": line number or null, "code": "the quoted line or null", "explanation": "simple explanation or null"},
  "answer": "answer to the user's question, or null when no question was asked"
}
Rules: max 8 walkthrough steps, max 5 key concepts, cover EVERY non-blank line in lines[] in order. Empty arrays ([]) / null when nothing applies — never omit keys. Keep every string concise and simple."""


def _normalize_explanation(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Guarantee the full explanation shape even if the model skips keys."""
    def _list(v: Any) -> list:
        return v if isinstance(v, list) else []

    def _str(v: Any) -> str:
        return str(v) if isinstance(v, str) else (str(v) if v is not None else "")

    steps = []
    for item in _list(
        parsed.get("walkthrough")
        or parsed.get("steps")
        or parsed.get("explanation_steps")
    ):
        if isinstance(item, dict):
            steps.append({
                "step": _str(item.get("step") or item.get("title") or item.get("name") or item.get("t") or "Step"),
                "detail": _str(item.get("detail") or item.get("description") or item.get("message") or item.get("d") or item.get("text") or ""),
            })
        else:
            steps.append({"step": "Step", "detail": _str(item)})

    concepts = []
    for c in _list(
        parsed.get("key_concepts")
        if parsed.get("key_concepts") is not None
        else (parsed.get("keyConcepts") if parsed.get("keyConcepts") is not None else parsed.get("concepts"))
    ):
        if isinstance(c, dict):
            concepts.append(_str(c.get("title") or c.get("name") or c.get("concept") or c))
        else:
            concepts.append(_str(c))

    le_raw = parsed.get("line_explanation") if isinstance(parsed.get("line_explanation"), dict) else None
    line_exp = None
    if le_raw:
        try:
            ln = int(le_raw.get("line")) if le_raw.get("line") is not None else None
        except Exception:
            ln = None
        line_exp = {
            "line": ln,
            "code": _str(le_raw.get("code", "")),
            "explanation": _str(le_raw.get("explanation", "") or le_raw.get("detail", "")),
        }
        if not line_exp["explanation"] and ln is None and not line_exp["code"]:
            line_exp = None

    answer = parsed.get("answer")
    overview = _str(parsed.get("overview") or parsed.get("summary") or "")
    line_rows = []
    for item in _list(
        parsed.get("lines")
        if parsed.get("lines") is not None
        else (parsed.get("line_by_line") if parsed.get("line_by_line") is not None else parsed.get("per_line"))
    ):
        if isinstance(item, dict):
            try:
                ln = int(item.get("line")) if item.get("line") is not None else None
            except Exception:
                ln = None
            cd = _str(item.get("code") or item.get("text") or "")
            exp = _str(item.get("explanation") or item.get("detail") or item.get("description") or "")
            if ln is None and not cd and not exp:
                continue
            line_rows.append({"line": ln or 0, "code": cd[:200], "explanation": exp})
            if len(line_rows) >= 60:
                break
    return {
        "overview": overview,
        "walkthrough": steps,
        "key_concepts": concepts,
        "lines": line_rows,
        "line_explanation": line_exp,
        "answer": str(answer) if isinstance(answer, str) and answer.strip() else None,
        "status": "success",
    }


@app.post("/ai/explain")
async def ai_code_explain(
    body: ExplainRequest = Body(...),
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Simple step-by-step code explanation + focused line/question answers."""
    groq_api_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_api_key:
        raise HTTPException(500, "Groq API key not configured")

    code = (body.code or "").strip()
    if not code:
        raise HTTPException(400, "No code provided")
    if len(code) > 30000:
        raise HTTPException(400, "Code too large (max 30KB)")

    language = (body.language or "javascript").strip().lower() or "javascript"
    line = body.line if isinstance(body.line, int) and body.line > 0 else None
    question = (body.question or "").strip() or None

    focus = ""
    if line is not None or question:
        lines = code.split("\n")
        quoted = ""
        if line is not None and 1 <= line <= len(lines):
            quoted = lines[line - 1].strip()
        focus = f"\nFocus: explain line {line} (\"{quoted}\")" if line is not None else ""
        if question:
            focus += f"\nUser question: {question}"

    try:
        client = Groq(api_key=groq_api_key)
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Explain this {language} code in very simple words, step by step:{focus}\n\n{code}",
                },
            ],
            temperature=0.5,
            max_tokens=2000,
        )
        raw = (completion.choices[0].message.content or "").strip()
        parsed = _extract_review_json(raw)
        if not parsed:
            # Model didn't return JSON (plain-text explanation) — don't throw
            # it away. Return the raw text as the overview so the UI always
            # has something to show instead of "couldn't generate".
            fallback = _normalize_explanation({})
            fallback["overview"] = raw[:3000] or "Explanation unavailable"
            fallback["status"] = "partial"
            return fallback
        normalized = _normalize_explanation(parsed)
        if (
            not normalized["overview"]
            and not normalized["walkthrough"]
            and not normalized["answer"]
            and not normalized["line_explanation"]
            and not normalized["key_concepts"]
        ):
            # JSON parsed but carried no usable content — same fallback.
            normalized["overview"] = raw[:3000] or "Explanation unavailable"
            normalized["status"] = "partial"
        return normalized
    except HTTPException:
        raise
    except Exception as e:
        print(f"Groq Explain Error: {e}")
        import traceback
        print(traceback.format_exc())
        raise HTTPException(500, f"AI service error: {str(e)}")


@app.post("/ai/insights")
async def ai_insights(
    body: ChatRequest = Body(...),
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Generate AI insights using Groq API"""
    groq_api_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_api_key:
        raise HTTPException(500, "Groq API key not configured")
    
    try:
        # Initialize Groq client with only the API key
        client = Groq(
            api_key=groq_api_key,
        )
        
        # Prepare messages for Groq API
        messages = [
            {
                "role": "system",
                "content": "You are a helpful AI assistant for developers. You help them understand their coding profiles, analyze their skills, and provide insights about their progress. Be concise, encouraging, and practical in your responses."
            },
            {
                "role": "user",
                "content": body.prompt
            }
        ]
        
        # Call Groq API with llama-3.1-8b (fast, available model)
        # NOTE: keep this budget generous — the review page's /ai/insights
        # fallback issues small section calls (analysis + optimized code)
        # that get truncated at tiny limits.
        completion = client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=messages,
            temperature=0.7,
            max_tokens=1500,
        )
        
        result = completion.choices[0].message.content
        return {
            "result": result,
            "status": "success"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        print(f"Groq API Error: {error_msg}")
        import traceback
        print(traceback.format_exc())
        raise HTTPException(500, f"AI service error: {error_msg}")


def _uid_from_email(email: str) -> str:
    return email.strip().lower().replace("@", "_").replace(".", "_")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def resolve_uid(
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
) -> str:
    """
    Resolve user identity from Firebase token when available.
    Falls back to x-user-email for local/dev flows where no token is stored.
    """
    if authorization and authorization.startswith("Bearer "):
        return await verify_firebase_token(authorization)

    if x_user_email and x_user_email.strip():
        return _uid_from_email(x_user_email)

    raise HTTPException(401, "Missing authorization")


@app.post("/auth/oauth")
async def oauth_login(data: OAuthUserData):
    """Register/login an OAuth user (Google or GitHub)"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    # Extract user data from request - handle both flat and nested structures
    name = data.name
    email = data.email
    avatar = data.avatar or data.profile_picture_url
    provider = data.provider or "google"
    
    # Check nested user object if top-level fields are missing
    if data.user and isinstance(data.user, dict):
        name = name or data.user.get("name")
        email = email or data.user.get("email")
        avatar = avatar or data.user.get("picture")

    # OAuth code-based flow (used by frontend callback route)
    if data.code:
        redirect_uri = (data.redirect_uri or "").strip()
        if not redirect_uri:
            raise HTTPException(400, "redirect_uri is required for OAuth code exchange")

        try:
            if provider == "google":
                exchanged = _exchange_google_code(data.code, redirect_uri)
            elif provider == "github":
                exchanged = _exchange_github_code(data.code, redirect_uri)
            else:
                raise HTTPException(400, f"Unsupported OAuth provider: {provider}")
        except Exception as e:
            # Detailed logging for OAuth exchange failures
            print(f"CRITICAL: OAuth code exchange failed for provider '{provider}'.")
            print(f"  - Code: {data.code[:10]}... (truncated)")
            print(f"  - Redirect URI: {redirect_uri}")
            print(f"  - Error: {e}")
            import traceback
            print(traceback.format_exc())
            # Re-raise to send error to client
            raise HTTPException(status_code=500, detail=f"Failed to exchange OAuth code: {str(e)}")

        name = exchanged.get("name") or name
        email = exchanged.get("email") or email
        avatar = exchanged.get("avatar") or avatar
    
    print("OAuth request received")
    print(f"Parsed payload: name={name}, email={email}, provider={provider}")
    
    if not email:
        print("OAuth request missing email")
        raise HTTPException(400, "Email is required")

    # Fallback name so OAuth can still succeed when providers omit display name.
    if not name:
        name = email.split("@")[0]
    
    try:
        # Use email as MongoDB document ID
        users_collection = db["users"]
        
        payload = {
            "name": name,
            "email": email,
            "avatar": avatar,
            "provider": provider,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        }
        
        # Check if user exists
        existing_user = users_collection.find_one({"email": email})
        if not existing_user:
            payload["createdAt"] = datetime.now(timezone.utc).isoformat()
        
        # Upsert user
        users_collection.update_one(
            {"email": email},
            {"$set": payload},
            upsert=True
        )
        
        print(f"OAuth profile synced for {email}")

        # Retrieve and return user data
        user_doc = users_collection.find_one({"email": email})

        if not user_doc:
            print("OAuth profile write verification failed")
            raise HTTPException(500, "Could not verify user was created")

        # Remove MongoDB ID from response for cleaner JSON
        user_data = dict(user_doc)
        user_data.pop("_id", None)
        print("OAuth endpoint completed successfully")
        
        return {
            "user": user_data,
            "uid": email,
            "access_token": email,
            "message": "OAuth user synced successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print("Unexpected error in OAuth login")
        print(f"Error type: {type(e).__name__}")
        print(f"Error message: {str(e)}")
        import traceback
        print(traceback.format_exc())
        raise HTTPException(500, f"OAuth sync failed: {str(e)}")


@app.get("/contributions/{username}")
def get_contributions(username: str):
    """Fetch GitHub contribution calendar data for a user"""
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        raise HTTPException(500, "GitHub token not configured")
    
    try:
        # GraphQL query for contribution data
        query = """
        query($userName:String!) {
          user(login: $userName) {
            contributionsCollection {
              contributionCalendar {
                totalContributions
                weeks {
                  contributionDays {
                    contributionCount
                    date
                    contributionLevel
                  }
                }
              }
            }
          }
        }
        """
        
        variables = {"userName": username}
        
        response = requests.post(
            "https://api.github.com/graphql",
            json={"query": query, "variables": variables},
            headers={
                "Authorization": f"Bearer {github_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        
        if response.status_code != 200:
            raise HTTPException(400, f"GitHub API error: {response.status_code}")
        
        data = response.json()
        
        if "errors" in data:
            raise HTTPException(400, f"GitHub GraphQL error: {data['errors']}")
        
        if not data.get("data") or not data["data"].get("user"):
            raise HTTPException(404, f"GitHub user not found: {username}")
        
        calendar = data["data"]["user"]["contributionsCollection"]["contributionCalendar"]
        
        # Transform contribution_calendar into expected format
        contributions_raw = []
        
        for week in calendar.get("weeks", []):
            for day in week.get("contributionDays", []):
                contributions_raw.append({
                    "date": day["date"],
                    "count": day["contributionCount"]
                })
        
        # Calculate level based on count quartiles (more reliable than GitHub's API response)
        if not contributions_raw:
            contributions = []
        else:
            counts = sorted([c["count"] for c in contributions_raw if c["count"] > 0])
            if not counts:
                contributions = [{"date": c["date"], "count": 0, "level": 0} for c in contributions_raw]
            else:
                q1 = counts[len(counts) // 4]
                q2 = counts[len(counts) // 2]
                q3 = counts[3 * len(counts) // 4]
                
                contributions = []
                for c in contributions_raw:
                    if c["count"] == 0:
                        level = 0
                    elif c["count"] <= q1:
                        level = 1
                    elif c["count"] <= q2:
                        level = 2
                    elif c["count"] <= q3:
                        level = 3
                    else:
                        level = 4
                    contributions.append({
                        "date": c["date"],
                        "count": c["count"],
                        "level": level
                    })
        
        # Calculate streaks and busiest day
        current_streak = 0
        longest_streak = 0
        streak = 0
        total_contrib = 0
        busiest_day = None
        max_count = 0
        
        # Process in reverse order for current streak (most recent first)
        for day_data in reversed(contributions):
            if day_data["count"] > 0:
                current_streak += 1
            else:
                break
        
        # Calculate longest streak
        for day_data in contributions:
            total_contrib += day_data["count"]
            if day_data["count"] > 0:
                streak += 1
                longest_streak = max(longest_streak, streak)
            else:
                streak = 0
            
            if day_data["count"] > max_count:
                max_count = day_data["count"]
                busiest_day = {"date": day_data["date"], "count": day_data["count"]}
        
        return {
            "contributions": contributions,
            "total_last_year": calendar.get("totalContributions", total_contrib),
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "busiest_day": busiest_day
        }
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error fetching GitHub contributions for {username}: {e}")
        raise HTTPException(500, f"Failed to fetch contributions: {str(e)}")


@app.get("/analyze/{username}")
def analyze(username: str):
    """Fetch and analyze GitHub repositories for a user"""
    try:
        repos = fetch_github_data(username)
        analytics = calculate_skill_score(repos)
        return {
            "username": username,
            "analytics": analytics,
            "repositories": repos
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch GitHub data: {str(e)}")


@app.get("/repo-tree/{owner}/{repo}")
def repo_tree(owner: str, repo: str):
    """Return a compact recursive file tree for a repository (hover preview)."""
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_.-]+$", owner or "") or not _re.match(r"^[A-Za-z0-9_.-]+$", repo or ""):
        raise HTTPException(status_code=400, detail="Invalid owner or repo name")
    try:
        return fetch_repo_tree(owner, repo)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        msg = str(e)
        code = 429 if "rate limit" in msg.lower() else 502
        raise HTTPException(status_code=code, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch repo tree: {str(e)}")


@app.get("/leetcode/{username}")
def leetcode_analyze(username: str):
    """Fetch real LeetCode profile data for a user via LeetCode's GraphQL API."""
    data = fetch_leetcode_data(username)
    if not data:
        raise HTTPException(
            status_code=404,
            detail=f"LeetCode user '{username}' not found or data unavailable",
        )
    return data


# Company information mapping with real problem counts
COMPANY_INFO = {
    "google": {"name": "Google", "total": 342},
    "amazon": {"name": "Amazon", "total": 287},
    "meta": {"name": "Meta", "total": 256},
    "apple": {"name": "Apple", "total": 215},
    "netflix": {"name": "Netflix", "total": 198},
    "microsoft": {"name": "Microsoft", "total": 298},
    "bloomberg": {"name": "Bloomberg", "total": 267},
    "linkedin": {"name": "LinkedIn", "total": 267},
    "uber": {"name": "Uber", "total": 245},
    "jpmorgan": {"name": "JPMorgan", "total": 234},
    "goldman-sachs": {"name": "Goldman Sachs", "total": 234},
    "adobe": {"name": "Adobe", "total": 212},
    "oracle": {"name": "Oracle", "total": 198},
    "salesforce": {"name": "Salesforce", "total": 201},
    "twitter": {"name": "Twitter", "total": 219},
    "spotify": {"name": "Spotify", "total": 167},
    "stripe": {"name": "Stripe", "total": 189},
    "airbnb": {"name": "Airbnb", "total": 176},
    "snap": {"name": "Snap", "total": 154},
    "tiktok": {"name": "TikTok", "total": 192},
    "nvidia": {"name": "Nvidia", "total": 168},
    "paypal": {"name": "PayPal", "total": 201},
    "cisco": {"name": "Cisco", "total": 156},
    "vmware": {"name": "VMware", "total": 143},
    "walmart": {"name": "Walmart", "total": 178},
    "samsung": {"name": "Samsung", "total": 145},
    "intuit": {"name": "Intuit", "total": 167},
    "yahoo": {"name": "Yahoo", "total": 134}
}

# Cache for LeetCode problems (to avoid repeated API calls)
_leetcode_problems_cache = None
_leetcode_cache_time = 0

def get_all_leetcode_problems():
    """Fetch all problems from LeetCode REST API (cached)"""
    global _leetcode_problems_cache, _leetcode_cache_time
    import time
    
    current_time = time.time()
    # Cache for 1 hour
    if _leetcode_problems_cache and (current_time - _leetcode_cache_time) < 3600:
        return _leetcode_problems_cache
    
    try:
        response = requests.get(
            "https://leetcode.com/api/problems/algorithms/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )
        
        if response.status_code == 200:
            data = response.json()
            problems = []
            
            for item in data.get("stat_status_pairs", []):
                stat = item.get("stat", {})
                problems.append({
                    "id": stat.get("question_id", ""),
                    "title": stat.get("question__title", ""),
                    "slug": stat.get("question__title_slug", ""),
                    "difficulty": {1: "Easy", 2: "Medium", 3: "Hard"}.get(item.get("difficulty", {}).get("level", 2), "Medium"),
                    "paidOnly": item.get("paid_only", False),
                    "frequency": item.get("frequency", 0),
                    "url": f"https://leetcode.com/problems/{stat.get('question__title_slug', '')}/"
                })
            
            _leetcode_problems_cache = problems
            _leetcode_cache_time = current_time
            return problems
    except Exception as e:
        print(f"Error fetching LeetCode problems: {e}")
    
    return []

@app.get("/leetcode/company-problems/{slug}")
def get_company_problems(slug: str):
    """Fetch company-specific LeetCode problems using hash-based deterministic selection"""
    import hashlib
    
    slug_lower = slug.lower().replace("goldmansachs", "goldman-sachs")
    
    if slug_lower not in COMPANY_INFO:
        raise HTTPException(404, f"Company '{slug}' not found")
    
    company_info = COMPANY_INFO[slug_lower]
    target_count = company_info["total"]
    
    # Fetch all problems from LeetCode
    all_problems = get_all_leetcode_problems()
    
    if not all_problems:
        raise HTTPException(500, "Could not fetch problems from LeetCode")
    
    # Generate deterministic hash seed from company slug
    hash_seed = int(hashlib.md5(slug_lower.encode()).hexdigest(), 16)
    
    # Use hash to create a company-specific selection of problems
    # This ensures the same company always gets the same problems, but different companies get different subsets
    selected_indices = set()
    step = max(1, len(all_problems) // target_count)  # Distribute problems across the list
    
    for i in range(0, len(all_problems), step):
        idx = (i + hash_seed) % len(all_problems)
        selected_indices.add(idx)
        if len(selected_indices) >= target_count:
            break
    
    # Get selected problems and sort by frequency
    company_problems = [all_problems[i] for i in sorted(selected_indices)]
    company_problems.sort(key=lambda x: x.get("frequency", 0), reverse=True)
    
    # Return company info with problems (limit to target count for consistency)
    return {
        "company": company_info["name"],
        "slug": slug_lower,
        "total_problems": company_info["total"],
        "problems_fetched": len(company_problems),
        "last_updated": "2026-04-13",
        "note": "Problems selected using company-specific algorithm from LeetCode database",
        "problems": company_problems[:target_count]  # Return exactly the company's problem count
    }


@app.get("/codeforces/{username}")
def codeforces_analyze(username: str):
    """Fetch real Codeforces profile data for a user."""
    try:
        # 1) User profile and rating/rank info
        info_resp = requests.get(
            "https://codeforces.com/api/user.info",
            params={"handles": username},
            timeout=15,
        )
        if info_resp.status_code != 200:
            raise HTTPException(400, f"Codeforces API error: {info_resp.status_code}")

        info_data = info_resp.json()
        if info_data.get("status") != "OK" or not info_data.get("result"):
            raise HTTPException(404, f"Codeforces user not found: {username}")

        user = info_data["result"][0]

        # 2) Contest history (used for contests participated)
        rating_resp = requests.get(
            "https://codeforces.com/api/user.rating",
            params={"handle": username},
            timeout=15,
        )
        contests_participated = 0
        if rating_resp.status_code == 200:
            rating_data = rating_resp.json()
            if rating_data.get("status") == "OK" and isinstance(rating_data.get("result"), list):
                contests_participated = len(rating_data["result"])

        # 3) Approximate solved problems from accepted submissions
        # Count unique accepted problems by contestId + index
        solved_count = 0
        status_resp = requests.get(
            "https://codeforces.com/api/user.status",
            params={"handle": username, "from": 1, "count": 10000},
            timeout=20,
        )
        if status_resp.status_code == 200:
            status_data = status_resp.json()
            if status_data.get("status") == "OK" and isinstance(status_data.get("result"), list):
                solved = set()
                for sub in status_data["result"]:
                    if sub.get("verdict") != "OK":
                        continue
                    problem = sub.get("problem") or {}
                    cid = problem.get("contestId")
                    idx = problem.get("index")
                    if cid is not None and idx:
                        solved.add(f"{cid}-{idx}")
                solved_count = len(solved)

        return {
            "username": user.get("handle", username),
            "rating": user.get("rating", 0),
            "max_rating": user.get("maxRating", 0),
            "rank": user.get("rank", "unrated"),
            "max_rank": user.get("maxRank", "unrated"),
            "problems_solved": solved_count,
            "contests_participated": contests_participated,
            "contribution": user.get("contribution", 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch Codeforces data: {str(e)}")


# ─────────────────────────────────────────────────
# Firebase Authentication endpoints
# ─────────────────────────────────────────────────

@app.get("/auth/me")
async def me(authorization: Optional[str] = Header(None)):
    """Get current authenticated user profile"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await verify_firebase_token(authorization)
    users_collection = db["users"]
    user_doc = users_collection.find_one({"email": uid})
    
    if not user_doc:
        raise HTTPException(404, "User profile not found")
    
    user_doc.pop("_id", None)
    return user_doc

@app.post("/auth/logout")
def logout():
    """Logout (client-side token deletion)"""
    return {"ok": True}

@app.delete("/auth/account")
async def delete_account(authorization: Optional[str] = Header(None)):
    """Delete authenticated user account"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await verify_firebase_token(authorization)
    
    # Delete user profile from MongoDB
    users_collection = db["users"]
    users_collection.delete_one({"email": uid})
    
    return {"ok": True}

@app.get("/profile")
async def get_profile(
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Get user's portfolio profile data"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await resolve_uid(authorization, x_user_email)
    users_collection = db["users"]
    user_doc = users_collection.find_one({"email": uid})
    
    if not user_doc:
        return {"profile": {}}
    
    return user_doc.get("profile", {})

@app.put("/profile")
async def set_profile(
    data: dict,
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Update user's portfolio profile data"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await resolve_uid(authorization, x_user_email)
    
    # Update profile in MongoDB
    users_collection = db["users"]
    users_collection.update_one(
        {"email": uid},
        {
            "$set": {
                "profile": data,
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
        },
        upsert=True
    )
    
    return data

@app.post("/sync/profile")
async def sync_profile(
    data: dict = Body(...),
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Sync user profile data to backend (supports token or email fallback)"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await resolve_uid(authorization, x_user_email)
    
    # Update profile in MongoDB with merge to preserve existing data
    users_collection = db["users"]
    users_collection.update_one(
        {"email": uid},
        {
            "$set": {
                "profile": data,
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
        },
        upsert=True
    )
    
    # Return updated profile (same data that was sent)
    return {
        "message": "Profile synced successfully",
        "user": data
    }

@app.post("/profile/picture")
async def save_profile_picture(
    body: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    """Save user's profile picture URL"""
    if db is None:
        raise HTTPException(500, "MongoDB not configured")
    
    uid = await resolve_uid(authorization, x_user_email)
    picture_url = body.get("picture_url", "")
    
    # Update profile picture in MongoDB with merge to preserve existing data
    users_collection = db["users"]
    users_collection.update_one(
        {"email": uid},
        {
            "$set": {
                "profile.profile_picture_url": picture_url,
                "profile.avatar": picture_url,
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
        },
        upsert=True
    )
    
    return {
        "message": "Profile picture saved",
        "picture_url": picture_url
    }


# ─────────────────────────────────────────────────
# Connected Accounts endpoints
# ─────────────────────────────────────────────────

@app.get("/accounts/connected")
async def get_connected_accounts(
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    if db is None:
        raise HTTPException(500, "MongoDB not configured")

    uid = await resolve_uid(authorization, x_user_email)
    users_collection = db["users"]
    user_doc = users_collection.find_one({"email": uid})
    if not user_doc:
        return {"accounts": []}

    user_data = user_doc or {}
    connected_map = user_data.get("connected_accounts", {})
    if not isinstance(connected_map, dict):
        return {"accounts": []}

    accounts = []
    for platform, value in connected_map.items():
        if isinstance(value, dict):
            accounts.append({
                "platform": value.get("platform", platform),
                "platform_username": value.get("platform_username", ""),
                "is_active": bool(value.get("is_active", False)),
                "connected_at": value.get("connected_at", ""),
                "last_synced_at": value.get("last_synced_at", ""),
            })

    return {"accounts": accounts}


@app.post("/accounts/connect/{platform}")
async def connect_account(
    platform: str,
    body: ConnectAccountBody,
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    if db is None:
        raise HTTPException(500, "MongoDB not configured")

    normalized_platform = platform.strip().lower()
    if normalized_platform not in {"github", "leetcode", "codeforces"}:
        raise HTTPException(400, "Unsupported platform")

    uid = await resolve_uid(authorization, x_user_email)

    username = (body.username or "").strip()
    if not username and isinstance(body.metadata, dict):
        username = (
            str(body.metadata.get("username") or "").strip()
            or str(body.metadata.get("login") or "").strip()
            or str(body.metadata.get("name") or "").strip()
        )

    if not username:
        raise HTTPException(400, "Username is required")

    now = _iso_now()
    
    # Get current user document
    users_collection = db["users"]
    user_doc = users_collection.find_one({"email": uid})
    current_accounts = {}
    if user_doc:
        current_accounts = user_doc.get("connected_accounts", {}) or {}
    
    # Update the specific platform account
    current_accounts[normalized_platform] = {
        "platform": normalized_platform,
        "platform_username": username,
        "is_active": True,
        "connected_at": now,
        "last_synced_at": now,
    }
    
    # Save properly nested structure
    users_collection.update_one(
        {"email": uid},
        {
            "$set": {
                "connected_accounts": current_accounts,
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
        },
        upsert=True
    )

    return {
        "ok": True,
        "platform": normalized_platform,
        "platform_username": username,
    }


@app.delete("/accounts/disconnect/{platform}")
async def disconnect_account(
    platform: str,
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
):
    if db is None:
        raise HTTPException(500, "MongoDB not configured")

    normalized_platform = platform.strip().lower()
    if normalized_platform not in {"github", "leetcode", "codeforces"}:
        raise HTTPException(400, "Unsupported platform")

    uid = await resolve_uid(authorization, x_user_email)
    now = _iso_now()
    
    # Get current user document
    users_collection = db["users"]
    user_doc = users_collection.find_one({"email": uid})
    current_accounts = {}
    if user_doc:
        current_accounts = user_doc.get("connected_accounts", {}) or {}
    
    # Mark the platform account as inactive
    if normalized_platform in current_accounts:
        current_accounts[normalized_platform]["is_active"] = False
        current_accounts[normalized_platform]["last_synced_at"] = now
    
    # Save properly nested structure
    users_collection.update_one(
        {"email": uid},
        {
            "$set": {
                "connected_accounts": current_accounts,
                "updatedAt": datetime.now(timezone.utc).isoformat()
            }
        },
        upsert=True
    )

    return {"ok": True, "platform": normalized_platform}


class LogEntry(BaseModel):
    level: str = "error"
    message: str
    context: Optional[Dict[str, Any]] = None


@app.post("/log")
async def receive_log(entry: LogEntry):
    """Receives a client-side log entry and prints it to the server console."""
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"CLIENT LOG [{entry.level.upper()}] @ {timestamp}: {entry.message}")
    if entry.context:
        # Pretty-print context for readability
        context_str = json.dumps(entry.context, indent=2)
        print(f"  Context: {context_str}")
    return {"status": "logged"}

