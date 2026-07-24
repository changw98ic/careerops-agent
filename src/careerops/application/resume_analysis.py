"""Deterministic resume analysis: skill detection, level estimation, job match.

Pure functions lifted out of the legacy ``scripts/resume_review.py`` CLI so that
LangGraph nodes and application services can import them without depending on
the uninstalled ``scripts/`` package (only ``careerops.*`` is shipped, see
``pyproject.toml`` entry points). Function signatures and behavior are preserved
verbatim from the original script; only the location changed.
"""

from __future__ import annotations

import json
import re
from typing import Any

SKILL_CATEGORIES: dict[str, list[str]] = {
    "languages": [
        "python",
        "go",
        "golang",
        "rust",
        "java",
        "javascript",
        "typescript",
        "c++",
        "c#",
        "ruby",
        "php",
        "swift",
        "kotlin",
        "scala",
        "sql",
        "bash",
        "shell",
    ],
    "frontend": [
        "react",
        "vue",
        "angular",
        "svelte",
        "next.js",
        "nextjs",
        "html",
        "css",
        "tailwind",
        "redux",
        "webpack",
        "vite",
    ],
    "backend": [
        "django",
        "flask",
        "fastapi",
        "spring",
        "rails",
        "express",
        "node.js",
        "nodejs",
        "graphql",
        "rest",
        "grpc",
        "microservices",
    ],
    "data": [
        "postgres",
        "postgresql",
        "mysql",
        "mongodb",
        "redis",
        "elasticsearch",
        "kafka",
        "spark",
        "airflow",
        "dbt",
        "snowflake",
        "bigquery",
    ],
    "cloud": [
        "aws",
        "gcp",
        "azure",
        "docker",
        "kubernetes",
        "terraform",
        "ansible",
        "ci/cd",
        "github actions",
        "jenkins",
        "lambda",
        "s3",
        "ec2",
    ],
    "ml_ai": [
        "machine learning",
        "deep learning",
        "pytorch",
        "tensorflow",
        "llm",
        "nlp",
        "computer vision",
        "mlops",
        "hugging face",
        "rag",
        "agents",
    ],
    "practices": [
        "agile",
        "scrum",
        "tdd",
        "code review",
        "system design",
        "distributed systems",
        "observability",
        "monitoring",
        "testing",
        "security",
    ],
}

LEVEL_KEYWORDS: dict[str, list[str]] = {
    "senior": ["senior", "sr.", "sr ", "5+ years", "6+ years", "7+ years", "8+ years"],
    "staff": ["staff", "principal", "lead", "architect", "10+ years", "8+ years"],
    "mid": ["mid-level", "mid level", "3+ years", "4+ years"],
    "junior": [
        "junior",
        "entry",
        "graduate",
        "new grad",
        "0-2 years",
        "1+ years",
        "intern",
    ],
}

REMOTE_KEYWORDS = ["remote", "work from home", "wfh", "distributed", "anywhere", "home-based"]


def detect_skills(text: str) -> dict[str, list[str]]:
    """Detect skills present in text, grouped by category."""
    text_lower = text.lower()
    found: dict[str, list[str]] = {}
    for category, skills in SKILL_CATEGORIES.items():
        matches: list[str] = []
        for skill in skills:
            # Word-boundary-ish matching
            pattern = re.escape(skill)
            if re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", text_lower):
                matches.append(skill)
        if matches:
            found[category] = matches
    return found


def detect_level(text: str) -> str:
    """Estimate experience level from text."""
    text_lower = text.lower()
    # Check years of experience
    years_match = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", text_lower)
    max_years = max((int(y) for y in years_match), default=0)
    if max_years >= 8:
        return "staff"
    if max_years >= 5:
        return "senior"
    if max_years >= 3:
        return "mid"
    if max_years > 0:
        return "junior"
    # Fall back to keywords
    for level in ["staff", "senior", "mid", "junior"]:
        if any(kw in text_lower for kw in LEVEL_KEYWORDS[level]):
            return level
    return "unknown"


def parse_job(job: dict[str, Any]) -> dict[str, Any]:
    """Extract structured requirements from a job record."""
    title = job.get("title", "")
    raw = json.dumps(job.get("raw_data", {})).lower() if "raw_data" in job else ""
    combined = f"{title} {raw}".lower()

    required_skills = detect_skills(combined)
    level = detect_level(title)
    is_remote = any(kw in combined for kw in REMOTE_KEYWORDS)

    return {
        "title": title,
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "required_skills": required_skills,
        "level": level,
        "is_remote": is_remote,
    }


def review(resume_text: str, job: dict[str, Any]) -> dict[str, Any]:
    """Produce a match report between resume and job."""
    resume_skills = detect_skills(resume_text)
    resume_level = detect_level(resume_text)
    parsed = parse_job(job)

    # Flatten skill sets
    resume_flat = {s for skills in resume_skills.values() for s in skills}
    required_flat = {s for skills in parsed["required_skills"].values() for s in skills}

    matched = sorted(resume_flat & required_flat)
    gaps = sorted(required_flat - resume_flat)

    # Level analysis
    level_order = {"junior": 0, "mid": 1, "senior": 2, "staff": 3, "unknown": -1}
    req_level = parsed["level"]
    level_ok = True
    level_note = ""
    if level_order.get(resume_level, -1) >= 0 and level_order.get(req_level, -1) >= 0:
        if level_order[resume_level] < level_order[req_level]:
            level_ok = False
            level_note = f"Resume reads as {resume_level}, job requires {req_level}"
        else:
            level_note = f"Resume level ({resume_level}) meets job requirement ({req_level})"

    # Match score
    score = 0.0
    if required_flat:
        score = len(matched) / len(required_flat)
    if not level_ok:
        score *= 0.7

    # Suggestions
    suggestions: list[str] = []
    if gaps:
        top_gaps = gaps[:5]
        suggestions.append(f"Consider highlighting or learning: {', '.join(top_gaps)}")
    if not level_ok:
        suggestions.append(
            "Emphasize leadership/ownership examples to support the seniority requirement"
        )
    if parsed["is_remote"] and "remote" not in resume_text.lower():
        suggestions.append(
            "Job is remote — mention remote work experience or async collaboration skills"
        )
    if not matched:
        suggestions.append(
            "No direct skill overlap detected — tailor resume keywords to the job description"
        )
    if score >= 0.7:
        suggestions.append("Strong match — prioritize this application")

    return {
        "job": {
            "title": parsed["title"],
            "company": parsed["company"],
            "location": parsed["location"],
            "url": parsed["url"],
            "level": req_level,
            "remote": parsed["is_remote"],
        },
        "match_score": round(score, 2),
        "matched_skills": matched,
        "gap_skills": gaps,
        "resume_level": resume_level,
        "level_ok": level_ok,
        "level_note": level_note,
        "resume_skill_count": len(resume_flat),
        "required_skill_count": len(required_flat),
        "suggestions": suggestions,
    }
