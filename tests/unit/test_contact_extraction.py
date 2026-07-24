import tempfile
from pathlib import Path

from careerops.application.contact_extraction import (
    NOISE_DOMAINS,
    classify_post_type,
    detect_platform,
    extract_context,
    extract_from_file,
    has_recruiting_context,
    infer_company_hint,
    is_noise,
)


class TestIsNoise:
    def test_rejects_example_domains(self) -> None:
        assert is_noise("user@example.com", "we are hiring") is True
        assert "example.com" in NOISE_DOMAINS

    def test_rejects_noreply_local_parts(self) -> None:
        assert is_noise("noreply@acme.com", "hiring context") is True

    def test_keeps_plausible_address(self) -> None:
        assert is_noise("jobs@acme.io", "we are hiring engineers") is False

    def test_rejects_image_asset_false_positive(self) -> None:
        assert is_noise("logo.png@2x", "hiring banner") is True


class TestHasRecruitingContext:
    def test_hiring_keyword_present(self) -> None:
        assert has_recruiting_context("We are hiring a backend engineer") is True

    def test_no_recruiting_signal(self) -> None:
        assert has_recruiting_context("discount shoes on sale now") is False


class TestClassifyPostType:
    def test_hiring_tag_classifies_as_hiring(self) -> None:
        text = "[Hiring] Senior Engineer. Email jobs@acme.io"
        assert classify_post_type(text, text.index("jobs@acme.io")) == "hiring"

    def test_for_hire_tag_classifies_as_for_hire(self) -> None:
        text = "[For Hire] Junior dev looking for work. me@dev.io"
        assert classify_post_type(text, text.index("me@dev.io")) == "for_hire"

    def test_returns_none_without_tag(self) -> None:
        assert classify_post_type("no flair here at all", 5) is None


class TestExtractContext:
    def test_returns_collapsed_window_around_match(self) -> None:
        text = "x" * 500 + "TARGET" + "y" * 500
        snippet = extract_context(text, 500, 506)
        assert "TARGET" in snippet
        # window=160 each side, collapsed; bounded well below full text
        assert len(snippet) <= 326


class TestDetectPlatform:
    def test_x_queries_map_to_x_twitter(self) -> None:
        assert detect_platform("x_query_foo.txt") == "x_twitter"

    def test_x_deep_maps_to_x_twitter(self) -> None:
        assert detect_platform("x_deep_bar.txt") == "x_twitter"

    def test_reddit_maps_to_subreddit(self) -> None:
        assert detect_platform("reddit_forhire.txt") == "reddit:r/forhire"

    def test_unknown_filename(self) -> None:
        assert detect_platform("random.txt") == "unknown"


class TestInferCompanyHint:
    def test_hiring_company_prefix_is_greedy_display_hint(self) -> None:
        # The hint regex is intentionally greedy across letters/spaces (display
        # only). Lock the verbatim behavior extracted from the legacy script.
        ctx = "Hiring Company: Stripe is looking for engineers"
        assert infer_company_hint(ctx) == "Stripe is looking for engineers"

    def test_returns_company_when_terminated_by_punctuation(self) -> None:
        ctx = "Hiring Company: Stripe."
        assert infer_company_hint(ctx) == "Stripe"

    def test_empty_when_no_signal(self) -> None:
        assert infer_company_hint("just a sentence") == ""


class TestExtractFromFile:
    def test_extracts_public_recruiting_email_with_provenance(self) -> None:
        text = "[Hiring] Senior Engineer at Stripe. Send CV to jobs@stripe.example"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reddit_forhire.txt"
            path.write_text(text)
            results = extract_from_file(path)
        assert len(results) == 1
        only = results[0]
        assert only["email"] == "jobs@stripe.example"
        assert only["platform"] == "reddit:r/forhire"
        assert only["publicly_listed"] is True
        assert only["source_file"] == "reddit_forhire.txt"
        assert only["post_type"] == "hiring"

    def test_skips_noise_addresses(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x_query_test.txt"
            path.write_text("hiring! contact noreply@acme.com for details")
            assert extract_from_file(path) == []

    def test_skips_non_recruiting_context(self) -> None:
        # real-looking address but no recruiting signal around it.
        # Local part chosen to avoid containing any recruiting hint substring.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x_query_test.txt"
            path.write_text("random note: drop a line to team@acme.io maybe")
            assert extract_from_file(path) == []

    def test_skips_for_hire_posts_in_forhire_file(self) -> None:
        # r/forhire file, but email sits under a [For Hire] tag -> skipped
        text = "[For Hire] I am looking for work. reach me at dev@dev.io"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reddit_forhire.txt"
            path.write_text(text)
            assert extract_from_file(path) == []

    def test_dedups_repeated_emails(self) -> None:
        text = "[Hiring] Eng. email jobs@acme.io or jobs@acme.io again"
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "reddit_forhire.txt"
            path.write_text(text)
            results = extract_from_file(path)
        assert len(results) == 1
