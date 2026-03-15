from leetcode import fetch_company_tagged_questions
import asyncio
import os
import traceback
from datetime import datetime, timedelta
from typing import Optional
import httpx

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
    allow_origins=["https://deviq-bay.vercel.app", "http://localhost:3000", "https://deviq.online", "https://saket21s.github.io"],
    allow_origin_regex=r"https://.*\.vercel\.app|http://localhost(:\d+)?",
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
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

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
        print(f"[TOKEN] JWT decoded successfully, user_id: {payload.get('user_id')}")
        
        # Check if session exists and is active in database
        session = db.get_session(token)
        if not session:
            print(f"[TOKEN] Session not found in database")
            return None
        if session.get('is_active') == 0:
            print(f"[TOKEN] Session is inactive")
            return None
        
        # Check if session expired
        expires_at = datetime.fromisoformat(session['expires_at'])
        if datetime.utcnow() > expires_at:
            print(f"[TOKEN] Session expired at {expires_at}")
            db.delete_session(token)
            return None
        
        print(f"[TOKEN] ✓ Valid token for user {payload.get('user_id')}")
        return payload
    except jwt.ExpiredSignatureError:
        print(f"[TOKEN] JWT signature expired")
        return None
    except jwt.InvalidTokenError as e:
        print(f"[TOKEN] Invalid JWT: {e}")
        return None
    except Exception as e:
        print(f"[TOKEN] Verification error: {e}")
        return None

def get_token_from_request(request: Request) -> Optional[str]:
    """Extract token from Authorization header or cookies"""
    # Try Authorization header first
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        print(f"[TOKEN] Found in Authorization header: {token[:20]}...")
        return token
    
    # Try cookies
    token = request.cookies.get("access_token")
    if token:
        print(f"[TOKEN] Found in cookie: {token[:20]}...")
    else:
        print(f"[TOKEN] No token found - headers: {list(request.headers.keys())[:5]}, cookies: {list(request.cookies.keys())}")
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
            samesite="none",
            secure=True,
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
            samesite="none",
            secure=True,
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
            "email": user["email"],
            "bio": user.get("bio", ""),
            "github_username": user.get("github_username", ""),
            "leetcode_username": user.get("leetcode_username", ""),
            "codeforces_handle": user.get("codeforces_handle", ""),
            "profile_picture_url": user.get("profile_picture_url", ""),
            "avatar": user.get("profile_picture_url", ""),
            "last_updated": datetime.utcnow().isoformat()
        }
        
        # Include displayName from profile snapshot (the user's chosen name)
        latest_profile = db.get_latest_user_profile(user_id)
        if latest_profile:
            stored = latest_profile.get('data', {})
            display_name = stored.get('displayName', '')
            if display_name:
                user_data["name"] = display_name
                user_data["displayName"] = display_name
            else:
                user_data["name"] = user["username"]
        else:
            user_data["name"] = user["username"]
        
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
    Handles OAuth provider responses and exchanges authorization codes
    """
    try:
        body = await request.json()
        print(f"[DEBUG] OAuth body received: {body}")
        
        provider = body.get("provider") or "google"
        code = body.get("code")
        redirect_uri = body.get("redirect_uri")
        
        # If code is provided, exchange it for user data
        if code:
            print(f"[DEBUG] Exchanging OAuth code for {provider}")
            
            if provider == "github":
                if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
                    raise HTTPException(status_code=500, detail="GitHub OAuth not configured")
                
                async with httpx.AsyncClient() as client:
                    # Exchange code for access token
                    token_resp = await client.post(
                        "https://github.com/login/oauth/access_token",
                        headers={"Accept": "application/json"},
                        data={
                            "client_id": GITHUB_CLIENT_ID,
                            "client_secret": GITHUB_CLIENT_SECRET,
                            "code": code,
                            "redirect_uri": redirect_uri or "",
                        }
                    )
                    token_data = token_resp.json()
                    print(f"[DEBUG] GitHub token exchange response: {token_data}")
                    access_token = token_data.get("access_token")
                    
                    if not access_token:
                        error_desc = token_data.get("error_description", token_data.get("error", "Unknown error"))
                        print(f"[ERROR] GitHub token exchange failed: {error_desc}")
                        raise HTTPException(status_code=400, detail=f"Failed to get GitHub access token: {error_desc}")
                    
                    # Fetch user info
                    user_resp = await client.get(
                        "https://api.github.com/user",
                        headers={"Authorization": f"Bearer {access_token}"}
                    )
                    user_data = user_resp.json()
                    
                    username = user_data.get("login")
                    email = user_data.get("email")
                    
                    # If email is not public, fetch from /user/emails endpoint
                    if not email:
                        try:
                            emails_resp = await client.get(
                                "https://api.github.com/user/emails",
                                headers={"Authorization": f"Bearer {access_token}"}
                            )
                            if emails_resp.status_code == 200:
                                emails_data = emails_resp.json()
                                # Prefer the primary verified email
                                for e in emails_data:
                                    if e.get("primary") and e.get("verified"):
                                        email = e["email"]
                                        break
                                # Fallback to any verified email
                                if not email:
                                    for e in emails_data:
                                        if e.get("verified"):
                                            email = e["email"]
                                            break
                        except Exception as ex:
                            print(f"[WARN] Could not fetch GitHub emails: {ex}")
                    
                    email = email or ""
                    profile_picture_url = user_data.get("avatar_url", "")
                    
            elif provider == "google":
                if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
                    raise HTTPException(status_code=500, detail="Google OAuth not configured")
                
                async with httpx.AsyncClient() as client:
                    # Exchange code for tokens
                    token_resp = await client.post(
                        "https://oauth2.googleapis.com/token",
                        data={
                            "client_id": GOOGLE_CLIENT_ID,
                            "client_secret": GOOGLE_CLIENT_SECRET,
                            "code": code,
                            "grant_type": "authorization_code",
                            "redirect_uri": redirect_uri or "postmessage",
                        }
                    )
                    token_data = token_resp.json()
                    access_token = token_data.get("access_token")
                    
                    if not access_token:
                        raise HTTPException(status_code=400, detail="Failed to get Google access token")
                    
                    # Fetch user info
                    user_resp = await client.get(
                        "https://www.googleapis.com/oauth2/v2/userinfo",
                        headers={"Authorization": f"Bearer {access_token}"}
                    )
                    user_data = user_resp.json()
                    
                    username = user_data.get("name") or user_data.get("email", "").split("@")[0]
                    email = user_data.get("email")
                    profile_picture_url = user_data.get("picture", "")
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")
        else:
            # Fallback to old behavior if code not provided
            access_token = body.get("access_token")
            id_token = body.get("id_token")
            oauth_token = body.get("token")
            user_obj = body.get("user") or body.get("profile") or body.get("result") or {}
            username = body.get("username") or user_obj.get("username") or user_obj.get("name")
            email = body.get("email") or user_obj.get("email")
            profile_picture_url = (body.get("profile_picture_url") or 
                                  user_obj.get("picture") or 
                                  user_obj.get("profile_picture_url") or
                                  user_obj.get("photoURL") or
                                  user_obj.get("photo") or
                                  user_obj.get("avatar_url") or
                                  user_obj.get("image_url") or
                                  "")
            
            oauth_credential = access_token or id_token or oauth_token
            if not oauth_credential and not username and not email:
                raise HTTPException(
                    status_code=400,
                    detail="Provide one of: code, access_token, id_token, token, username, or email"
                )
        
        print(f"[DEBUG] OAuth user: {username}, {email}")

        # Create or get user
        base_username = username or (email.split("@")[0] if email else None) or f"{provider}_user"
        safe_username = _make_username_seed(base_username, f"{provider}_user")
        user_email = (email or f"{safe_username}@{provider}.oauth.local").strip().lower()

        user = db.get_user_by_email(user_email)
        if not user and not email:
            user = db.get_user_by_username(safe_username)
        
        if not user:
            unique_username = _ensure_unique_username(safe_username)
            user_id = db.create_user(unique_username, user_email, "oauth_user")
            if not user_id:
                raise HTTPException(status_code=500, detail="Failed to create OAuth user")
            user = db.get_user_by_id(user_id)
        
        # Update profile picture
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
            samesite="none",
            secure=True,
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
            samesite="none",
            secure=True,
            max_age=2592000 if remember_me else 86400,
            path="/"
        )

        print(f"[GMAIL LOGIN] User {email} logged in successfully (ID: {user['id']})")
        print(f"[GMAIL LOGIN] Token created: {token[:30]}...")
        print(f"[GMAIL LOGIN] Cookie set with max_age={2592000 if remember_me else 86400}")

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
            "last_fetched": datetime.utcnow().isoformat(),
            # Initialize default fields
            "recentAnalyses": [],
            "following": [],
            "notifications": [],
            "analysesRun": 0,
            "comparisonsRun": 0,
            "aiInsightsRun": 0,
            "displayName": "",
            "website": "",
            "location": "",
            "joinedAt": "",
            "avatar": user.get("profile_picture_url", ""),
            "solvedProblems": [],
            "weakCategories": [],
            "lastPracticeProblem": None
        }
        
        # Get stored profile data (analysis history, following, etc.)
        latest_profile = db.get_latest_user_profile(user_id)
        if latest_profile:
            stored_data = latest_profile.get('data', {})
            print(f"[LOAD] Found stored profile: recentAnalyses={len(stored_data.get('recentAnalyses', []))}, "
                  f"following={len(stored_data.get('following', []))}, "
                  f"notifications={len(stored_data.get('notifications', []))}")
            print(f"[LOAD] Stored website: '{stored_data.get('website', 'NOT_FOUND')}', location: '{stored_data.get('location', 'NOT_FOUND')}'")
            print(f"[LOAD] All stored keys: {list(stored_data.keys())}")
            # Merge stored profile data with current user data (overwrite defaults)
            profile_data.update({
                'recentAnalyses': stored_data.get('recentAnalyses', []),
                'following': stored_data.get('following', []),
                'notifications': stored_data.get('notifications', []),
                'analysesRun': stored_data.get('analysesRun', 0),
                'comparisonsRun': stored_data.get('comparisonsRun', 0),
                'aiInsightsRun': stored_data.get('aiInsightsRun', 0),
                'displayName': stored_data.get('displayName', ''),
                'website': stored_data.get('website', ''),
                'location': stored_data.get('location', ''),
                'joinedAt': stored_data.get('joinedAt', ''),
                'avatar': stored_data.get('avatar', profile_data['avatar']),
                'solvedProblems': stored_data.get('solvedProblems', []),
                'weakCategories': stored_data.get('weakCategories', []),
                'lastPracticeProblem': stored_data.get('lastPracticeProblem', None)
            })
            print(f"[LOAD] Merged website: '{profile_data['website']}', location: '{profile_data['location']}'")
        else:
            print(f"[LOAD] No stored profile found for user {user_id} - using defaults")
        
        print(f"[LOAD] Returning profile with {len(profile_data.get('recentAnalyses', []))} analyses")
        
        # Cache the result
        cache_entry = set_cached_data("profile", str(user_id), profile_data)
        add_cache_headers(response, cache_entry["etag"], max_age=60)
        
        # Use JSONResponse to ensure proper serialization
        return JSONResponse(content=profile_data)
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to get profile")

@app.get("/sync/check")
async def sync_check(request: Request):
    """Lightweight endpoint: returns last profile update timestamp for polling"""
    try:
        token = get_token_from_request(request)
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        user_id = payload.get("user_id")
        latest = db.get_latest_user_profile(user_id)
        return {
            "last_updated": latest["created_at"] if latest else None,
            "user_id": user_id
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to check sync")

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
        print(f"[SYNC] Received data: bio={body.get('bio')}, website={body.get('website')}, location={body.get('location')}")
        print(f"[SYNC] Received data: recentAnalyses={len(body.get('recentAnalyses', []))}, "
              f"following={len(body.get('following', []))}, "
              f"notifications={len(body.get('notifications', []))}")
        
        # Load the latest stored snapshot first so we can avoid destructive overwrites
        latest_profile = db.get_latest_user_profile(user_id)
        existing_snapshot = latest_profile.get('data', {}) if latest_profile else {}

        def keep_cloud_value_if_empty(field: str, incoming_value, default_value):
            existing_value = existing_snapshot.get(field)

            # Preserve existing non-empty strings when incoming value is empty
            if isinstance(incoming_value, str):
                if incoming_value.strip() == "" and isinstance(existing_value, str) and existing_value.strip() != "":
                    return existing_value
                return incoming_value

            # Preserve existing non-empty lists when incoming list is empty
            if isinstance(incoming_value, list):
                if len(incoming_value) == 0 and isinstance(existing_value, list) and len(existing_value) > 0:
                    return existing_value
                return incoming_value

            # Preserve existing non-zero numbers when incoming number is zero
            if isinstance(incoming_value, (int, float)):
                if incoming_value == 0 and isinstance(existing_value, (int, float)) and existing_value > 0:
                    return existing_value
                return incoming_value

            # If key is missing in request body, keep existing snapshot value if present
            if incoming_value is None:
                if existing_value is not None:
                    return existing_value
                return default_value

            return incoming_value

        # Update basic user info in database
        update_data = {}
        if "email" in body:
            update_data["email"] = body["email"]
        if "bio" in body:
            update_data["bio"] = keep_cloud_value_if_empty("bio", body.get("bio", ""), "")
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
            'recentAnalyses': keep_cloud_value_if_empty('recentAnalyses', body.get('recentAnalyses', []), []),
            'following': keep_cloud_value_if_empty('following', body.get('following', []), []),
            'notifications': keep_cloud_value_if_empty('notifications', body.get('notifications', []), []),
            'analysesRun': keep_cloud_value_if_empty('analysesRun', body.get('analysesRun', 0), 0),
            'comparisonsRun': keep_cloud_value_if_empty('comparisonsRun', body.get('comparisonsRun', 0), 0),
            'aiInsightsRun': keep_cloud_value_if_empty('aiInsightsRun', body.get('aiInsightsRun', 0), 0),
            'displayName': keep_cloud_value_if_empty('displayName', body.get('displayName', ''), ''),
            'website': keep_cloud_value_if_empty('website', body.get('website', ''), ''),
            'location': keep_cloud_value_if_empty('location', body.get('location', ''), ''),
            'joinedAt': keep_cloud_value_if_empty('joinedAt', body.get('joinedAt', ''), ''),
            'avatar': keep_cloud_value_if_empty('avatar', body.get('avatar', ''), ''),
            'solvedProblems': keep_cloud_value_if_empty('solvedProblems', body.get('solvedProblems', []), []),
            'weakCategories': keep_cloud_value_if_empty('weakCategories', body.get('weakCategories', []), []),
            'lastPracticeProblem': keep_cloud_value_if_empty('lastPracticeProblem', body.get('lastPracticeProblem'), None)
        }
        
        print(f"[SYNC] Snapshot being saved: website={profile_snapshot.get('website')}, location={profile_snapshot.get('location')}")
        
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
        
        print(f"[SYNC] Response data: website={response_data.get('website')}, location={response_data.get('location')}")
        
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

@app.post("/ai/insights")
async def ai_insights(request: Request):
    """Generate AI insights using Groq"""
    try:
        if not GROQ_API_KEY:
            raise HTTPException(status_code=500, detail="AI service not configured")
        
        body = await request.json()
        prompt = body.get("prompt", "")
        
        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt is required")
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 1000
                },
                timeout=30.0
            )
            
            if response.status_code != 200:
                error_body = response.text
                print(f"[AI] Groq error {response.status_code}: {error_body}")
                raise HTTPException(status_code=502, detail=f"AI service error: {response.status_code}")
            
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            return {
                "result": content,
                "model": "llama-3.3-70b-versatile"
            }
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

# ============ CONNECTED ACCOUNTS ENDPOINTS ============

@app.get("/accounts/connected")
async def get_connected_accounts_endpoint(
    request: Request
):
    """Get all connected accounts for the current user"""
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        accounts = db.get_connected_accounts(user_id)
        
        # Don't send sensitive tokens to frontend
        safe_accounts = []
        for acc in accounts:
            safe_accounts.append({
                "platform": acc["platform"],
                "platform_username": acc.get("platform_username"),
                "connected_at": acc["connected_at"],
                "last_synced_at": acc.get("last_synced_at"),
                "is_active": acc["is_active"] == 1,
                "metadata": acc.get("metadata", {})
            })
        
        return {"accounts": safe_accounts}
    
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/accounts/connect/{platform}")
async def connect_account_endpoint(
    platform: str,
    request: Request
):
    """Connect a platform account (GitHub, LeetCode, Codeforces)"""
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        body = await request.json()
        username = body.get("username", "").strip()
        
        if not username:
            raise HTTPException(status_code=400, detail="Username required")
        
        # For now, we'll just store the username
        # In production, you'd verify the account via OAuth or API
        success = db.connect_account(
            user_id=user_id,
            platform=platform.lower(),
            platform_username=username,
            metadata={"verified": False, "manual_entry": True}
        )
        
        if success:
            return {
                "success": True,
                "message": f"{platform} account connected",
                "platform": platform.lower(),
                "username": username
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to connect account")
    
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/accounts/disconnect/{platform}")
async def disconnect_account_endpoint(
    platform: str,
    request: Request
):
    """Disconnect a platform account"""
    token = get_token_from_request(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        success = db.disconnect_account(user_id, platform.lower())
        
        if success:
            return {
                "success": True,
                "message": f"{platform} account disconnected"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to disconnect account")
    
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


# ============ LEETCODE COMPANY TAGS ============
@app.post("/leetcode/company-problems/{company}")
async def leetcode_company_problems_post(company: str, request: Request, response: Response):
    """POST variant: Fetch LeetCode problems tagged with a specific company (same as GET)"""
    # Reuse GET logic
    return await leetcode_company_problems(company, request, response)

@app.get("/leetcode/company-problems/{company}")
async def leetcode_company_problems(company: str, request: Request, response: Response):
    """Fetch LeetCode problems tagged with a specific company"""
    try:
        cache_key = f"company_{company.lower()}"
        cached = get_cached_data("leetcode_company", cache_key)

        if cached:
            if check_etag(request, cached["etag"]):
                return Response(status_code=304)
            add_cache_headers(response, cached["etag"], max_age=3600)
            return cached["data"]

        slug = company.lower().strip().replace(" ", "-")
        result = fetch_company_tagged_questions(slug)
        if "error" in result or not result.get("problems"):
            fallback = _get_company_fallback(slug)
            if fallback:
                cache_entry = set_cached_data("leetcode_company", cache_key, fallback)
                add_cache_headers(response, cache_entry["etag"], max_age=3600)
                return fallback
            raise HTTPException(status_code=404, detail=f"No problems found for company '{company}'")
        cache_entry = set_cached_data("leetcode_company", cache_key, result)
        add_cache_headers(response, cache_entry["etag"], max_age=3600)
        return result

        # (Removed unreachable code: now handled by reusable function)

    except HTTPException:
        raise
    except Exception as e:
        # Fallback to curated data on any error
        fallback = _get_company_fallback(company.lower().strip().replace(" ", "-"))
        if fallback:
            return fallback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/leetcode/companies")
async def leetcode_companies():
    """Return list of supported companies with metadata"""
    return {
        "companies": [
            {"name": "Google", "slug": "google", "icon": "G", "tier": "FAANG"},
            {"name": "Amazon", "slug": "amazon", "icon": "A", "tier": "FAANG"},
            {"name": "Meta", "slug": "facebook", "icon": "M", "tier": "FAANG"},
            {"name": "Apple", "slug": "apple", "icon": "Ap", "tier": "FAANG"},
            {"name": "Netflix", "slug": "netflix", "icon": "N", "tier": "FAANG"},
            {"name": "Microsoft", "slug": "microsoft", "icon": "Ms", "tier": "FAANG"},
            {"name": "Bloomberg", "slug": "bloomberg", "icon": "Bl", "tier": "Top"},
            {"name": "Goldman Sachs", "slug": "goldman-sachs", "icon": "GS", "tier": "Top"},
            {"name": "Uber", "slug": "uber", "icon": "Ub", "tier": "Top"},
            {"name": "LinkedIn", "slug": "linkedin", "icon": "Li", "tier": "Top"},
            {"name": "Adobe", "slug": "adobe", "icon": "Ad", "tier": "Top"},
            {"name": "Oracle", "slug": "oracle", "icon": "Or", "tier": "Top"},
            {"name": "Salesforce", "slug": "salesforce", "icon": "Sf", "tier": "Top"},
            {"name": "Twitter", "slug": "twitter", "icon": "Tw", "tier": "Top"},
            {"name": "Spotify", "slug": "spotify", "icon": "Sp", "tier": "Mid"},
            {"name": "Stripe", "slug": "stripe", "icon": "St", "tier": "Mid"},
            {"name": "Airbnb", "slug": "airbnb", "icon": "Ab", "tier": "Mid"},
            {"name": "Snap", "slug": "snapchat", "icon": "Sn", "tier": "Mid"},
            {"name": "TikTok", "slug": "tiktok", "icon": "Tk", "tier": "Mid"},
            {"name": "Nvidia", "slug": "nvidia", "icon": "Nv", "tier": "Mid"},
            {"name": "PayPal", "slug": "paypal", "icon": "Pp", "tier": "Mid"},
            {"name": "Cisco", "slug": "cisco", "icon": "Cs", "tier": "Mid"},
            {"name": "VMware", "slug": "vmware", "icon": "Vm", "tier": "Mid"},
            {"name": "Walmart", "slug": "walmart", "icon": "Wm", "tier": "Mid"},
            {"name": "JPMorgan", "slug": "jpmorgan", "icon": "JP", "tier": "Mid"},
            {"name": "Samsung", "slug": "samsung", "icon": "Sm", "tier": "Mid"},
            {"name": "Intuit", "slug": "intuit", "icon": "In", "tier": "Mid"},
            {"name": "Yahoo", "slug": "yahoo", "icon": "Ya", "tier": "Mid"},
        ]
    }


def _get_company_fallback(slug: str) -> dict | None:
    """Return curated company problem sets for well-known companies."""
    # Curated sets of the most well-known company-tagged problems
    _DATA = {
        "google": {
            "company": "Google", "slug": "google",
            "problems": [
                {"id": "1", "title": "Two Sum", "slug": "two-sum", "difficulty": "Easy", "topicTags": ["Array", "Hash Table"], "paidOnly": False, "frequency": 95, "url": "https://leetcode.com/problems/two-sum/"},
                {"id": "4", "title": "Median of Two Sorted Arrays", "slug": "median-of-two-sorted-arrays", "difficulty": "Hard", "topicTags": ["Array", "Binary Search", "Divide and Conquer"], "paidOnly": False, "frequency": 90, "url": "https://leetcode.com/problems/median-of-two-sorted-arrays/"},
                {"id": "5", "title": "Longest Palindromic Substring", "slug": "longest-palindromic-substring", "difficulty": "Medium", "topicTags": ["String", "Dynamic Programming"], "paidOnly": False, "frequency": 88, "url": "https://leetcode.com/problems/longest-palindromic-substring/"},
                {"id": "20", "title": "Valid Parentheses", "slug": "valid-parentheses", "difficulty": "Easy", "topicTags": ["String", "Stack"], "paidOnly": False, "frequency": 85, "url": "https://leetcode.com/problems/valid-parentheses/"},
                {"id": "42", "title": "Trapping Rain Water", "slug": "trapping-rain-water", "difficulty": "Hard", "topicTags": ["Array", "Two Pointers", "Stack", "Dynamic Programming"], "paidOnly": False, "frequency": 92, "url": "https://leetcode.com/problems/trapping-rain-water/"},
                {"id": "56", "title": "Merge Intervals", "slug": "merge-intervals", "difficulty": "Medium", "topicTags": ["Array", "Sorting"], "paidOnly": False, "frequency": 89, "url": "https://leetcode.com/problems/merge-intervals/"},
                {"id": "200", "title": "Number of Islands", "slug": "number-of-islands", "difficulty": "Medium", "topicTags": ["Array", "BFS", "DFS", "Union Find"], "paidOnly": False, "frequency": 87, "url": "https://leetcode.com/problems/number-of-islands/"},
                {"id": "146", "title": "LRU Cache", "slug": "lru-cache", "difficulty": "Medium", "topicTags": ["Hash Table", "Linked List", "Design"], "paidOnly": False, "frequency": 91, "url": "https://leetcode.com/problems/lru-cache/"},
                {"id": "322", "title": "Coin Change", "slug": "coin-change", "difficulty": "Medium", "topicTags": ["Array", "Dynamic Programming", "BFS"], "paidOnly": False, "frequency": 84, "url": "https://leetcode.com/problems/coin-change/"},
                {"id": "253", "title": "Meeting Rooms II", "slug": "meeting-rooms-ii", "difficulty": "Medium", "topicTags": ["Array", "Two Pointers", "Greedy", "Sorting", "Heap"], "paidOnly": True, "frequency": 93, "url": "https://leetcode.com/problems/meeting-rooms-ii/"},
                {"id": "76", "title": "Minimum Window Substring", "slug": "minimum-window-substring", "difficulty": "Hard", "topicTags": ["Hash Table", "String", "Sliding Window"], "paidOnly": False, "frequency": 86, "url": "https://leetcode.com/problems/minimum-window-substring/"},
                {"id": "23", "title": "Merge k Sorted Lists", "slug": "merge-k-sorted-lists", "difficulty": "Hard", "topicTags": ["Linked List", "Divide and Conquer", "Heap"], "paidOnly": False, "frequency": 83, "url": "https://leetcode.com/problems/merge-k-sorted-lists/"},
            ],
        },
        "amazon": {
            "company": "Amazon", "slug": "amazon",
            "problems": [
                {"id": "1", "title": "Two Sum", "slug": "two-sum", "difficulty": "Easy", "topicTags": ["Array", "Hash Table"], "paidOnly": False, "frequency": 97, "url": "https://leetcode.com/problems/two-sum/"},
                {"id": "2", "title": "Add Two Numbers", "slug": "add-two-numbers", "difficulty": "Medium", "topicTags": ["Linked List", "Math", "Recursion"], "paidOnly": False, "frequency": 88, "url": "https://leetcode.com/problems/add-two-numbers/"},
                {"id": "3", "title": "Longest Substring Without Repeating Characters", "slug": "longest-substring-without-repeating-characters", "difficulty": "Medium", "topicTags": ["Hash Table", "String", "Sliding Window"], "paidOnly": False, "frequency": 92, "url": "https://leetcode.com/problems/longest-substring-without-repeating-characters/"},
                {"id": "21", "title": "Merge Two Sorted Lists", "slug": "merge-two-sorted-lists", "difficulty": "Easy", "topicTags": ["Linked List", "Recursion"], "paidOnly": False, "frequency": 86, "url": "https://leetcode.com/problems/merge-two-sorted-lists/"},
                {"id": "49", "title": "Group Anagrams", "slug": "group-anagrams", "difficulty": "Medium", "topicTags": ["Array", "Hash Table", "String", "Sorting"], "paidOnly": False, "frequency": 90, "url": "https://leetcode.com/problems/group-anagrams/"},
                {"id": "127", "title": "Word Ladder", "slug": "word-ladder", "difficulty": "Hard", "topicTags": ["Hash Table", "String", "BFS"], "paidOnly": False, "frequency": 82, "url": "https://leetcode.com/problems/word-ladder/"},
                {"id": "138", "title": "Copy List with Random Pointer", "slug": "copy-list-with-random-pointer", "difficulty": "Medium", "topicTags": ["Hash Table", "Linked List"], "paidOnly": False, "frequency": 85, "url": "https://leetcode.com/problems/copy-list-with-random-pointer/"},
                {"id": "200", "title": "Number of Islands", "slug": "number-of-islands", "difficulty": "Medium", "topicTags": ["Array", "BFS", "DFS", "Union Find"], "paidOnly": False, "frequency": 93, "url": "https://leetcode.com/problems/number-of-islands/"},
                {"id": "297", "title": "Serialize and Deserialize Binary Tree", "slug": "serialize-and-deserialize-binary-tree", "difficulty": "Hard", "topicTags": ["String", "Tree", "DFS", "BFS", "Design"], "paidOnly": False, "frequency": 84, "url": "https://leetcode.com/problems/serialize-and-deserialize-binary-tree/"},
                {"id": "937", "title": "Reorder Data in Log Files", "slug": "reorder-data-in-log-files", "difficulty": "Medium", "topicTags": ["Array", "String", "Sorting"], "paidOnly": False, "frequency": 91, "url": "https://leetcode.com/problems/reorder-data-in-log-files/"},
                {"id": "973", "title": "K Closest Points to Origin", "slug": "k-closest-points-to-origin", "difficulty": "Medium", "topicTags": ["Array", "Math", "Divide and Conquer", "Sorting", "Heap"], "paidOnly": False, "frequency": 89, "url": "https://leetcode.com/problems/k-closest-points-to-origin/"},
                {"id": "819", "title": "Most Common Word", "slug": "most-common-word", "difficulty": "Easy", "topicTags": ["Hash Table", "String", "Counting"], "paidOnly": False, "frequency": 87, "url": "https://leetcode.com/problems/most-common-word/"},
            ],
        },
        "facebook": {
            "company": "Meta", "slug": "facebook",
            "problems": [
                {"id": "1", "title": "Two Sum", "slug": "two-sum", "difficulty": "Easy", "topicTags": ["Array", "Hash Table"], "paidOnly": False, "frequency": 90, "url": "https://leetcode.com/problems/two-sum/"},
                {"id": "15", "title": "3Sum", "slug": "3sum", "difficulty": "Medium", "topicTags": ["Array", "Two Pointers", "Sorting"], "paidOnly": False, "frequency": 92, "url": "https://leetcode.com/problems/3sum/"},
                {"id": "23", "title": "Merge k Sorted Lists", "slug": "merge-k-sorted-lists", "difficulty": "Hard", "topicTags": ["Linked List", "Divide and Conquer", "Heap"], "paidOnly": False, "frequency": 88, "url": "https://leetcode.com/problems/merge-k-sorted-lists/"},
                {"id": "56", "title": "Merge Intervals", "slug": "merge-intervals", "difficulty": "Medium", "topicTags": ["Array", "Sorting"], "paidOnly": False, "frequency": 95, "url": "https://leetcode.com/problems/merge-intervals/"},
                {"id": "88", "title": "Merge Sorted Array", "slug": "merge-sorted-array", "difficulty": "Easy", "topicTags": ["Array", "Two Pointers", "Sorting"], "paidOnly": False, "frequency": 85, "url": "https://leetcode.com/problems/merge-sorted-array/"},
                {"id": "121", "title": "Best Time to Buy and Sell Stock", "slug": "best-time-to-buy-and-sell-stock", "difficulty": "Easy", "topicTags": ["Array", "Dynamic Programming"], "paidOnly": False, "frequency": 87, "url": "https://leetcode.com/problems/best-time-to-buy-and-sell-stock/"},
                {"id": "124", "title": "Binary Tree Maximum Path Sum", "slug": "binary-tree-maximum-path-sum", "difficulty": "Hard", "topicTags": ["Dynamic Programming", "Tree", "DFS"], "paidOnly": False, "frequency": 91, "url": "https://leetcode.com/problems/binary-tree-maximum-path-sum/"},
                {"id": "199", "title": "Binary Tree Right Side View", "slug": "binary-tree-right-side-view", "difficulty": "Medium", "topicTags": ["Tree", "DFS", "BFS"], "paidOnly": False, "frequency": 86, "url": "https://leetcode.com/problems/binary-tree-right-side-view/"},
                {"id": "215", "title": "Kth Largest Element in an Array", "slug": "kth-largest-element-in-an-array", "difficulty": "Medium", "topicTags": ["Array", "Divide and Conquer", "Sorting", "Heap"], "paidOnly": False, "frequency": 93, "url": "https://leetcode.com/problems/kth-largest-element-in-an-array/"},
                {"id": "301", "title": "Remove Invalid Parentheses", "slug": "remove-invalid-parentheses", "difficulty": "Hard", "topicTags": ["String", "BFS", "DFS"], "paidOnly": False, "frequency": 84, "url": "https://leetcode.com/problems/remove-invalid-parentheses/"},
                {"id": "621", "title": "Task Scheduler", "slug": "task-scheduler", "difficulty": "Medium", "topicTags": ["Array", "Hash Table", "Greedy", "Sorting", "Heap"], "paidOnly": False, "frequency": 89, "url": "https://leetcode.com/problems/task-scheduler/"},
                {"id": "986", "title": "Interval List Intersections", "slug": "interval-list-intersections", "difficulty": "Medium", "topicTags": ["Array", "Two Pointers"], "paidOnly": False, "frequency": 88, "url": "https://leetcode.com/problems/interval-list-intersections/"},
            ],
        },
        "apple": {
            "company": "Apple", "slug": "apple",
            "problems": [
                {"id": "1", "title": "Two Sum", "slug": "two-sum", "difficulty": "Easy", "topicTags": ["Array", "Hash Table"], "paidOnly": False, "frequency": 88, "url": "https://leetcode.com/problems/two-sum/"},
                {"id": "7", "title": "Reverse Integer", "slug": "reverse-integer", "difficulty": "Medium", "topicTags": ["Math"], "paidOnly": False, "frequency": 80, "url": "https://leetcode.com/problems/reverse-integer/"},
                {"id": "11", "title": "Container With Most Water", "slug": "container-with-most-water", "difficulty": "Medium", "topicTags": ["Array", "Two Pointers", "Greedy"], "paidOnly": False, "frequency": 85, "url": "https://leetcode.com/problems/container-with-most-water/"},
                {"id": "53", "title": "Maximum Subarray", "slug": "maximum-subarray", "difficulty": "Medium", "topicTags": ["Array", "Divide and Conquer", "Dynamic Programming"], "paidOnly": False, "frequency": 87, "url": "https://leetcode.com/problems/maximum-subarray/"},
                {"id": "70", "title": "Climbing Stairs", "slug": "climbing-stairs", "difficulty": "Easy", "topicTags": ["Math", "Dynamic Programming", "Memoization"], "paidOnly": False, "frequency": 82, "url": "https://leetcode.com/problems/climbing-stairs/"},
                {"id": "146", "title": "LRU Cache", "slug": "lru-cache", "difficulty": "Medium", "topicTags": ["Hash Table", "Linked List", "Design"], "paidOnly": False, "frequency": 86, "url": "https://leetcode.com/problems/lru-cache/"},
                {"id": "206", "title": "Reverse Linked List", "slug": "reverse-linked-list", "difficulty": "Easy", "topicTags": ["Linked List", "Recursion"], "paidOnly": False, "frequency": 84, "url": "https://leetcode.com/problems/reverse-linked-list/"},
                {"id": "238", "title": "Product of Array Except Self", "slug": "product-of-array-except-self", "difficulty": "Medium", "topicTags": ["Array", "Prefix Sum"], "paidOnly": False, "frequency": 83, "url": "https://leetcode.com/problems/product-of-array-except-self/"},
                {"id": "283", "title": "Move Zeroes", "slug": "move-zeroes", "difficulty": "Easy", "topicTags": ["Array", "Two Pointers"], "paidOnly": False, "frequency": 81, "url": "https://leetcode.com/problems/move-zeroes/"},
                {"id": "347", "title": "Top K Frequent Elements", "slug": "top-k-frequent-elements", "difficulty": "Medium", "topicTags": ["Array", "Hash Table", "Sorting", "Heap"], "paidOnly": False, "frequency": 85, "url": "https://leetcode.com/problems/top-k-frequent-elements/"},
            ],
        },
        "microsoft": {
            "company": "Microsoft", "slug": "microsoft",
            "problems": [
                {"id": "1", "title": "Two Sum", "slug": "two-sum", "difficulty": "Easy", "topicTags": ["Array", "Hash Table"], "paidOnly": False, "frequency": 92, "url": "https://leetcode.com/problems/two-sum/"},
                {"id": "2", "title": "Add Two Numbers", "slug": "add-two-numbers", "difficulty": "Medium", "topicTags": ["Linked List", "Math", "Recursion"], "paidOnly": False, "frequency": 85, "url": "https://leetcode.com/problems/add-two-numbers/"},
                {"id": "33", "title": "Search in Rotated Sorted Array", "slug": "search-in-rotated-sorted-array", "difficulty": "Medium", "topicTags": ["Array", "Binary Search"], "paidOnly": False, "frequency": 88, "url": "https://leetcode.com/problems/search-in-rotated-sorted-array/"},
                {"id": "54", "title": "Spiral Matrix", "slug": "spiral-matrix", "difficulty": "Medium", "topicTags": ["Array", "Matrix", "Simulation"], "paidOnly": False, "frequency": 86, "url": "https://leetcode.com/problems/spiral-matrix/"},
                {"id": "73", "title": "Set Matrix Zeroes", "slug": "set-matrix-zeroes", "difficulty": "Medium", "topicTags": ["Array", "Hash Table", "Matrix"], "paidOnly": False, "frequency": 84, "url": "https://leetcode.com/problems/set-matrix-zeroes/"},
                {"id": "146", "title": "LRU Cache", "slug": "lru-cache", "difficulty": "Medium", "topicTags": ["Hash Table", "Linked List", "Design"], "paidOnly": False, "frequency": 90, "url": "https://leetcode.com/problems/lru-cache/"},
                {"id": "151", "title": "Reverse Words in a String", "slug": "reverse-words-in-a-string", "difficulty": "Medium", "topicTags": ["Two Pointers", "String"], "paidOnly": False, "frequency": 82, "url": "https://leetcode.com/problems/reverse-words-in-a-string/"},
                {"id": "212", "title": "Word Search II", "slug": "word-search-ii", "difficulty": "Hard", "topicTags": ["Array", "String", "Backtracking", "Trie", "Matrix"], "paidOnly": False, "frequency": 83, "url": "https://leetcode.com/problems/word-search-ii/"},
                {"id": "236", "title": "Lowest Common Ancestor of a Binary Tree", "slug": "lowest-common-ancestor-of-a-binary-tree", "difficulty": "Medium", "topicTags": ["Tree", "DFS", "Binary Tree"], "paidOnly": False, "frequency": 89, "url": "https://leetcode.com/problems/lowest-common-ancestor-of-a-binary-tree/"},
                {"id": "348", "title": "Design Tic-Tac-Toe", "slug": "design-tic-tac-toe", "difficulty": "Medium", "topicTags": ["Array", "Hash Table", "Design", "Matrix"], "paidOnly": True, "frequency": 87, "url": "https://leetcode.com/problems/design-tic-tac-toe/"},
                {"id": "545", "title": "Boundary of Binary Tree", "slug": "boundary-of-binary-tree", "difficulty": "Medium", "topicTags": ["Tree", "DFS", "Binary Tree"], "paidOnly": True, "frequency": 81, "url": "https://leetcode.com/problems/boundary-of-binary-tree/"},
            ],
        },
        "netflix": {
            "company": "Netflix", "slug": "netflix",
            "problems": [
                {"id": "1", "title": "Two Sum", "slug": "two-sum", "difficulty": "Easy", "topicTags": ["Array", "Hash Table"], "paidOnly": False, "frequency": 80, "url": "https://leetcode.com/problems/two-sum/"},
                {"id": "3", "title": "Longest Substring Without Repeating Characters", "slug": "longest-substring-without-repeating-characters", "difficulty": "Medium", "topicTags": ["Hash Table", "String", "Sliding Window"], "paidOnly": False, "frequency": 85, "url": "https://leetcode.com/problems/longest-substring-without-repeating-characters/"},
                {"id": "146", "title": "LRU Cache", "slug": "lru-cache", "difficulty": "Medium", "topicTags": ["Hash Table", "Linked List", "Design"], "paidOnly": False, "frequency": 88, "url": "https://leetcode.com/problems/lru-cache/"},
                {"id": "200", "title": "Number of Islands", "slug": "number-of-islands", "difficulty": "Medium", "topicTags": ["Array", "BFS", "DFS", "Union Find"], "paidOnly": False, "frequency": 82, "url": "https://leetcode.com/problems/number-of-islands/"},
                {"id": "207", "title": "Course Schedule", "slug": "course-schedule", "difficulty": "Medium", "topicTags": ["DFS", "BFS", "Graph", "Topological Sort"], "paidOnly": False, "frequency": 84, "url": "https://leetcode.com/problems/course-schedule/"},
                {"id": "239", "title": "Sliding Window Maximum", "slug": "sliding-window-maximum", "difficulty": "Hard", "topicTags": ["Array", "Queue", "Sliding Window", "Heap", "Monotonic Queue"], "paidOnly": False, "frequency": 86, "url": "https://leetcode.com/problems/sliding-window-maximum/"},
                {"id": "295", "title": "Find Median from Data Stream", "slug": "find-median-from-data-stream", "difficulty": "Hard", "topicTags": ["Two Pointers", "Design", "Sorting", "Heap", "Data Stream"], "paidOnly": False, "frequency": 83, "url": "https://leetcode.com/problems/find-median-from-data-stream/"},
                {"id": "380", "title": "Insert Delete GetRandom O(1)", "slug": "insert-delete-getrandom-o1", "difficulty": "Medium", "topicTags": ["Array", "Hash Table", "Math", "Design", "Randomized"], "paidOnly": False, "frequency": 81, "url": "https://leetcode.com/problems/insert-delete-getrandom-o1/"},
            ],
        },
        "bloomberg": {
            "company": "Bloomberg", "slug": "bloomberg",
            "problems": [
                {"id": "1", "title": "Two Sum", "slug": "two-sum", "difficulty": "Easy", "topicTags": ["Array", "Hash Table"], "paidOnly": False, "frequency": 90, "url": "https://leetcode.com/problems/two-sum/"},
                {"id": "20", "title": "Valid Parentheses", "slug": "valid-parentheses", "difficulty": "Easy", "topicTags": ["String", "Stack"], "paidOnly": False, "frequency": 88, "url": "https://leetcode.com/problems/valid-parentheses/"},
                {"id": "42", "title": "Trapping Rain Water", "slug": "trapping-rain-water", "difficulty": "Hard", "topicTags": ["Array", "Two Pointers", "Stack", "Dynamic Programming"], "paidOnly": False, "frequency": 85, "url": "https://leetcode.com/problems/trapping-rain-water/"},
                {"id": "146", "title": "LRU Cache", "slug": "lru-cache", "difficulty": "Medium", "topicTags": ["Hash Table", "Linked List", "Design"], "paidOnly": False, "frequency": 87, "url": "https://leetcode.com/problems/lru-cache/"},
                {"id": "238", "title": "Product of Array Except Self", "slug": "product-of-array-except-self", "difficulty": "Medium", "topicTags": ["Array", "Prefix Sum"], "paidOnly": False, "frequency": 84, "url": "https://leetcode.com/problems/product-of-array-except-self/"},
                {"id": "380", "title": "Insert Delete GetRandom O(1)", "slug": "insert-delete-getrandom-o1", "difficulty": "Medium", "topicTags": ["Array", "Hash Table", "Math", "Design", "Randomized"], "paidOnly": False, "frequency": 83, "url": "https://leetcode.com/problems/insert-delete-getrandom-o1/"},
                {"id": "692", "title": "Top K Frequent Words", "slug": "top-k-frequent-words", "difficulty": "Medium", "topicTags": ["Hash Table", "String", "Trie", "Sorting", "Heap"], "paidOnly": False, "frequency": 86, "url": "https://leetcode.com/problems/top-k-frequent-words/"},
                {"id": "735", "title": "Asteroid Collision", "slug": "asteroid-collision", "difficulty": "Medium", "topicTags": ["Array", "Stack", "Simulation"], "paidOnly": False, "frequency": 82, "url": "https://leetcode.com/problems/asteroid-collision/"},
            ],
        },
        "goldman-sachs": {
            "company": "Goldman Sachs", "slug": "goldman-sachs",
            "problems": [
                {"id": "1", "title": "Two Sum", "slug": "two-sum", "difficulty": "Easy", "topicTags": ["Array", "Hash Table"], "paidOnly": False, "frequency": 88, "url": "https://leetcode.com/problems/two-sum/"},
                {"id": "11", "title": "Container With Most Water", "slug": "container-with-most-water", "difficulty": "Medium", "topicTags": ["Array", "Two Pointers", "Greedy"], "paidOnly": False, "frequency": 82, "url": "https://leetcode.com/problems/container-with-most-water/"},
                {"id": "15", "title": "3Sum", "slug": "3sum", "difficulty": "Medium", "topicTags": ["Array", "Two Pointers", "Sorting"], "paidOnly": False, "frequency": 85, "url": "https://leetcode.com/problems/3sum/"},
                {"id": "48", "title": "Rotate Image", "slug": "rotate-image", "difficulty": "Medium", "topicTags": ["Array", "Math", "Matrix"], "paidOnly": False, "frequency": 84, "url": "https://leetcode.com/problems/rotate-image/"},
                {"id": "54", "title": "Spiral Matrix", "slug": "spiral-matrix", "difficulty": "Medium", "topicTags": ["Array", "Matrix", "Simulation"], "paidOnly": False, "frequency": 86, "url": "https://leetcode.com/problems/spiral-matrix/"},
                {"id": "242", "title": "Valid Anagram", "slug": "valid-anagram", "difficulty": "Easy", "topicTags": ["Hash Table", "String", "Sorting"], "paidOnly": False, "frequency": 80, "url": "https://leetcode.com/problems/valid-anagram/"},
                {"id": "347", "title": "Top K Frequent Elements", "slug": "top-k-frequent-elements", "difficulty": "Medium", "topicTags": ["Array", "Hash Table", "Sorting", "Heap"], "paidOnly": False, "frequency": 83, "url": "https://leetcode.com/problems/top-k-frequent-elements/"},
                {"id": "380", "title": "Insert Delete GetRandom O(1)", "slug": "insert-delete-getrandom-o1", "difficulty": "Medium", "topicTags": ["Array", "Hash Table", "Math", "Design", "Randomized"], "paidOnly": False, "frequency": 81, "url": "https://leetcode.com/problems/insert-delete-getrandom-o1/"},
            ],
        },
    }

    entry = _DATA.get(slug)
    if not entry:
        return None

    return {
        "company": entry["company"],
        "slug": entry["slug"],
        "total_problems": len(entry["problems"]),
        "problems": entry["problems"],
        "last_updated": datetime.utcnow().isoformat(),
    }


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