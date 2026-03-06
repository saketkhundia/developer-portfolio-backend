import asyncio
import os
import traceback
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Cookie, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import jwt

try:
    from github import fetch_github_data
    from analytics import calculate_skill_score
    from leetcode import fetch_leetcode_data
    from codeforces import fetch_codeforces_data
    from contributions import fetch_contributions
    from cache import get_cached_data, set_cached_data, invalidate_cache, get_cache_stats
except ImportError as e:
    print(f"IMPORT ERROR: {e}")

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

# In-memory user store (replace with database in production)
USERS_DB = {
    "testuser": {"id": 1, "username": "testuser", "email": "test@example.com", "password": "hashed_password"}
}

# ============ AUTH HELPERS ============
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Create a JWT token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRY_HOURS)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_token(token: str):
    """Verify JWT token"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
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

@app.post("/auth/login")
async def auth_login(request: Request):
    """
    Login endpoint - accepts username and password
    Returns access token in response
    """
    try:
        body = await request.json()
        username = body.get("username")
        password = body.get("password")
        
        if not username or not password:
            raise HTTPException(status_code=400, detail="Username and password required")
        
        # Validate user (replace with real password hashing in production)
        user = USERS_DB.get(username)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        # Create token
        token = create_access_token({"sub": username, "user_id": user["id"]})
        
        return {
            "access_token": token,
            "token_type": "bearer",
            "user": {
                "id": user["id"],
                "username": user["username"],
                "email": user["email"]
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Login failed")

@app.get("/auth/me")
async def auth_me(request: Request):
    """
    Get current authenticated user
    Requires valid JWT token
    """
    try:
        token = get_token_from_request(request)
        
        if not token:
            raise HTTPException(status_code=401, detail="No token provided")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        
        username = payload.get("sub")
        user = USERS_DB.get(username)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        return {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"]
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to get user info")

@app.post("/auth/oauth")
async def auth_oauth(request: Request):
    """
    OAuth callback endpoint
    Handles OAuth provider responses
    """
    try:
        body = await request.json()
        provider = body.get("provider")  # github, google, etc.
        code = body.get("code")
        
        if not provider or not code:
            raise HTTPException(status_code=400, detail="Provider and code required")
        
        # TODO: Implement OAuth logic for different providers
        # For now, return placeholder
        token = create_access_token({"sub": "oauth_user", "provider": provider})
        
        return {
            "access_token": token,
            "token_type": "bearer",
            "provider": provider
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="OAuth failed")

@app.post("/auth/logout")
async def auth_logout(request: Request):
    """
    Logout endpoint
    Invalidates user session (frontend should clear token)
    """
    try:
        token = get_token_from_request(request)
        if not token:
            raise HTTPException(status_code=400, detail="No token provided")
        
        # In production, add token to blacklist
        
        return {
            "status": "success",
            "message": "Logged out successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Logout failed")

# ============ PROFILE ENDPOINTS ============

@app.get("/profile")
async def get_profile(request: Request):
    """Get authenticated user's profile"""
    try:
        token = get_token_from_request(request)
        
        if not token:
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        payload = verify_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        username = payload.get("sub")
        user = USERS_DB.get(username)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        return {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "bio": "Developer",
            "created_at": datetime.utcnow().isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to get profile")

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
        
        username = payload.get("sub")
        user = USERS_DB.get(username)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        body = await request.json()
        
        # Update user fields
        if "email" in body:
            user["email"] = body["email"]
        if "bio" in body:
            user["bio"] = body.get("bio", "")
        
        return {
            "message": "Profile updated",
            "user": user
        }
    except HTTPException:
        raise
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Failed to update profile")

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
        from contributions import fetch_contributions
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