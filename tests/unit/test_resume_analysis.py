from careerops.application.resume_analysis import (
    SKILL_CATEGORIES,
    detect_level,
    detect_skills,
    parse_job,
    review,
)


class TestSkillCategoriesSurface:
    def test_expected_categories_preserved(self) -> None:
        assert set(SKILL_CATEGORIES) == {
            "languages",
            "frontend",
            "backend",
            "data",
            "cloud",
            "ml_ai",
            "practices",
        }


class TestDetectSkills:
    def test_detects_python_fastapi_react_across_categories(self) -> None:
        found = detect_skills("I build APIs with Python and FastAPI, plus React on the frontend.")
        assert "python" in found["languages"]
        assert "fastapi" in found["backend"]
        assert "react" in found["frontend"]

    def test_returns_empty_dict_when_no_skills_match(self) -> None:
        assert detect_skills("just some prose without tech keywords") == {}

    def test_is_case_insensitive(self) -> None:
        found = detect_skills("EXPERIENCE WITH GOLANG AND DOCKER")
        assert "golang" in found["languages"]
        assert "docker" in found["cloud"]

    def test_word_boundary_does_not_match_inside_mysql(self) -> None:
        # "sql" (languages bucket) must not match inside "mysql" (data bucket)
        found = detect_skills("experienced with mysql databases")
        assert "sql" not in found.get("languages", [])
        assert "mysql" in found["data"]


class TestDetectLevel:
    def test_years_drive_senior(self) -> None:
        assert detect_level("Software Engineer with 6+ years of experience") == "senior"

    def test_years_drive_staff(self) -> None:
        assert detect_level("Principal with 10+ years building systems") == "staff"

    def test_years_drive_mid(self) -> None:
        assert detect_level("Did 3+ years of backend work") == "mid"

    def test_years_drive_junior(self) -> None:
        assert detect_level("Intern with 1+ years") == "junior"

    def test_keyword_fallback_senior(self) -> None:
        assert detect_level("Senior Engineer") == "senior"

    def test_keyword_fallback_staff(self) -> None:
        assert detect_level("Staff Engineer title only") == "staff"

    def test_unknown_when_no_signal(self) -> None:
        assert detect_level("a plain sentence about weather") == "unknown"


class TestParseJob:
    def test_extracts_requirements_remote_flag_and_level(self) -> None:
        job = {
            "title": "Senior Backend Engineer",
            "company": "Acme",
            "location": "Earth",
            "url": "https://acme.example/job",
            "raw_data": {"description": "Build with python and fastapi. Fully remote."},
        }
        parsed = parse_job(job)
        assert parsed["title"] == "Senior Backend Engineer"
        assert parsed["company"] == "Acme"
        assert parsed["location"] == "Earth"
        assert parsed["url"] == "https://acme.example/job"
        assert parsed["level"] == "senior"
        assert parsed["is_remote"] is True
        assert "python" in parsed["required_skills"]["languages"]
        assert "fastapi" in parsed["required_skills"]["backend"]

    def test_handles_job_without_raw_data(self) -> None:
        parsed = parse_job({"title": "Junior Dev"})
        assert parsed["level"] == "junior"
        assert parsed["required_skills"] == {}
        assert parsed["is_remote"] is False


class TestReview:
    def test_report_shape_and_positive_match_score(self) -> None:
        resume = (
            "Senior Engineer, 6+ years. I code in python and fastapi, deploy to aws, "
            "use postgres and react."
        )
        job = {
            "title": "Backend Engineer",
            "company": "Acme",
            "location": "Remote",
            "url": "https://acme.example",
            "raw_data": {"description": "Need python, fastapi, postgres. Remote."},
        }
        report = review(resume, job)
        assert report["job"]["title"] == "Backend Engineer"
        assert report["job"]["company"] == "Acme"
        assert report["job"]["remote"] is True
        assert "python" in report["matched_skills"]
        assert "fastapi" in report["matched_skills"]
        assert report["match_score"] > 0.0
        assert report["resume_skill_count"] >= 4
        assert isinstance(report["suggestions"], list)

    def test_level_shortfall_penalizes_score(self) -> None:
        # Resume reads junior (1 year), job asks senior -> level_ok False, score * 0.7
        resume = "Junior dev, 1+ years, knows python"
        job = {"title": "Senior Engineer", "company": "X", "raw_data": {"d": "python"}}
        report = review(resume, job)
        assert report["level_ok"] is False
        assert report["resume_level"] == "junior"
        assert "junior" in report["level_note"]

    def test_no_skill_overlap_yields_zero_score(self) -> None:
        resume = "I paint watercolor landscapes"
        job = {"title": "Engineer", "company": "X", "raw_data": {"d": "python fastapi"}}
        report = review(resume, job)
        assert report["match_score"] == 0.0
        assert report["matched_skills"] == []
        assert any("overlap" in s.lower() for s in report["suggestions"])
