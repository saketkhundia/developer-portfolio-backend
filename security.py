"""
security.py — shared authentication, rate limiting, validation, and guardrails.

Design notes:
- Session tokens are opaque, high-entropy values. Only a SHA-256 hash is
  stored server-side (Mongo `sessions` collection), so a database read never
  exposes a live token. Identity is ALWAYS derived from the token — never
  from client-supplied email/uid headers or body fields.
- Password hashing uses stdlib PBKDF2-HMAC-SHA256 (no new dependencies).
- Rate limiting is in-memory fixed-window. Correct for a single instance;
  behind multiple instances it degrades to per-instance limits (documented).
- Auth tokens cannot move to HttpOnly cookies: the API is cross-origin
  (backend on onrender.com, frontends on deviq.online / github.io), where
  third-party cookies are blocked. Tokens live in frontend memory/storage
  with a 30-day expiry, revocation on logout, and XSS hardening via CSP.
"""

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# ─── Password hashing (PBKDF2-HMAC-SHA256, stdlib) ────────────────────────────

_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 210_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash a password. Returns `algo$iterations$salthex$dkhex` (no plaintext)."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time password check. False on any malformed input."""
    try:
        algo, iters, salthex, dkhex = (stored or "").split("$")
        if algo != _PBKDF2_ALGO:
            return False
        iterations = int(iters)
        if not (10_000 <= iterations <= 2_000_000):
            return False
        salt = bytes.fromhex(salthex)
        expected = bytes.fromhex(dkhex)
        if len(salt) != _SALT_BYTES or len(expected) != 32:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


def password_meets_policy(password: str) -> bool:
    """Signup policy (mirrors the frontend): 8+ chars, upper, digit."""
    if not password or len(password) < 8 or len(password) > 128:
        return False
    return bool(re.search(r"[A-Z]", password) and re.search(r"[0-9]", password))


# ─── Session tokens ───────────────────────────────────────────────────────────

SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days, revocable


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> Tuple[str, str]:
    """Returns (opaque token, sha256 hex). Only the hash is persisted."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


_db = None


def init_security(db_handle) -> None:
    """Wire the Mongo handle in (called once from main at startup)."""
    global _db
    _db = db_handle
    if db_handle is None:
        return
    try:
        db_handle["sessions"].create_index("token_hash", unique=True)
        db_handle["sessions"].create_index("expires_at", expireAfterSeconds=0)
    except Exception:
        pass  # index creation is best-effort; lookups still work


def mint_session(email: str, provider: str) -> str:
    """Create a session row and return the opaque token. Raises if no DB."""
    if _db is None:
        raise RuntimeError("session store unavailable")
    token, digest = new_session_token()
    now = time.time()
    _db["sessions"].insert_one({
        "token_hash": digest,
        "email": email,
        "provider": provider,
        "created_at": _utcnow_iso(),
        "expires_at": now + SESSION_TTL_SECONDS,
    })
    return token


def lookup_session_email(token: str) -> Optional[str]:
    """Resolve a bearer token to an email. None when missing/expired."""
    try:
        if _db is None or not token or len(token) > 256:
            return None
        row = _db["sessions"].find_one({"token_hash": hash_token(token)})
        if not row:
            return None
        if float(row.get("expires_at", 0)) < time.time():
            try:
                _db["sessions"].delete_one({"token_hash": hash_token(token)})
            except Exception:
                pass
            return None
        email = row.get("email", "")
        return email if isinstance(email, str) and email else None
    except Exception:
        return None


def revoke_session(token: str) -> None:
    try:
        if _db is None or not token:
            return
        _db["sessions"].delete_one({"token_hash": hash_token(token)})
    except Exception:
        pass


def revoke_user_sessions(email: str) -> None:
    try:
        if _db is None or not email:
            return
        _db["sessions"].delete_many({"email": email})
    except Exception:
        pass


async def current_user_email(
    authorization: Optional[str] = Header(None),
) -> str:
    """FastAPI dependency: the ONLY trusted source of user identity.

    Accepts `Authorization: Bearer <opaque-session-token>`. Anything else —
    including any client-supplied email/uid — is untrusted and ignored.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid authorization")
    email = lookup_session_email(authorization[len("Bearer "):].strip())
    if not email:
        raise HTTPException(401, "Invalid or expired session")
    return email


# ─── Rate limiting (in-memory fixed window) ───────────────────────────────────

_buckets: Dict[str, List[float]] = {}
_bucket_lock = threading.Lock()


def _window_ok(key: str, max_calls: int, window_s: int, now: float) -> Tuple[bool, float]:
    """Returns (allowed, retry_after_seconds)."""
    with _bucket_lock:
        hits = _buckets.get(key, [])
        hits = [t for t in hits if t > now - window_s]
        if len(hits) >= max_calls:
            retry = max(0.0, (hits[0] + window_s) - now)
            _buckets[key] = hits
            return False, retry
        hits.append(now)
        _buckets[key] = hits
        # Opportunistic memory hygiene.
        if len(_buckets) > 20000:
            _buckets.clear()
        return True, 0.0


def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate limiting (NOT for auth decisions)."""
    try:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first and len(first) < 64:
                return first
        if request.client and request.client.host:
            return request.client.host[:64]
    except Exception:
        pass
    return "unknown"


def enforce_rate_limit(
    request: Request,
    scope: str,
    max_calls: int,
    window_s: int,
    user: Optional[str] = None,
) -> None:
    """Apply global + per-IP (+ per-user when known) buckets. 429 on breach."""
    now = time.time()
    checks = [
        (f"rl:g:{scope}", max_calls * 20, window_s),
        (f"rl:ip:{scope}:{_client_ip(request)}", max_calls, window_s),
    ]
    if user:
        checks.append((f"rl:u:{scope}:{user}", max_calls, window_s))
    for key, limit, window in checks:
        ok, retry = _window_ok(key, limit, window, now)
        if not ok:
            raise HTTPException(
                429,
                "Too many requests, please slow down",
                headers={"Retry-After": str(int(retry) + 1)},
            )


# ─── Input validators ─────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_GITHUB_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,30}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def valid_email(value: object) -> str:
    """Normalize + validate an email address. Raises ValueError."""
    email = str(value or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise ValueError("Invalid email address")
    return email


def valid_github_username(value: object) -> str:
    name = str(value or "").strip()
    if not _GITHUB_USER_RE.match(name):
        raise ValueError("Invalid GitHub username")
    return name


def valid_platform_handle(platform: str, value: object) -> str:
    """Validate leetcode/codeforces handles (conservative charset + length)."""
    handle = str(value or "").strip()
    if not _HANDLE_RE.match(handle):
        raise ValueError(f"Invalid {platform} handle")
    return handle


def valid_owner_repo(value: object, what: str = "name") -> str:
    name = str(value or "").strip()
    if not _OWNER_REPO_RE.match(name) or len(name) > 100:
        raise ValueError(f"Invalid {what}")
    return name


def valid_slug(value: object) -> str:
    slug = str(value or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError("Invalid slug")
    return slug


def safe_url(value: object, *, allow_empty: bool = True, max_length: int = 2000) -> str:
    """Allow only http(s) URLs (blocks javascript:/data:/vbscript: and controls)."""
    url = str(value or "").strip()
    if not url:
        if allow_empty:
            return ""
        raise ValueError("URL is required")
    if len(url) > max_length or _CONTROL_CHARS_RE.search(url):
        raise ValueError("Invalid URL")
    lowered = url.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError("URL must use http(s)")
    return url


def valid_redirect_uri(value: object, allowed_hosts: List[str]) -> str:
    """OAuth redirect_uri must be https (or http+localhost) on an allowed host."""
    uri = str(value or "").strip()
    if len(uri) > 500:
        raise ValueError("redirect_uri too long")
    try:
        from urllib.parse import urlparse
        parts = urlparse(uri)
    except Exception:
        raise ValueError("Invalid redirect_uri")
    host = (parts.hostname or "").lower()
    if not host or host not in [h.lower() for h in allowed_hosts]:
        raise ValueError("redirect_uri host not allowed")
    if parts.scheme == "https":
        return uri
    if parts.scheme == "http" and host in ("localhost", "127.0.0.1"):
        return uri
    raise ValueError("redirect_uri must use https")


def capped_get(
    url: str,
    *,
    timeout: int = 15,
    max_bytes: int = 2_000_000,
    headers: Optional[Dict[str, str]] = None,
):
    """Streaming GET with a hard response-size cap (memory-DoS guard)."""
    import requests

    resp = requests.get(url, timeout=timeout, headers=headers or {}, stream=True)
    length = resp.headers.get("content-length")
    try:
        if length is not None and int(length) > max_bytes:
            resp.close()
            raise ValueError("Upstream response too large")
    except (TypeError, ValueError) as e:
        resp.close()
        if isinstance(e, ValueError) and str(e) == "Upstream response too large":
            raise
    chunks: List[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            resp.close()
            raise ValueError("Upstream response too large")
        chunks.append(chunk)
    resp.close()
    body = b"".join(chunks)
    resp._content = body  # let callers use .json()/.text normally
    return resp


# ─── Guard middlewares ────────────────────────────────────────────────────────

MAX_BODY_BYTES = 1_000_000  # 1 MB global request cap


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies early (413) via Content-Length."""

    async def dispatch(self, request: Request, call_next):
        try:
            length = request.headers.get("content-length")
            if length is not None and int(length) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
        except (TypeError, ValueError):
            pass
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers for a JSON API (no CSP: Swagger needs it
    when enabled, and JSON responses are not script-executed with nosniff)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        try:
            if request.url.scheme == "https":
                response.headers["Strict-Transport-Security"] = (
                    "max-age=31536000; includeSubDomains"
                )
        except Exception:
            pass
        return response


def log_server_error(context: str, exc: BaseException) -> None:
    """Server-side error log WITHOUT sensitive payloads (type only, no data)."""
    try:
        print(f"[ERROR] {context}: {type(exc).__name__}")
    except Exception:
        pass
