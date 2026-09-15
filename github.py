import os
import re
import time
import requests
from typing import List, Dict, Any, Tuple
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API_URL = "https://api.github.com"

_VALID_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_TREE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_TREE_TTL = 600  # seconds


def _github_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "DevIQ/1.0",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    return headers

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


def fetch_repo_tree(owner: str, repo: str, max_nodes: int = 220, max_depth: int = 4) -> Dict[str, Any]:
    """Fetch a compact recursive file tree for a public repository.

    Returns {"owner", "repo", "branch", "truncated", "total_files",
    "total_dirs", "entries": [{"path", "type": "blob"|"tree"}], "empty"?}.
    Results are cached in-memory for _TREE_TTL seconds.
    """
    if not _VALID_NAME.match(owner or "") or not _VALID_NAME.match(repo or ""):
        raise ValueError("Invalid owner or repo name")

    key = f"{owner.lower()}/{repo.lower()}"
    now = time.time()
    hit = _TREE_CACHE.get(key)
    if hit and (now - hit[0]) < _TREE_TTL:
        return hit[1]

    headers = _github_headers()

    def _fail_for_status(resp: requests.Response, what: str) -> None:
        if resp.status_code == 404:
            raise ValueError("Repository not found")
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            raise RuntimeError("GitHub rate limit exceeded, try again later")
        if resp.status_code != 200:
            raise RuntimeError(f"GitHub API {resp.status_code} while loading {what}")

    try:
        meta_resp = requests.get(f"{GITHUB_API_URL}/repos/{owner}/{repo}", headers=headers, timeout=10)
        _fail_for_status(meta_resp, "repository info")
        default_branch = (meta_resp.json() or {}).get("default_branch") or "main"

        tree_resp = requests.get(
            f"{GITHUB_API_URL}/repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1",
            headers=headers,
            timeout=15,
        )
        if tree_resp.status_code == 409 or tree_resp.status_code == 404:
            # Empty repository (no commits yet) — trees API has nothing to return.
            data: Dict[str, Any] = {
                "owner": owner, "repo": repo, "branch": default_branch,
                "truncated": False, "total_files": 0, "total_dirs": 0,
                "entries": [], "empty": True,
            }
            _TREE_CACHE[key] = (now, data)
            return data
        _fail_for_status(tree_resp, "file tree")

        payload = tree_resp.json() or {}
        raw = payload.get("tree", []) if isinstance(payload.get("tree"), list) else []
        truncated = bool(payload.get("truncated"))

        kept: List[Dict[str, str]] = []
        files = 0
        dirs = 0
        for e in raw:
            if not isinstance(e, dict):
                continue
            path = e.get("path") or ""
            if not path or path.startswith(".git/"):
                continue
            if path.count("/") + 1 > max_depth:
                continue
            kind = "tree" if e.get("type") == "tree" else "blob"
            kept.append({"path": path, "type": kind})
            if kind == "tree":
                dirs += 1
            else:
                files += 1
            if len(kept) >= max_nodes:
                truncated = True
                break

        kept.sort(key=lambda e: (0 if e["type"] == "tree" else 1, e["path"].lower()))
        data = {
            "owner": owner, "repo": repo, "branch": default_branch,
            "truncated": truncated or len(raw) > len(kept),
            "total_files": files, "total_dirs": dirs,
            "entries": kept,
        }
        _TREE_CACHE[key] = (now, data)
        return data

    except (ValueError, RuntimeError):
        raise
    except Exception as e:
        print(f"Error fetching repo tree for {owner}/{repo}: {e}")
        raise RuntimeError(f"Failed to fetch file tree: {str(e)}")
