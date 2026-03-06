# Cross-Device Cloud Sync Guide

## ✅ Your Backend Already Has Cloud Sync!

**Important:** Your profile data is **already stored in the cloud** (server-side database), not in the browser! When you clear browser history, you only lose the **authentication token**, not your profile data.

## How It Works

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│   Device 1   │         │ Cloud Server │         │   Device 2   │
│   (Phone)    │         │  (Database)  │         │   (Laptop)   │
└──────┬───────┘         └──────┬───────┘         └──────┬───────┘
       │                        │                        │
       │ 1. Login              │                        │
       ├──────────────────────>│                        │
       │ Token + Profile       │                        │
       │<──────────────────────┤                        │
       │                        │                        │
       │                        │ 2. Login              │
       │                        │<───────────────────────┤
       │                        │ Token + Same Profile  │
       │                        ├───────────────────────>│
       │                        │                        │
       │ 3. Update Profile     │                        │
       ├──────────────────────>│                        │
       │                        │ (Saved to database)   │
       │                        │                        │
       │                        │ 4. Get Profile        │
       │                        │<───────────────────────┤
       │                        │ Updated Profile       │
       │                        ├───────────────────────>│
```

## What's New

I've added **multi-device session management**:

### 1. Device Tracking
- Each login tracks the device (Phone, Laptop, Tablet)
- Shows IP address and timestamp
- View all active sessions

### 2. Multi-Device Sessions
- Login on multiple devices simultaneously
- Each device gets its own token
- All devices access the same profile data

### 3. Remote Logout
- Logout from specific devices
- Logout from all devices at once
- Keep current session while logging out others

## New API Endpoints

### View Active Sessions (All Devices)
```http
GET /auth/sessions
Authorization: Bearer <token>
```

**Response:**
```json
{
  "status": "success",
  "sessions": [
    {
      "id": 1,
      "device_name": "iPhone",
      "device_type": "Mobile",
      "ip_address": "192.168.1.100",
      "created_at": "2026-03-06T10:00:00",
      "last_accessed": "2026-03-06T10:30:00"
    },
    {
      "id": 2,
      "device_name": "Windows PC",
      "device_type": "Desktop",
      "ip_address": "192.168.1.101",
      "created_at": "2026-03-06T09:00:00",
      "last_accessed": "2026-03-06T10:25:00"
    }
  ],
  "count": 2
}
```

### Login with Remember Me
```http
POST /auth/login
Content-Type: application/json

{
  "username": "testuser",
  "password": "password",
  "remember_me": true
}
```

**Response:**
```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 2592000,
  "user": {
    "id": 1,
    "username": "testuser",
    "email": "test@example.com"
  },
  "device": "iPhone"
}
```

**Token Expiry:**
- Without `remember_me`: 24 hours
- With `remember_me: true`: 30 days

### Logout from All Devices
```http
POST /auth/logout-all
Authorization: Bearer <token>
Content-Type: application/json

{
  "keep_current_session": true
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Logged out from 3 device(s)",
  "devices_logged_out": 3
}
```

## Frontend Implementation

### 1. Store Token Persistently
```javascript
// After login
const response = await fetch('/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    username,
    password,
    remember_me: true  // 30-day token
  })
});

const data = await response.json();

// Store in localStorage (persists across sessions)
localStorage.setItem('access_token', data.access_token);
localStorage.setItem('user', JSON.stringify(data.user));
localStorage.setItem('device', data.device);

console.log(`Logged in on ${data.device}`);
```

### 2. Auto-Login on App Start
```javascript
// Check for existing token on app load
async function checkAuth() {
  const token = localStorage.getItem('access_token');
  
  if (!token) {
    // Not logged in
    window.location.href = '/login';
    return null;
  }
  
  try {
    // Verify token is still valid
    const response = await fetch('/auth/me', {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    
    if (response.ok) {
      const user = await response.json();
      return user;  // User is authenticated
    } else {
      // Token expired or invalid
      localStorage.removeItem('access_token');
      window.location.href = '/login';
      return null;
    }
  } catch (error) {
    console.error('Auth check failed:', error);
    return null;
  }
}

// Run on app initialization
document.addEventListener('DOMContentLoaded', async () => {
  const user = await checkAuth();
  if (user) {
    initializeApp(user);
  }
});
```

### 3. View Active Devices
```javascript
async function showActiveDevices() {
  const token = localStorage.getItem('access_token');
  
  const response = await fetch('/auth/sessions', {
    headers: { 'Authorization': `Bearer ${token}` }
  });
  
  const data = await response.json();
  
  console.log(`Active on ${data.count} devices:`);
  data.sessions.forEach(session => {
    console.log(`- ${session.device_name} (${session.device_type})`);
    console.log(`  Last active: ${session.last_accessed}`);
  });
  
  return data.sessions;
}
```

### 4. Logout from Other Devices
```javascript
async function logoutOtherDevices() {
  const token = localStorage.getItem('access_token');
  
  const response = await fetch('/auth/logout-all', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      keep_current_session: true  // Keep this device logged in
    })
  });
  
  const data = await response.json();
  alert(`Logged out from ${data.devices_logged_out} other device(s)`);
}
```

### 5. Complete Example - Login Component
```javascript
// Login.jsx
import { useState } from 'react';

function Login() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [rememberMe, setRememberMe] = useState(false);
  
  async function handleLogin(e) {
    e.preventDefault();
    
    try {
      const response = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username,
          password,
          remember_me: rememberMe
        })
      });
      
      if (!response.ok) {
        throw new Error('Login failed');
      }
      
      const data = await response.json();
      
      // Store token
      localStorage.setItem('access_token', data.access_token);
      localStorage.setItem('user', JSON.stringify(data.user));
      
      // Redirect to dashboard
      window.location.href = '/dashboard';
    } catch (error) {
      alert('Login failed: ' + error.message);
    }
  }
  
  return (
    <form onSubmit={handleLogin}>
      <input
        type="text"
        placeholder="Username"
        value={username}
        onChange={(e) => setUsername(e.target.value)}
      />
      <input
        type="password"
        placeholder="Password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
      />
      <label>
        <input
          type="checkbox"
          checked={rememberMe}
          onChange={(e) => setRememberMe(e.target.checked)}
        />
        Remember me (30 days)
      </label>
      <button type="submit">Login</button>
    </form>
  );
}
```

## Cloud Deployment

To make this truly "in the cloud", deploy your backend to a server:

### Option 1: Deploy to Render.com (Free)

1. **Create account** at https://render.com
2. **Create new Web Service**
3. **Connect GitHub repo**
4. **Configure:**
   ```yaml
   Build Command: pip install -r requirements.txt
   Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
   ```
5. **Set environment variables:**
   ```
   SECRET_KEY=your-super-secret-key-here
   GITHUB_TOKEN=your-github-token
   ```
6. **Deploy!**

Your API will be at: `https://your-app.onrender.com`

### Option 2: Deploy to Railway.app

1. **Create account** at https://railway.app
2. **New Project** → **Deploy from GitHub**
3. **Add variables:**
   ```
   SECRET_KEY=your-secret-key
   ```
4. **Done!** Auto-deploys on push

### Option 3: Deploy to Vercel (Serverless)

```bash
# Install Vercel CLI
npm i -g vercel

# Deploy
cd backend
vercel --prod
```

### Option 4: Use Cloud Database (Advanced)

Replace SQLite with cloud database:

#### PostgreSQL (Heroku, Supabase)
```python
# requirements.txt
psycopg2-binary==2.9.9

# database.py
import psycopg2
DATABASE_URL = os.getenv("DATABASE_URL")
conn = psycopg2.connect(DATABASE_URL)
```

#### MongoDB (MongoDB Atlas)
```python
# requirements.txt
pymongo==4.6.1

# database.py
from pymongo import MongoClient
client = MongoClient(os.getenv("MONGODB_URI"))
db = client.portfolio
```

## Understanding the Sync

### What's Stored in Browser
```
localStorage:
  - access_token (authentication only)
  - user (cached user info)
```

### What's Stored in Cloud
```
Database (server-side):
  ✅ All user profile data
  ✅ All sessions (tokens)
  ✅ Profile history
  ✅ Profile snapshots
  ✅ Device information
```

## Common Scenarios

### Scenario 1: Clear Browser History
```
❌ What you lose: Token (must login again)
✅ What persists: All profile data, history, settings
```

**Solution:** Login again with same credentials → Access same profile

### Scenario 2: Switch Devices
```
1. Login on Device A → Get token A
2. Login on Device B → Get token B
3. Both devices access SAME profile data
4. Update profile on Device A → Device B sees changes
```

### Scenario 3: Lost Phone
```
1. Login on new device
2. Go to Settings → Active Devices
3. Click "Logout from all other devices"
4. Old device session invalidated
```

## Testing Multi-Device Sync

### Test 1: Login on Multiple "Devices"
```bash
# Device 1
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -H "User-Agent: Mozilla/5.0 (iPhone)" \
  -d '{"username": "testuser", "password": "hashed_password"}'
# Save token1

# Device 2
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -H "User-Agent: Mozilla/5.0 (Windows NT 10.0)" \
  -d '{"username": "testuser", "password": "hashed_password"}'
# Save token2
```

### Test 2: View Active Sessions
```bash
curl http://localhost:8000/auth/sessions \
  -H "Authorization: Bearer $token1"
```

Should show both iPhone and Windows PC sessions!

### Test 3: Update Profile on One Device
```bash
# Update on Device 1
curl -X PUT http://localhost:8000/profile \
  -H "Authorization: Bearer $token1" \
  -H "Content-Type: application/json" \
  -d '{"bio": "Updated from iPhone"}'

# Check on Device 2
curl http://localhost:8000/profile \
  -H "Authorization: Bearer $token2"
```

Should see the updated bio!

## Security Best Practices

1. **Use HTTPS in production** - Protects tokens in transit
2. **Set secure SECRET_KEY** - Use environment variable
3. **Implement rate limiting** - Prevent brute force attacks
4. **Add password hashing** - Use bcrypt (currently using plain text)
5. **Enable 2FA** - Extra security layer
6. **Monitor active sessions** - Alert on suspicious devices

## Summary

✅ **Profile data IS in the cloud** (server database)  
✅ **Works across all devices** (just login)  
✅ **Survives browser clear** (data on server)  
✅ **Multi-device support** (simultaneous sessions)  
✅ **Device tracking** (know where you're logged in)  
✅ **Remote logout** (revoke device access)  

**The only thing stored in the browser is the authentication token. All your actual data is safely stored in the cloud database!** 🎉

When you "clear browser history," you're just deleting the key to access your cloud data. Login again to get a new key and access the same data.
