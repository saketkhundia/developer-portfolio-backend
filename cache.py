"""
Caching layer for developer portfolio data
Supports both Redis and disk-based fallback for data synchronization
"""
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Any
import diskcache as dc
import os

# Initialize disk cache (fallback when Redis unavailable)
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")
cache = dc.Cache(CACHE_DIR)

# Cache TTL settings (in seconds)
CACHE_TTL = {
    "github": 300,      # 5 minutes
    "leetcode": 600,    # 10 minutes
    "codeforces": 600,  # 10 minutes
    "contributions": 300,  # 5 minutes
    "profile": 60,      # 1 minute
}

def get_cache_key(prefix: str, identifier: str) -> str:
    """Generate consistent cache key"""
    return f"{prefix}:{identifier}"

def get_etag(data: Any) -> str:
    """Generate ETag from data hash"""
    data_str = json.dumps(data, sort_keys=True, default=str)
    return hashlib.md5(data_str.encode()).hexdigest()

def get_cached_data(prefix: str, identifier: str) -> Optional[dict]:
    """
    Retrieve cached data with metadata
    Returns: dict with 'data', 'etag', 'cached_at', 'expires_at' or None
    """
    try:
        key = get_cache_key(prefix, identifier)
        cached = cache.get(key)
        
        if cached:
            # Verify expiration
            if cached.get("expires_at"):
                expires_at = datetime.fromisoformat(cached["expires_at"])
                if datetime.utcnow() > expires_at:
                    cache.delete(key)
                    return None
            return cached
        return None
    except Exception as e:
        print(f"Cache get error: {e}")
        return None

def set_cached_data(prefix: str, identifier: str, data: Any, ttl: Optional[int] = None) -> dict:
    """
    Store data in cache with metadata
    Returns: dict with 'data', 'etag', 'cached_at', 'expires_at'
    """
    try:
        key = get_cache_key(prefix, identifier)
        
        # Use default TTL if not provided
        if ttl is None:
            ttl = CACHE_TTL.get(prefix, 300)
        
        now = datetime.utcnow()
        expires_at = now + timedelta(seconds=ttl)
        etag = get_etag(data)
        
        cache_entry = {
            "data": data,
            "etag": etag,
            "cached_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "version": 1
        }
        
        # Store with expiration
        cache.set(key, cache_entry, expire=ttl)
        
        return cache_entry
    except Exception as e:
        print(f"Cache set error: {e}")
        return {
            "data": data,
            "etag": get_etag(data),
            "cached_at": datetime.utcnow().isoformat(),
            "expires_at": None,
            "version": 1
        }

def invalidate_cache(prefix: str, identifier: str) -> bool:
    """
    Manually invalidate cached data
    Returns: True if deleted, False otherwise
    """
    try:
        key = get_cache_key(prefix, identifier)
        return cache.delete(key)
    except Exception as e:
        print(f"Cache invalidate error: {e}")
        return False

def clear_all_cache() -> bool:
    """Clear entire cache (use with caution)"""
    try:
        cache.clear()
        return True
    except Exception as e:
        print(f"Cache clear error: {e}")
        return False

def get_cache_stats() -> dict:
    """Get cache statistics"""
    try:
        return {
            "size": len(cache),
            "volume": cache.volume(),
            "directory": CACHE_DIR
        }
    except Exception as e:
        print(f"Cache stats error: {e}")
        return {"error": str(e)}
