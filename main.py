import asyncio
import os
import traceback
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Cookie, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import jwt

import database as db


def _missing_feature(name: str, err: Exception):
    def _raiser(*args, **kwargs):
        raise RuntimeError(f"{name} unavailable: {err}")
    return _raiser


try:
    from cache import get_cached_data, set_cached_data, invalidate_cache, get_cache_stats
except ImportError as e:
    print(f"IMPORT ERROR (cache): {e}")

    def get_cached_data(*args, **kwargs):
        return None

    def set_cached_data(_prefix, _key, data):
        return {"etag": "", "data": data}

    def invalidate_cache(*args, **kwargs):
        return False

    def get_cache_stats():
        return {"enabled": False, "reason": "cache dependency missing"}

try:
    from github import fetch_github_data
except ImportError as e:
    print(f"IMPORT ERROR (github): {e}")
    fetch_github_data = _missing_feature("github", e)

try:
    from analytics import calculate_skill_score
except ImportError as e:
    print(f"IMPORT ERROR (analytics): {e}")
    calculate_skill_score = _missing_feature("analytics", e)

try:
    from leetcode import fetch_leetcode_data
except ImportError as e:
    print(f"IMPORT ERROR (leetcode): {e}")
    fetch_leetcode_data = _missing_feature("leetcode", e)

try:
    from codeforces import fetch_codeforces_data
except ImportError as e:
    print(f"IMPORT ERROR (codeforces): {e}")
    fetch_codeforces_data = _missing_feature("codeforces", e)

try:
    from contributions import fetch_contributions
except ImportError as e:
    print(f"IMPORT ERROR (contributions): {e}")
    fetch_contributions = _missing_feature("contributions", e)

app = FastAPI()

# ============ CORS CONFIGURATION ============
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://deviq-bay.vercel.app", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["Content-Type"],
    max_age=3600,
)

# ============ CONFIGURATION ============
SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
TOKEN_EXPIRY_HOURS = 24

# Create default test user if not exists
try:
    if not db.get_user_by_username("testuser"):
        db.create_user("testuser", "test@example.com", "hashed_password")
        print("✓ Created default test user")
except Exception as e:
    print(f"Database init warning: {e}")

# ============ AUTH HELPERS ============
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None, device_info: Optional[dict] = None):
    """Create a JWT token and persist session with device tracking"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRY_HOURS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    
    # Persist session in database with device info
    try:
        user_id = data.get("user_id")
        if user_id:
            db.create_session(user_id, encoded_jwt, expire.isoformat(), device_info)
    except Exception as e:
        print(f"Session creation error: {e}")
    
    return encoded_jwt

def verify_token(token: str):
    """Verify JWT token and check database session"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        
        # Check if session exists and is active in database
        session = db.get_session(token)
        if not session or session.get('is_active') == 0:
            return None
        
        # Check if session expired
        expires_at = datetime.fromisoformat(session['expires_at'])
        if datetime.utcnow() > expires_at:
            db.delete_session(token)
            return None
        
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def get_token_from_request(request: Request) -> Optional[str]:
    """Extract token from Authorization header or cookies"""
    # Try Authorization header first
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:]
    
    # Try cookies
    token = request.cookies.get("access_token")
    return token

def check_etag(request: Request, cached_etag: str) -> bool:
    """Check if client's ETag matches current data"""
    client_etag = request.headers.get("If-None-Match")
    return client_etag == cached_etag

def add_cache_headers(response: Response, etag: str, max_age: int = 300):
    """Add caching headers to response"""
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = f"public, max-age={max_age}, must-revalidate"
    response.headers["Last-Modified"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

def get_device_info(request: Request) -> dict:
    """Extract device information from request"""
    user_agent = request.headers.get("User-Agent", "")
    
    # Simple device detection
    device_type = "Desktop"
    if "mobile" in user_agent.lower():
        device_type = "Mobile"
    elif "tablet" in user_agent.lower():
        device_type = "Tablet"
    
    # Extract device name from user agent
    device_name = "Unknown Device"
    if "Windows" in user_agent:
        device_name = "Windows PC"
    elif "Mac" in user_agent:
        device_name = "Mac"
    elif "Linux" in user_agent:
        device_name = "Linux PC"
    elif "iPhone" in user_agent:
        device_name = "iPhone"
    elif "iPad" in user_agent:
        device_name = "iPad"
    elif "Android" in user_agent:
        device_name = "Android Device"
    
    return {
        "device_name": device_name,
        "device_type": device_type,
        "ip_address": request.client.host if request.client else "",
        "user_agent": user_agent[:200]  # Truncate to avoid too long strings
    }

def _make_username_seed(value: str, fallback: str = "user") -> str:
    seed = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in (value or fallback)).strip("_")
    return seed or fallback

def _ensure_unique_username(base_seed: str) -> str:
    candidate = _make_username_seed(base_seed)
    if not db.get_user_by_username(candidate):
        return candidate

    for i in range(1, 1000):
        next_candidate = f"{candidate}_{i}"
        if not db.get_user_by_username(next_candidate):
            return next_candidate

    return f"{candidate}_{int(datetime.utcnow().timestamp())}"

# ============ ENDPOINTS ============

@app.get("/")
async def home():
    """Health check endpoint"""
    return {
        "status": "online",
        "message": "Developer Intelligence API",
        "version": "1.0.0"
    }

# ============ AUTH ENDPOINTS ============

@app.post("/auth/signup")
async def auth_signup(request: Request, response: Response):
    """Signup endpoint - accepts name/email/password and creates a user session"""
    try:
        body = await request.json()
        name = (body.get("name") or "").strip()
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        remember_me = body.get("remember_me", False)
        profile_picture_url = (
            body.get("profile_picture_url") or
            body.get("avatar") or
            body.get("picture") or
            body.get("photo") or
            body.get("avatar_url") or
            ""
        )

        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="Valid email is required")
        if not password:
            raise HTTPException(status_code=400, detail="Password is required")

        existing = db.get_user_by_email(email)
        if existing:
            raise HTTPException(status_code=409, detail="Email already exists")

        base_seed = name or email.split("@")[0]
        unique_username = _ensure_unique_username(base_seed)
        user_id = db.create_user(unique_username, email, password)
        if not user_id:
            raise HTTPException(status_code=500, detail="Failed to create user")

        if profile_picture_url:
            db.update_user(user_id, profile_picture_url=profile_picture_url)

        user = db.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=500, detail="Failed to load created user")

        device_info = get_device_info(request)
        expiry = timedelta(days=30) if remember_me else None
        token = create_access_token(
            {"sub": user["username"], "user_id": user["id"], "provider": "email"},
            expires_delta=expiry,
            device_info=device_info
        )

        db.add_profile_history(user["id"], "signup", {
            "timestamp": datetime.utcnow().isoformat(),
            "device": device_info["device_name"],
            "ip": device_info["ip_address"]
        })

        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=2592000 if remember_me else 86400,
            path="/"
        )

        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": 2592000 if remember_me else 86400,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "name": user["username"],
                "email": user["email"],
                "profile_picture_url": user.get("profile_picture_url", ""),
                "avatar": user.get("profile_picture_url", "")
            },
            "device": device_info["device_name"]
        }
    except HTTPException:
        raise
    except Exception:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Signup failed")

@app.post("/auth/login")
async def auth_login(request: Request, response: Response):
    """
    Login endpoint - accepts username and password
    Returns access token and tracks device
    """
    try:
        body = await request.json()
        username = (body.get("username") or "").strip()
        email = (body.get("email") or "").strip().lower()
        password = body.get("password")
        remember_me = body.get("remember_me", False)
        identifier = username or email
        
        if not identifier or not password:
            raise HTTPException(status_code=400, detail="Username/email and password required")
        
        # Get user from database
        user = None
        if username:
            user = db.get_user_by_username(username)
        if not user and email:
            user = db.get_user_by_email(email)
        if not user and "@" in identifier:
            user = db.get_user_by_email(identifier.lower())

        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        # Validate password (use proper hashing in production)
        if user["password"] != password:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        # Get device info
        device_info = get_device_info(request)
        
        # Create token with longer expiry if remember_me
        expiry = timedelta(days=30) if remember_me else None
        token = create_access_token(
            {"sub": user["username"], "user_id": user["id"], "provider": "email"},
            expires_delta=expiry,
            device_info=device_info
        )
        
        # Add to profile history
        db.add_profile_history(user["id"], "login", {
            "timestamp": datetime.utcnow().isoformat(),
            "device": device_info["device_name"],
            "ip": device_info["ip_address"]
        })
        
        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=2592000 if remember_me else 86400,
            path="/"
        )

        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": 2592000 if remember_me else 86400,  # seconds
            "user": {
                "id": user["id"],
                "username": user["username"],
                "name": user["username"],
                "email": user["email"],
                "profile_picture_url": user.get("profile_picture_url", ""),
                "avatar": user.get("profile_picture_url", "")
            },
            "device": device_info["device_name"]
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Login failed")

@app.get("/auth/me")
async def auth_me(request: Request, response: Response):
    """
    Get current authenticated user with caching
    Requires valid JWT token
    """
    try:
        token = get_token_from_request(request)
        
        if not token:
            raise HTTPException(status_code=401, detail="No token provided")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        
        user_id = payload.get("user_id")
        
        # Check cache first
        cached = get_cached_data("profile", str(user_id))
        if cached:
            if check_etag(request, cached["etag"]):
                return Response(status_code=304)
            
            add_cache_headers(response, cached["etag"], max_age=60)
            return cached["data"]
        
        # Get from database
        user = db.get_user_by_id(user_id)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        user_data = {
            "id": user["id"],
            "username": user["username"],
            "name": user["username"],
            "email": user["email"],
            "bio": user.get("bio", ""),
            "github_username": user.get("github_username", ""),
            "leetcode_username": user.get("leetcode_username", ""),
            "codeforces_handle": user.get("codeforces_handle", ""),
            "profile_picture_url": user.get("profile_picture_url", ""),
            "avatar": user.get("profile_picture_url", ""),
            "last_updated": datetime.utcnow().isoformat()
        }
        
        # Cache the result
        cache_entry = set_cached_data("profile", str(user_id), user_data)
        add_cache_headers(response, cache_entry["etag"], max_age=60)
        
        return user_data
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to get user info")

@app.post("/auth/oauth")
async def auth_oauth(request: Request, response: Response):
    """
    OAuth callback endpoint
    Handles OAuth provider responses
    """
    try:
        body = await request.json()
        print(f"[DEBUG] OAuth body received: {body}")
        
        provider = body.get("provider") or "google"  # default for gmail/google login
        code = body.get("code")
        access_token = body.get("access_token")
        id_token = body.get("id_token")
        oauth_token = body.get("token")
        user_obj = body.get("user") or body.get("profile") or body.get("result") or {}
        username = body.get("username") or user_obj.get("username") or user_obj.get("name")
        email = body.get("email") or user_obj.get("email")
        
        print(f"[DEBUG] user_obj: {user_obj}")

        oauth_credential = code or access_token or id_token or oauth_token
        if not oauth_credential and not username and not email:
            raise HTTPException(
                status_code=400,
                detail="Provide one of: code, access_token, id_token, token, username, or email"
            )

        # Placeholder OAuth user mapping (until provider verification is implemented)
        base_username = username or (email.split("@")[0] if email else None) or f"{provider}_user"
        safe_username = _make_username_seed(base_username, f"{provider}_user")

        user_email = (email or f"{safe_username}@{provider}.oauth.local").strip().lower()

        user = db.get_user_by_email(user_email)
        # Avoid linking to a different account via username when we already have an email identity.
        if not user and not email:
            user = db.get_user_by_username(safe_username)
        profile_picture_url = (body.get("profile_picture_url") or 
                              user_obj.get("picture") or 
                              user_obj.get("profile_picture_url") or
                              user_obj.get("photoURL") or
                              user_obj.get("photo") or
                              user_obj.get("avatar_url") or
                              user_obj.get("image_url") or
                              "")
        
        print(f"[DEBUG] Extracted profile_picture_url: {profile_picture_url}")
        
        if not user:
            unique_username = _ensure_unique_username(safe_username)
            user_id = db.create_user(unique_username, user_email, "oauth_user")
            if not user_id:
                raise HTTPException(status_code=500, detail="Failed to create OAuth user")
            user = db.get_user_by_id(user_id)
        
        # Update profile picture if provided from Google/OAuth
        if profile_picture_url:
            db.update_user(user["id"], profile_picture_url=profile_picture_url)
            user = db.get_user_by_id(user["id"])

        device_info = get_device_info(request)
        token = create_access_token(
            {"sub": user["username"], "user_id": user["id"], "provider": provider},
            device_info=device_info
        )

        db.add_profile_history(user["id"], "oauth_login", {
            "provider": provider,
            "timestamp": datetime.utcnow().isoformat(),
            "device": device_info["device_name"]
        })

        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=86400,
            path="/"
        )

        return {
            "access_token": token,
            "token_type": "bearer",
            "provider": provider,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "name": user["username"],
                "email": user["email"],
                "profile_picture_url": user.get("profile_picture_url", ""),
                "avatar": user.get("profile_picture_url", "")
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="OAuth failed")

@app.post("/auth/gmail/login")
async def auth_gmail_login(request: Request, response: Response):
    """
    Gmail-based cloud sync login.
    Accepts email (+ optional name) and returns a profile bound to that email across devices.
    """
    try:
        body = await request.json()
        print(f"[DEBUG] Gmail login body received: {body}")
        
        email = (body.get("email") or "").strip().lower()
        name = (body.get("name") or "").strip()
        remember_me = body.get("remember_me", True)

        if not email:
            raise HTTPException(status_code=400, detail="email is required")
        if "@" not in email:
            raise HTTPException(status_code=400, detail="Invalid email")

        # Keep it Gmail-focused as requested
        if not email.endswith("@gmail.com"):
            raise HTTPException(status_code=400, detail="Use a Gmail address")

        existing = db.get_user_by_email(email)
        profile_picture_url = (body.get("profile_picture_url") or 
                              body.get("picture") or
                              body.get("photoURL") or
                              body.get("photo") or
                              body.get("avatar_url") or
                              body.get("image_url") or
                              "")
        
        print(f"[DEBUG] Gmail extracted profile_picture_url: {profile_picture_url}")

        
        if existing:
            user = existing
            if profile_picture_url:
                db.update_user(user["id"], profile_picture_url=profile_picture_url)
                user = db.get_user_by_id(user["id"])
        else:
            base_seed = name or email.split("@")[0]
            unique_username = _ensure_unique_username(base_seed)
            user_id = db.create_user(unique_username, email, "gmail_user")
            if not user_id:
                raise HTTPException(status_code=500, detail="Failed to create Gmail user")
            user = db.get_user_by_id(user_id)
            if profile_picture_url:
                db.update_user(user["id"], profile_picture_url=profile_picture_url)
                user = db.get_user_by_id(user["id"])

        device_info = get_device_info(request)
        expiry = timedelta(days=30) if remember_me else timedelta(hours=TOKEN_EXPIRY_HOURS)
        token = create_access_token(
            {"sub": user["username"], "user_id": user["id"], "provider": "gmail"},
            expires_delta=expiry,
            device_info=device_info
        )

        db.add_profile_history(user["id"], "gmail_login", {
            "timestamp": datetime.utcnow().isoformat(),
            "device": device_info["device_name"],
            "email": email
        })

        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=2592000 if remember_me else 86400,
            path="/"
        )

        return {
            "access_token": token,
            "token_type": "bearer",
            "provider": "gmail",
            "expires_in": 2592000 if remember_me else 86400,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "name": user["username"],
                "email": user["email"],
                "profile_picture_url": user.get("profile_picture_url", ""),
                "avatar": user.get("profile_picture_url", "")
            }
        }
    except HTTPException:
        raise
    except Exception:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Gmail login failed")

@app.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    """
    Logout endpoint
    Invalidates session in database
    """
    try:
        token = get_token_from_request(request)
        if not token:
            raise HTTPException(status_code=400, detail="No token provided")
        
        # Get user info before deleting session
        payload = verify_token(token)
        if payload:
            user_id = payload.get("user_id")
            if user_id:
                db.add_profile_history(user_id, "logout", {"timestamp": datetime.utcnow().isoformat()})
        
        # Delete session from database
        db.delete_session(token)
        
        response.delete_cookie(key="access_token", path="/")

        return {
            "status": "success",
            "message": "Logged out successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Logout failed")

@app.get("/auth/sessions")
async def get_user_sessions_endpoint(request: Request):
    """Get all active sessions (devices) for current user"""
    try:
        token = get_token_from_request(request)
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_id = payload.get("user_id")
        sessions = db.get_user_sessions(user_id)
        
        return {
            "status": "success",
            "sessions": sessions,
            "count": len(sessions)
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to get sessions")

@app.post("/auth/logout-all")
async def logout_all_devices(request: Request):
    """Logout from all devices"""
    try:
        token = get_token_from_request(request)
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_id = payload.get("user_id")
        
        # Get body to check if we should keep current session
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
        keep_current = body.get("keep_current_session", False)
        
        # Delete all sessions (optionally except current)
        deleted_count = db.delete_all_user_sessions(user_id, token if keep_current else None)
        
        # Add to history
        db.add_profile_history(user_id, "logout_all_devices", {
            "timestamp": datetime.utcnow().isoformat(),
            "kept_current": keep_current,
            "deleted_sessions": deleted_count
        })
        
        return {
            "status": "success",
            "message": f"Logged out from {deleted_count} device(s)",
            "devices_logged_out": deleted_count
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to logout from all devices")

# ============ PROFILE ENDPOINTS ============

@app.get("/profile")
async def get_profile(request: Request, response: Response):
    """Get authenticated user's profile with caching and history"""
    try:
        token = get_token_from_request(request)
        
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_id = payload.get("user_id")
        
        # Check cache first
        cached = get_cached_data("profile", str(user_id))
        if cached:
            if check_etag(request, cached["etag"]):
                return Response(status_code=304)
            
            add_cache_headers(response, cached["etag"], max_age=60)
            return cached["data"]
        
        # Get from database
        user = db.get_user_by_id(user_id)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        print(f"[LOAD] User {user['email']} (ID: {user_id}) loading profile")
        
        profile_data = {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "bio": user.get("bio", ""),
            "github_username": user.get("github_username", ""),
            "leetcode_username": user.get("leetcode_username", ""),
            "codeforces_handle": user.get("codeforces_handle", ""),
            "profile_picture_url": user.get("profile_picture_url", ""),
            "created_at": user["created_at"],
            "updated_at": user["updated_at"],
            "last_fetched": datetime.utcnow().isoformat()
        }
        
        # Get stored profile data (analysis history, following, etc.)
        latest_profile = db.get_latest_user_profile(user_id)
        if latest_profile:
            stored_data = latest_profile.get('data', {})
            print(f"[LOAD] Found stored profile: recentAnalyses={len(stored_data.get('recentAnalyses', []))}, "
                  f"following={len(stored_data.get('following', []))}, "
                  f"notifications={len(stored_data.get('notifications', []))}")
            # Merge stored profile data with current user data
            if 'recentAnalyses' in stored_data:
                profile_data['recentAnalyses'] = stored_data['recentAnalyses']
            if 'following' in stored_data:
                profile_data['following'] = stored_data['following']
            if 'notifications' in stored_data:
                profile_data['notifications'] = stored_data['notifications']
            if 'analysesRun' in stored_data:
                profile_data['analysesRun'] = stored_data['analysesRun']
            if 'displayName' in stored_data:
                profile_data['displayName'] = stored_data['displayName']
            if 'joinedAt' in stored_data:
                profile_data['joinedAt'] = stored_data['joinedAt']
            if 'avatar' in stored_data:
                profile_data['avatar'] = stored_data['avatar']
            if 'solvedProblems' in stored_data:
                profile_data['solvedProblems'] = stored_data['solvedProblems']
            if 'weakCategories' in stored_data:
                profile_data['weakCategories'] = stored_data['weakCategories']
            if 'lastPracticeProblem' in stored_data:
                profile_data['lastPracticeProblem'] = stored_data['lastPracticeProblem']
        else:
            print(f"[LOAD] No stored profile found for user {user_id}")
        
        print(f"[LOAD] Returning profile with {len(profile_data.get('recentAnalyses', []))} analyses")
        
        # Cache the result
        cache_entry = set_cached_data("profile", str(user_id), profile_data)
        add_cache_headers(response, cache_entry["etag"], max_age=60)
        
        return profile_data
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to get profile")

@app.post("/sync/profile")
async def sync_profile(request: Request, response: Response):
    """Sync complete profile data to cloud including analysis results"""
    try:
        token = get_token_from_request(request)
        
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_id = payload.get("user_id")
        user = db.get_user_by_id(user_id)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        body = await request.json()
        
        print(f"[SYNC] User {user['email']} (ID: {user_id}) syncing profile")
        print(f"[SYNC] Received data: recentAnalyses={len(body.get('recentAnalyses', []))}, "
              f"following={len(body.get('following', []))}, "
              f"notifications={len(body.get('notifications', []))}")
        
        # Update basic user info in database
        update_data = {}
        if "email" in body:
            update_data["email"] = body["email"]
        if "bio" in body:
            update_data["bio"] = body.get("bio", "")
        if "github_username" in body:
            update_data["github_username"] = body.get("github_username", "")
        if "leetcode_username" in body:
            update_data["leetcode_username"] = body.get("leetcode_username", "")
        if "codeforces_handle" in body:
            update_data["codeforces_handle"] = body.get("codeforces_handle", "")
        if "profile_picture_url" in body:
            update_data["profile_picture_url"] = body.get("profile_picture_url", "")
        
        if update_data:
            success = db.update_user(user_id, **update_data)
            if not success:
                raise HTTPException(status_code=500, detail="Failed to sync profile")
        
        # Store complete profile data (analysis history, following, notifications, etc.)
        profile_snapshot = {
            'recentAnalyses': body.get('recentAnalyses', []),
            'following': body.get('following', []),
            'notifications': body.get('notifications', []),
            'analysesRun': body.get('analysesRun', 0),
            'displayName': body.get('displayName', ''),
            'joinedAt': body.get('joinedAt', ''),
            'avatar': body.get('avatar', ''),
            'solvedProblems': body.get('solvedProblems', []),
            'weakCategories': body.get('weakCategories', []),
            'lastPracticeProblem': body.get('lastPracticeProblem')
        }
        
        # Save profile snapshot
        snapshot_id = db.save_user_profile(user_id, profile_snapshot)
        print(f"[SYNC] Saved profile snapshot ID: {snapshot_id} with {len(profile_snapshot['recentAnalyses'])} analyses")
        
        # Add to profile history
        db.add_profile_history(user_id, "profile_sync", {"analysesCount": len(profile_snapshot['recentAnalyses'])})
        
        # Invalidate cache
        invalidate_cache("profile", str(user_id))
        
        # Get updated user
        updated_user = db.get_user_by_id(user_id)
        
        # Build response with merged data
        response_data = {
            "id": updated_user["id"],
            "username": updated_user["username"],
            "email": updated_user["email"],
            "bio": updated_user.get("bio", ""),
            "github_username": updated_user.get("github_username", ""),
            "leetcode_username": updated_user.get("leetcode_username", ""),
            "codeforces_handle": updated_user.get("codeforces_handle", ""),
            "profile_picture_url": updated_user.get("profile_picture_url", "")
        }
        
        # Merge profile snapshot data
        response_data.update(profile_snapshot)
        
        return {
            "message": "Profile synced to cloud",
            "user": response_data
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to sync profile")

@app.put("/profile")
async def update_profile(request: Request):
    """Update user profile"""
    try:
        token = get_token_from_request(request)
        
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_id = payload.get("user_id")
        user = db.get_user_by_id(user_id)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        body = await request.json()
        
        # Update user in database
        update_data = {}
        if "email" in body:
            update_data["email"] = body["email"]
        if "bio" in body:
            update_data["bio"] = body.get("bio", "")
        if "github_username" in body:
            update_data["github_username"] = body.get("github_username", "")
        if "leetcode_username" in body:
            update_data["leetcode_username"] = body.get("leetcode_username", "")
        if "codeforces_handle" in body:
            update_data["codeforces_handle"] = body.get("codeforces_handle", "")
        if "profile_picture_url" in body:
            update_data["profile_picture_url"] = body.get("profile_picture_url", "")
        
        success = db.update_user(user_id, **update_data)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update profile")
        
        # Add to profile history
        db.add_profile_history(user_id, "profile_update", update_data)
        
        # Invalidate cache
        invalidate_cache("profile", str(user_id))
        
        # Get updated user
        updated_user = db.get_user_by_id(user_id)
        
        return {
            "message": "Profile updated",
            "user": {
                "id": updated_user["id"],
                "username": updated_user["username"],
                "email": updated_user["email"],
                "bio": updated_user.get("bio", ""),
                "github_username": updated_user.get("github_username", ""),
                "leetcode_username": updated_user.get("leetcode_username", ""),
                "codeforces_handle": updated_user.get("codeforces_handle", ""),
                "profile_picture_url": updated_user.get("profile_picture_url", "")
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to update profile")

@app.get("/profile/history")
async def get_profile_history_endpoint(request: Request, limit: int = 50):
    """Get user's profile history"""
    try:
        token = get_token_from_request(request)
        
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_id = payload.get("user_id")
        
        # Get profile history
        history = db.get_profile_history(user_id, limit)
        
        return {
            "status": "success",
            "history": history,
            "count": len(history)
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to get profile history")

@app.get("/public/profile/{username}")
async def get_public_profile(username: str, response: Response):
    """Get public profile for a user (no auth required)"""
    try:
        user = db.get_user_by_username(username)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Check cache first
        cached = get_cached_data("public_profile", username)
        if cached:
            add_cache_headers(response, cached["etag"], max_age=300)
            return cached["data"]
        
        profile_data = {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "bio": user.get("bio", ""),
            "github_username": user.get("github_username", ""),
            "leetcode_username": user.get("leetcode_username", ""),
            "codeforces_handle": user.get("codeforces_handle", ""),
            "profile_picture_url": user.get("profile_picture_url", ""),
            "created_at": user["created_at"],
            "updated_at": user["updated_at"]
        }
        
        # Cache the result
        cache_entry = set_cached_data("public_profile", username, profile_data)
        add_cache_headers(response, cache_entry["etag"], max_age=300)
        
        return profile_data
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to get public profile")

@app.post("/profile/picture")
async def upload_profile_picture(request: Request):
    """Upload user's profile picture URL"""
    try:
        token = get_token_from_request(request)
        
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_id = payload.get("user_id")
        
        body = await request.json()
        picture_url = body.get("picture_url", "")
        
        if not picture_url:
            raise HTTPException(status_code=400, detail="picture_url is required")
        
        # Update user's profile picture URL
        success = db.update_user(user_id, profile_picture_url=picture_url)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update profile picture")
        
        # Invalidate cache
        user = db.get_user_by_id(user_id)
        invalidate_cache("public_profile", user["username"])
        invalidate_cache("profile", str(user_id))
        
        return {
            "message": "Profile picture updated",
            "picture_url": picture_url
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to upload profile picture")

@app.get("/profile/snapshots")
async def get_profile_snapshots(request: Request, limit: int = 10):
    """Get user's profile data snapshots over time"""
    try:
        token = get_token_from_request(request)
        
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user_id = payload.get("user_id")
        
        # Get profile snapshots
        snapshots = db.get_user_profile_history(user_id, limit)
        
        return {
            "status": "success",
            "snapshots": snapshots,
            "count": len(snapshots)
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to get profile snapshots")

# ============ DEVELOPER DATA ENDPOINTS ============

@app.get("/analyze/{username}")
async def analyze(username: str, request: Request, response: Response):
    """Analyze developer's GitHub data with caching"""
    try:
        # Check cache first
        cached = get_cached_data("github", username)
        
        if cached:
            # Check if client has current version
            if check_etag(request, cached["etag"]):
                response.status_code = 304
                return Response(status_code=304)
            
            # Return cached data with headers
            add_cache_headers(response, cached["etag"], max_age=300)
            return cached["data"]
        
        # Fetch fresh data
        repos = fetch_github_data(username)
        analytics = calculate_skill_score(repos)
        result = {
            "username": username,
            "analytics": analytics,
            "repositories": repos,
            "last_updated": datetime.utcnow().isoformat()
        }
        
        # Cache the result
        cache_entry = set_cached_data("github", username, result)
        add_cache_headers(response, cache_entry["etag"], max_age=300)
        
        return result
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/leetcode/{username}")
async def leetcode(username: str, request: Request, response: Response):
    """Get LeetCode stats with caching"""
    try:
        # Check cache first
        cached = get_cached_data("leetcode", username)
        
        if cached:
            if check_etag(request, cached["etag"]):
                response.status_code = 304
                return Response(status_code=304)
            
            add_cache_headers(response, cached["etag"], max_age=600)
            return cached["data"]
        
        # Fetch fresh data
        import inspect
        if inspect.iscoroutinefunction(fetch_leetcode_data):
            data = await fetch_leetcode_data(username)
        else:
            data = fetch_leetcode_data(username)
        
        if isinstance(data, dict) and data.get("error"):
            raise HTTPException(status_code=404, detail=data.get("error"))
        
        # Add timestamp
        data["last_updated"] = datetime.utcnow().isoformat()
        
        # Cache the result
        cache_entry = set_cached_data("leetcode", username, data)
        add_cache_headers(response, cache_entry["etag"], max_age=600)
        
        return data
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/codeforces/{handle}")
async def codeforces(handle: str, request: Request, response: Response):
    """Get CodeForces stats with caching"""
    try:
        # Check cache first
        cached = get_cached_data("codeforces", handle)
        
        if cached:
            if check_etag(request, cached["etag"]):
                response.status_code = 304
                return Response(status_code=304)
            
            add_cache_headers(response, cached["etag"], max_age=600)
            return cached["data"]
        
        # Fetch fresh data
        import inspect
        if inspect.iscoroutinefunction(fetch_codeforces_data):
            data = await fetch_codeforces_data(handle)
        else:
            data = fetch_codeforces_data(handle)
        
        if isinstance(data, dict) and data.get("error"):
            raise HTTPException(status_code=404, detail=data.get("error"))
        
        # Add timestamp
        data["last_updated"] = datetime.utcnow().isoformat()
        
        # Cache the result
        cache_entry = set_cached_data("codeforces", handle, data)
        add_cache_headers(response, cache_entry["etag"], max_age=600)
        
        return data
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/contributions/{username}")
async def contributions(username: str, request: Request, response: Response):
    """Get GitHub contributions with caching"""
    try:
        # Check cache first
        cached = get_cached_data("contributions", username)
        
        if cached:
            if check_etag(request, cached["etag"]):
                response.status_code = 304
                return Response(status_code=304)
            
            add_cache_headers(response, cached["etag"], max_age=300)
            return cached["data"]
        
        # Fetch fresh data
        data = await fetch_contributions(username)
        
        if data.get("error"):
            raise HTTPException(status_code=404, detail=data.get("error"))
        
        # Add timestamp
        data["last_updated"] = datetime.utcnow().isoformat()
        
        # Cache the result
        cache_entry = set_cached_data("contributions", username, data)
        add_cache_headers(response, cache_entry["etag"], max_age=300)
        
        return data
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

# ============ CACHE MANAGEMENT ENDPOINTS ============

@app.post("/cache/invalidate/{prefix}/{identifier}")
async def invalidate_user_cache(prefix: str, identifier: str, request: Request):
    """Invalidate specific cached data (requires auth)"""
    try:
        token = get_token_from_request(request)
        if not token or not verify_token(token):
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        success = invalidate_cache(prefix, identifier)
        return {
            "status": "success" if success else "not_found",
            "message": f"Cache invalidated for {prefix}:{identifier}" if success else "Cache entry not found"
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cache/stats")
async def cache_statistics(request: Request):
    """Get cache statistics"""
    try:
        stats = get_cache_stats()
        return {
            "status": "success",
            "cache": stats,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

# ============ ERROR HANDLERS ============

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom HTTP exception handler"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail}
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """General exception handler"""
    print(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"}
    )

# ============ OPTIONS HANDLER ============
@app.options("/{full_path:path}")
async def preflight_handler(full_path: str):
    """Handle CORS preflight requests"""
    return JSONResponse(
        status_code=200,
        content={}
    )