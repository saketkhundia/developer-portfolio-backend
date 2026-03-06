"""
Persistent storage for user profiles and authentication
Uses SQLite for simple, reliable persistence
"""
import sqlite3
import json
import os
from datetime import datetime
from typing import Optional, Dict, Any
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    """Initialize database tables"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                bio TEXT,
                github_username TEXT,
                leetcode_username TEXT,
                codeforces_handle TEXT,
                profile_picture_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        
        # User profiles table (for profile history)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                profile_data TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        
        # Sessions table (for token tracking)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token TEXT UNIQUE NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_accessed TEXT NOT NULL,
                device_name TEXT,
                device_type TEXT,
                ip_address TEXT,
                user_agent TEXT,
                is_active INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        
        # Profile history table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profile_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                data TEXT,
                timestamp TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        
        # Connected accounts table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS connected_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                platform_user_id TEXT,
                platform_username TEXT,
                access_token TEXT,
                refresh_token TEXT,
                token_expires_at TEXT,
                connected_at TEXT NOT NULL,
                last_synced_at TEXT,
                is_active INTEGER DEFAULT 1,
                metadata TEXT,
                UNIQUE(user_id, platform),
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        
        conn.commit()

# Initialize database on import
init_db()

# ============ USER OPERATIONS ============

def create_user(username: str, email: str, password: str, **kwargs) -> Optional[int]:
    """Create a new user"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            
            cursor.execute("""
                INSERT INTO users (username, email, password, bio, github_username, 
                                 leetcode_username, codeforces_handle, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                username, email, password,
                kwargs.get('bio', ''),
                kwargs.get('github_username', ''),
                kwargs.get('leetcode_username', ''),
                kwargs.get('codeforces_handle', ''),
                now, now
            ))
            
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None

def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Get user by username"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        
        if row:
            return dict(row)
        return None

def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Get user by email (case-insensitive)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,))
        row = cursor.fetchone()

        if row:
            return dict(row)
        return None

def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    """Get user by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        
        if row:
            return dict(row)
        return None

def update_user(user_id: int, **kwargs) -> bool:
    """Update user information"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            
            # Build update query dynamically
            fields = []
            values = []
            
            for key in ['email', 'bio', 'github_username', 'leetcode_username', 'codeforces_handle', 'profile_picture_url']:
                if key in kwargs:
                    fields.append(f"{key} = ?")
                    values.append(kwargs[key])
            
            if not fields:
                return True
            
            fields.append("updated_at = ?")
            values.append(now)
            values.append(user_id)
            
            query = f"UPDATE users SET {', '.join(fields)} WHERE id = ?"
            cursor.execute(query, values)
            
            return cursor.rowcount > 0
    except Exception as e:
        print(f"Update user error: {e}")
        return False

# ============ SESSION OPERATIONS ============

def create_session(user_id: int, token: str, expires_at: str, device_info: Optional[Dict] = None) -> Optional[int]:
    """Create a new session with device tracking"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            
            device_name = device_info.get('device_name', 'Unknown') if device_info else 'Unknown'
            device_type = device_info.get('device_type', 'Unknown') if device_info else 'Unknown'
            ip_address = device_info.get('ip_address', '') if device_info else ''
            user_agent = device_info.get('user_agent', '') if device_info else ''
            
            cursor.execute("""
                INSERT INTO sessions (user_id, token, expires_at, created_at, last_accessed,
                                    device_name, device_type, ip_address, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, token, expires_at, now, now, device_name, device_type, ip_address, user_agent))
            
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None

def get_session(token: str) -> Optional[Dict[str, Any]]:
    """Get session by token"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sessions WHERE token = ?", (token,))
        row = cursor.fetchone()
        
        if row:
            # Update last accessed
            cursor.execute(
                "UPDATE sessions SET last_accessed = ? WHERE token = ?",
                (datetime.utcnow().isoformat(), token)
            )
            conn.commit()
            return dict(row)
        return None

def get_user_sessions(user_id: int) -> list:
    """Get all active sessions for a user (multi-device)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, device_name, device_type, ip_address, created_at, last_accessed
            FROM sessions 
            WHERE user_id = ? AND is_active = 1 AND expires_at > ?
            ORDER BY last_accessed DESC
        """, (user_id, datetime.utcnow().isoformat()))
        
        rows = cursor.fetchall()
        sessions = []
        for row in rows:
            sessions.append(dict(row))
        return sessions

def delete_session(token: str) -> bool:
    """Delete a session (logout)"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE sessions SET is_active = 0 WHERE token = ?", (token,))
        return cursor.rowcount > 0

def delete_all_user_sessions(user_id: int, except_token: Optional[str] = None) -> int:
    """Logout from all devices (except current if token provided)"""
    with get_db() as conn:
        cursor = conn.cursor()
        if except_token:
            cursor.execute(
                "UPDATE sessions SET is_active = 0 WHERE user_id = ? AND token != ?",
                (user_id, except_token)
            )
        else:
            cursor.execute(
                "UPDATE sessions SET is_active = 0 WHERE user_id = ?",
                (user_id,)
            )
        return cursor.rowcount

def cleanup_expired_sessions():
    """Remove expired sessions"""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        cursor.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        return cursor.rowcount

# ============ PROFILE HISTORY OPERATIONS ============

def add_profile_history(user_id: int, action: str, data: Optional[Dict] = None) -> int:
    """Add profile history entry"""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        
        cursor.execute("""
            INSERT INTO profile_history (user_id, action, data, timestamp)
            VALUES (?, ?, ?, ?)
        """, (user_id, action, json.dumps(data) if data else None, now))
        
        return cursor.lastrowid

def get_profile_history(user_id: int, limit: int = 50) -> list:
    """Get profile history for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM profile_history 
            WHERE user_id = ? 
            ORDER BY timestamp DESC 
            LIMIT ?
        """, (user_id, limit))
        
        rows = cursor.fetchall()
        history = []
        
        for row in rows:
            entry = dict(row)
            if entry.get('data'):
                try:
                    entry['data'] = json.loads(entry['data'])
                except:
                    pass
            history.append(entry)
        
        return history

# ============ PROFILE DATA OPERATIONS ============

def save_user_profile(user_id: int, profile_data: Dict) -> int:
    """Save user profile snapshot"""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        
        json_str = json.dumps(profile_data)
        print(f"[DB] Saving profile: user_id={user_id}, website='{profile_data.get('website')}', location='{profile_data.get('location')}'")
        print(f"[DB] JSON to save: {json_str[:200]}...")  # First 200 chars
        
        cursor.execute("""
            INSERT INTO user_profiles (user_id, profile_data, created_at)
            VALUES (?, ?, ?)
        """, (user_id, json_str, now))
        
        print(f"[DB] Saved successfully")
        return cursor.lastrowid

def get_latest_user_profile(user_id: int) -> Optional[Dict]:
    """Get latest profile snapshot"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT profile_data, created_at 
            FROM user_profiles 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 1
        """, (user_id,))
        
        row = cursor.fetchone()
        if row:
            loaded_data = json.loads(row['profile_data'])
            print(f"[DB] Loaded profile: website='{loaded_data.get('website')}', location='{loaded_data.get('location')}'")
            print(f"[DB] All keys in loaded data: {list(loaded_data.keys())}")
            return {
                'data': loaded_data,
                'created_at': row['created_at']
            }
        return None

def get_user_profile_history(user_id: int, limit: int = 10) -> list:
    """Get profile snapshots over time"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT profile_data, created_at 
            FROM user_profiles 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT ?
        """, (user_id, limit))
        
        rows = cursor.fetchall()
        profiles = []
        
        for row in rows:
            profiles.append({
                'data': json.loads(row['profile_data']),
                'created_at': row['created_at']
            })
        
        return profiles

# ============ CONNECTED ACCOUNTS OPERATIONS ============

def connect_account(user_id: int, platform: str, platform_user_id: str = None, 
                   platform_username: str = None, access_token: str = None,
                   refresh_token: str = None, token_expires_at: str = None,
                   metadata: dict = None) -> bool:
    """Connect or update a platform account for a user"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            
            # Check if connection exists
            cursor.execute("""
                SELECT id FROM connected_accounts 
                WHERE user_id = ? AND platform = ?
            """, (user_id, platform))
            
            existing = cursor.fetchone()
            metadata_json = json.dumps(metadata) if metadata else None
            
            if existing:
                # Update existing connection
                cursor.execute("""
                    UPDATE connected_accounts 
                    SET platform_user_id = ?, platform_username = ?,
                        access_token = ?, refresh_token = ?, token_expires_at = ?,
                        last_synced_at = ?, is_active = 1, metadata = ?
                    WHERE user_id = ? AND platform = ?
                """, (platform_user_id, platform_username, access_token, refresh_token,
                      token_expires_at, now, metadata_json, user_id, platform))
            else:
                # Insert new connection
                cursor.execute("""
                    INSERT INTO connected_accounts 
                    (user_id, platform, platform_user_id, platform_username,
                     access_token, refresh_token, token_expires_at, connected_at,
                     last_synced_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, platform, platform_user_id, platform_username,
                      access_token, refresh_token, token_expires_at, now, now,
                      metadata_json))
            
            return True
    except Exception as e:
        print(f"Error connecting account: {e}")
        return False

def disconnect_account(user_id: int, platform: str) -> bool:
    """Disconnect a platform account"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE connected_accounts 
                SET is_active = 0
                WHERE user_id = ? AND platform = ?
            """, (user_id, platform))
            return True
    except Exception as e:
        print(f"Error disconnecting account: {e}")
        return False

def get_connected_accounts(user_id: int) -> list:
    """Get all connected accounts for a user"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT platform, platform_user_id, platform_username,
                   connected_at, last_synced_at, is_active, metadata
            FROM connected_accounts
            WHERE user_id = ?
        """, (user_id,))
        
        accounts = []
        for row in cursor.fetchall():
            account = dict(row)
            if account.get('metadata'):
                try:
                    account['metadata'] = json.loads(account['metadata'])
                except:
                    account['metadata'] = {}
            accounts.append(account)
        return accounts

def get_account_connection(user_id: int, platform: str) -> Optional[Dict[str, Any]]:
    """Get a specific connected account"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM connected_accounts
            WHERE user_id = ? AND platform = ? AND is_active = 1
        """, (user_id, platform))
        
        row = cursor.fetchone()
        if row:
            account = dict(row)
            if account.get('metadata'):
                try:
                    account['metadata'] = json.loads(account['metadata'])
                except:
                    account['metadata'] = {}
            return account
        return None
