from careerops.application.email_drafting import generate_body


class TestGenerateBodyContract:
    def test_returns_subject_and_body_strings(self) -> None:
        job = {"title": "Backend Engineer", "company": "Acme", "raw_data": {}}
        subject, body = generate_body(job, "I know python and fastapi, 5+ years")
        assert isinstance(subject, str)
        assert isinstance(body, str)
        assert subject == "Application: Backend Engineer"

    def test_subject_uses_default_title_when_missing(self) -> None:
        subject, _body = generate_body({}, "")
        assert subject == "Application: the position"


class TestGenerateBodyContent:
    def test_body_addresses_company_and_role(self) -> None:
        job = {"title": "Staff SRE", "company": "Globex", "raw_data": {}}
        _subject, body = generate_body(job, "python, aws, kubernetes, 8+ years")
        assert "Globex" in body
        assert "Staff SRE" in body

    def test_body_surfaces_matched_skills_in_highlight(self) -> None:
        resume = "I build with python, fastapi, postgres and react"
        job = {
            "title": "Engineer",
            "company": "Initech",
            "raw_data": {"description": "python fastapi postgres"},
        }
        _subject, body = generate_body(job, resume)
        assert "python" in body

    def test_falls_back_to_resume_skills_when_no_overlap(self) -> None:
        resume = "I know python and rust"
        job = {"title": "Designer", "company": "Foo", "raw_data": {"description": "figma"}}
        _subject, body = generate_body(job, resume)
        assert "python" in body or "rust" in body

    def test_includes_years_phrase_when_resume_states_experience(self) -> None:
        _subject, body = generate_body({"title": "Eng", "company": "Bar"}, "7+ years of Go")
        assert "over 7 years of experience" in body
