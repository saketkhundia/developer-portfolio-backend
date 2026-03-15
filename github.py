# github.py - replace your current file with this
import requests
import os

def get_headers():
    token = os.getenv("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    return headers

def fetch_github_data(username: str):
    all_repos = []
    page = 1

    while True:
        url = f"https://api.github.com/users/{username}/repos?per_page=100&page={page}"
        response = requests.get(url, headers=get_headers(), timeout=10)

        if response.status_code == 403:
            raise ValueError("GitHub rate limit exceeded. Set GITHUB_TOKEN in Render environment variables.")
        elif response.status_code == 404:
            raise ValueError(f"GitHub user '{username}' not found")
        elif response.status_code != 200:
            raise ValueError(f"GitHub API error {response.status_code}: {response.text}")

        repos = response.json()
        if not isinstance(repos, list):
            raise ValueError(f"Unexpected GitHub response: {repos}")
        if not repos:
            break

        for repo in repos:
            all_repos.append({
                "name":        repo["name"],
                "stars":       repo["stargazers_count"],
                "forks":       repo["forks_count"],
                "language":    repo["language"],
                "description": repo.get("description") or "",
                "topics":      repo.get("topics") or [],
                "updated_at":  repo["updated_at"],
            })
        page += 1

    return all_repos