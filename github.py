# github.py - replace your current file with this
import os
from datetime import datetime, timezone

import requests

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
                "has_issues":  repo.get("has_issues", False),
                "open_issues_count": repo.get("open_issues_count", 0),
                "default_branch": repo.get("default_branch") or "main",
                "archived": repo.get("archived", False),
                "pushed_at": repo.get("pushed_at"),
                "updated_at":  repo["updated_at"],
            })
        page += 1

    return all_repos


def _get_repo_json(owner: str, repo_name: str, path: str):
    url = f"https://api.github.com/repos/{owner}/{repo_name}{path}"
    response = requests.get(url, headers=get_headers(), timeout=10)
    if response.status_code == 404:
        return None
    if response.status_code == 403:
        raise ValueError("GitHub rate limit exceeded. Set GITHUB_TOKEN in Render environment variables.")
    if response.status_code != 200:
        return None
    return response.json()


def _has_test_hints(root_items):
    if not isinstance(root_items, list):
        return False
    names = [str(item.get("name", "")).lower() for item in root_items]
    explicit_dirs = {"tests", "test", "__tests__", "spec", "specs"}
    explicit_files = {
        "pytest.ini", "tox.ini", "jest.config.js", "jest.config.ts", "vitest.config.ts",
        "vitest.config.js", "mocha.opts", "cypress.config.ts", "cypress.config.js"
    }
    return any(name in explicit_dirs for name in names) or any(name in explicit_files for name in names)


def _has_ci_hints(owner: str, repo_name: str, root_items):
    if not isinstance(root_items, list):
        return False
    names = [str(item.get("name", "")).lower() for item in root_items]
    ci_root_files = {".travis.yml", "azure-pipelines.yml", "jenkinsfile", "circle.yml", ".gitlab-ci.yml"}
    if any(name in ci_root_files for name in names):
        return True
    if ".github" in names:
        workflows = _get_repo_json(owner, repo_name, "/contents/.github/workflows")
        return isinstance(workflows, list) and len(workflows) > 0
    return False


def _issue_hygiene_score(repo):
    if repo.get("archived"):
        return 20
    if repo.get("has_issues") is False:
        return 60
    open_issues = int(repo.get("open_issues_count") or 0)
    if open_issues == 0:
        return 100
    if open_issues <= 5:
        return 90
    if open_issues <= 15:
        return 70
    if open_issues <= 30:
        return 45
    return 25


def _recency_score(iso_date: str | None):
    if not iso_date:
        return 20
    try:
        last = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        age_days = max(0, (datetime.now(timezone.utc) - last).days)
    except Exception:
        return 20
    if age_days <= 14:
        return 100
    if age_days <= 30:
        return 90
    if age_days <= 60:
        return 75
    if age_days <= 120:
        return 55
    if age_days <= 240:
        return 35
    return 15


def _commit_consistency_score(owner: str, repo_name: str, repo):
    commits = _get_repo_json(owner, repo_name, "/commits?per_page=12")
    if not isinstance(commits, list) or len(commits) == 0:
        return _recency_score(repo.get("pushed_at") or repo.get("updated_at"))

    dates = []
    for commit in commits:
        commit_date = (((commit or {}).get("commit") or {}).get("author") or {}).get("date")
        if commit_date:
            try:
                dates.append(datetime.fromisoformat(commit_date.replace("Z", "+00:00")))
            except Exception:
                pass
    if not dates:
        return _recency_score(repo.get("pushed_at") or repo.get("updated_at"))

    unique_weeks = len({f"{d.isocalendar().year}-{d.isocalendar().week}" for d in dates})
    spread_ratio = unique_weeks / max(1, min(len(dates), 8))
    cadence_score = min(100, round(spread_ratio * 100))
    recency = _recency_score(dates[0].isoformat())
    return round(cadence_score * 0.6 + recency * 0.4)


def analyze_repo_quality(username: str, repos: list[dict]):
    selected = [repo for repo in repos if not repo.get("archived")][:]
    selected.sort(key=lambda repo: ((repo.get("stars") or 0) + (repo.get("forks") or 0), repo.get("pushed_at") or repo.get("updated_at") or ""), reverse=True)
    selected = selected[:8]

    if not selected:
        return {
            "score": 0,
            "readmePct": 0,
            "testsPct": 0,
            "ciPct": 0,
            "issuePct": 0,
            "commitPct": 0,
            "analyzed": 0,
            "grade": "No Data",
            "topRepos": [],
        }

    repo_results = []
    for repo in selected:
        name = repo["name"]
        readme_exists = _get_repo_json(username, name, "/readme") is not None
        root_items = _get_repo_json(username, name, "/contents")
        has_tests = _has_test_hints(root_items)
        has_ci = _has_ci_hints(username, name, root_items)
        issue_score = _issue_hygiene_score(repo)
        commit_score = _commit_consistency_score(username, name, repo)

        maturity_score = round(
            (100 if readme_exists else 0) * 0.22 +
            (100 if has_tests else 0) * 0.24 +
            (100 if has_ci else 0) * 0.20 +
            issue_score * 0.14 +
            commit_score * 0.20
        )

        repo_results.append({
            "name": name,
            "score": maturity_score,
            "readme": readme_exists,
            "tests": has_tests,
            "ci": has_ci,
            "issueScore": issue_score,
            "commitScore": commit_score,
            "openIssues": int(repo.get("open_issues_count") or 0),
            "updatedAt": repo.get("pushed_at") or repo.get("updated_at"),
        })

    n = len(repo_results)
    readme_pct = round(sum(1 for repo in repo_results if repo["readme"]) / n * 100)
    tests_pct = round(sum(1 for repo in repo_results if repo["tests"]) / n * 100)
    ci_pct = round(sum(1 for repo in repo_results if repo["ci"]) / n * 100)
    issue_pct = round(sum(repo["issueScore"] for repo in repo_results) / n)
    commit_pct = round(sum(repo["commitScore"] for repo in repo_results) / n)
    score = round(readme_pct * 0.22 + tests_pct * 0.24 + ci_pct * 0.20 + issue_pct * 0.14 + commit_pct * 0.20)

    grade = "Mature" if score >= 85 else "Growing" if score >= 70 else "Developing" if score >= 50 else "Early"
    repo_results.sort(key=lambda repo: repo["score"], reverse=True)

    return {
        "score": score,
        "readmePct": readme_pct,
        "testsPct": tests_pct,
        "ciPct": ci_pct,
        "issuePct": issue_pct,
        "commitPct": commit_pct,
        "analyzed": n,
        "grade": grade,
        "topRepos": repo_results[:3],
    }