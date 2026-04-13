import os
import requests
from typing import List, Dict, Any
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API_URL = "https://api.github.com"

def fetch_github_data(username: str) -> List[Dict[str, Any]]:
    """Fetch GitHub user data and repositories"""
    if not GITHUB_TOKEN:
        print("⚠️ Warning: GITHUB_TOKEN not set. GitHub data fetching won't work.")
        return []
    
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    try:
        # Fetch user info
        user_url = f"{GITHUB_API_URL}/users/{username}"
        user_resp = requests.get(user_url, headers=headers, timeout=10)
        if user_resp.status_code != 200:
            return []
        
        # Fetch repositories
        repos_url = f"{GITHUB_API_URL}/users/{username}/repos?per_page=100&sort=stars&order=desc"
        repos_resp = requests.get(repos_url, headers=headers, timeout=10)
        if repos_resp.status_code != 200:
            return []
        
        repos = repos_resp.json()
        return repos if isinstance(repos, list) else []
    
    except Exception as e:
        print(f"Error fetching GitHub data for {username}: {e}")
        return []
