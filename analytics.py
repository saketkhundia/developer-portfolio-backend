from typing import List, Dict, Any
from collections import Counter

def calculate_skill_score(repos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate analytics from GitHub repositories"""
    if not repos:
        return {
            "total_projects": 0,
            "total_stars": 0,
            "recent_projects": 0,
            "skill_score": 0,
            "most_used_language": None,
            "language_distribution": {}
        }
    
    total_stars = sum(repo.get("stargazers_count", 0) for repo in repos)
    total_projects = len(repos)
    
    # Count languages
    languages = []
    for repo in repos:
        lang = repo.get("language")
        if lang:
            languages.append(lang)
    
    language_dist = dict(Counter(languages))
    most_used = max(language_dist, key=language_dist.get) if language_dist else None
    
    # Calculate skill score (0-100)
    # Based on stars, forks, and language diversity
    avg_stars = total_stars / max(total_projects, 1)
    avg_forks = sum(repo.get("forks_count", 0) for repo in repos) / max(total_projects, 1)
    language_score = min(len(language_dist) * 10, 30)  # Up to 30 points for language diversity
    
    # Star-based score (weighted)
    stars_score = min((avg_stars / 10) * 50, 50)  # Up to 50 points for average stars
    forks_score = min((avg_forks / 5) * 20, 20)   # Up to 20 points for average forks
    
    skill_score = round(stars_score + forks_score + language_score)
    skill_score = min(100, max(0, skill_score))  # Clamp to 0-100
    
    return {
        "total_projects": total_projects,
        "total_stars": total_stars,
        "recent_projects": total_projects,
        "skill_score": skill_score,
        "most_used_language": most_used,
        "language_distribution": language_dist
    }
