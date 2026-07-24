"""Application email drafting: generate ``(subject, body)`` for an application email.

Pure function lifted out of the legacy ``scripts/email_apply.py`` CLI. This is the
public API consumed by application services and (future) LangGraph draft nodes;
it performs no I/O and no interactive ``input()``. Behavior is preserved verbatim
from the original ``generate_body``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from careerops.application.resume_analysis import detect_skills


def generate_body(job: dict[str, Any], resume_text: str) -> tuple[str, str]:
    """Generate (subject, body) for an application email."""
    title = job.get("title", "the position")
    company = job.get("company", "your team")

    resume_skills = detect_skills(resume_text)
    job_skills = detect_skills(json.dumps(job.get("raw_data", {})) + " " + title)
    resume_flat = {s for skills in resume_skills.values() for s in skills}
    job_flat = {s for skills in job_skills.values() for s in skills}
    matched = sorted(resume_flat & job_flat)
    highlight = ", ".join(matched[:5]) if matched else ", ".join(sorted(resume_flat)[:5])

    years_match = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", resume_text.lower())
    years = f"over {max(int(y) for y in years_match)} years of " if years_match else ""

    subject = f"Application: {title}"
    body = f"""Dear {company} Hiring Team,

I am writing to apply for the {title} role.

With {years}experience in software engineering, I have built strong expertise in
{highlight}. My background aligns well with this role, and I am confident I can
contribute meaningfully to {company} from day one.

In previous roles I have delivered high-impact work end-to-end, collaborated
across functions, and taken ownership of complex problems. I am drawn to
{company}'s mission and the chance to grow with an ambitious team.

My resume is attached. I would welcome the opportunity to discuss how my
experience can benefit {company}.

Thank you for your time and consideration.

Best regards,
[Your Name]
"""
    return subject, body
