"""Verified public ATS sources for broad Tier 1 crawling.

The catalog contains public, read-only job-board endpoints that do not require
an account or an authenticated browser session.  Every entry was probed on
2026-07-31 and returned a valid ATS payload.  A valid board with zero current
openings remains useful: the production crawler records it as VERIFIED_EMPTY.

The acceptance runner re-probes the endpoints through the production fetcher;
this snapshot is a durable seed list, not a claim that every company will
always remain on the same ATS.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PublicJobSource:
    company_name: str
    source_type: str
    source_identifier: str

    @property
    def base_url(self) -> str:
        if self.source_type == "greenhouse":
            return (
                "https://boards-api.greenhouse.io/v1/boards/"
                f"{self.source_identifier}/jobs?content=true"
            )
        if self.source_type == "lever":
            return (
                "https://api.lever.co/v0/postings/"
                f"{self.source_identifier}?mode=json"
            )
        if self.source_type == "ashby":
            return (
                "https://api.ashbyhq.com/posting-api/job-board/"
                f"{self.source_identifier}"
            )
        raise ValueError(f"unsupported public source type: {self.source_type}")


PUBLIC_JOB_SOURCES: tuple[PublicJobSource, ...] = (
    PublicJobSource("Stripe", "greenhouse", "stripe"),
    PublicJobSource("Airbnb", "greenhouse", "airbnb"),
    PublicJobSource("Coinbase", "greenhouse", "coinbase"),
    PublicJobSource("Figma", "greenhouse", "figma"),
    PublicJobSource("Datadog", "greenhouse", "datadog"),
    PublicJobSource("Brex", "greenhouse", "brex"),
    PublicJobSource("Discord", "greenhouse", "discord"),
    PublicJobSource("Vercel", "greenhouse", "vercel"),
    PublicJobSource("Scale AI", "greenhouse", "scaleai"),
    PublicJobSource("Anduril", "greenhouse", "andurilindustries"),
    PublicJobSource("Anthropic", "greenhouse", "anthropic"),
    PublicJobSource("Databricks", "greenhouse", "databricks"),
    PublicJobSource("Cloudflare", "greenhouse", "cloudflare"),
    PublicJobSource("GitLab", "greenhouse", "gitlab"),
    PublicJobSource("Samsara", "greenhouse", "samsara"),
    PublicJobSource("Toast", "greenhouse", "toast"),
    PublicJobSource("Gusto", "greenhouse", "gusto"),
    PublicJobSource("Webflow", "greenhouse", "webflow"),
    PublicJobSource("Airtable", "greenhouse", "airtable"),
    PublicJobSource("PagerDuty", "greenhouse", "pagerduty"),
    PublicJobSource("MongoDB", "greenhouse", "mongodb"),
    PublicJobSource("Cockroach Labs", "greenhouse", "cockroachlabs"),
    PublicJobSource("Lattice", "greenhouse", "lattice"),
    PublicJobSource("Elastic", "greenhouse", "elastic"),
    PublicJobSource("Twilio", "greenhouse", "twilio"),
    PublicJobSource("Netlify", "greenhouse", "netlify"),
    PublicJobSource("Dropbox", "greenhouse", "dropbox"),
    PublicJobSource("Fivetran", "greenhouse", "fivetran"),
    PublicJobSource("LaunchDarkly", "greenhouse", "launchdarkly"),
    PublicJobSource("Grafana Labs", "greenhouse", "grafanalabs"),
    PublicJobSource("Reddit", "greenhouse", "reddit"),
    PublicJobSource("Pinterest", "greenhouse", "pinterest"),
    PublicJobSource("Instacart", "greenhouse", "instacart"),
    PublicJobSource("DoorDash", "greenhouse", "doordashusa"),
    PublicJobSource("Robinhood", "greenhouse", "robinhood"),
    PublicJobSource("Mercury", "greenhouse", "mercury"),
    PublicJobSource("Tenable", "greenhouse", "tenableinc"),
    PublicJobSource("Wiz", "greenhouse", "wizinc"),
    PublicJobSource("Chainguard", "greenhouse", "chainguard"),
    PublicJobSource("Orca Security", "greenhouse", "orcasecurity"),
    PublicJobSource("Abnormal Security", "greenhouse", "abnormalsecurity"),
    PublicJobSource("Expel", "greenhouse", "expel"),
    PublicJobSource("Huntress", "greenhouse", "huntress"),
    PublicJobSource("Axonius", "greenhouse", "axonius"),
    PublicJobSource("Dragos", "greenhouse", "dragos"),
    PublicJobSource("Censys", "greenhouse", "censys"),
    PublicJobSource("Bugcrowd", "greenhouse", "bugcrowd"),
    PublicJobSource("Socket", "greenhouse", "socket"),
    PublicJobSource("Corelight", "greenhouse", "corelight"),
    PublicJobSource("ThreatLocker", "greenhouse", "threatlocker"),
    PublicJobSource("Bishop Fox", "greenhouse", "bishopfox"),
    PublicJobSource("Praetorian", "greenhouse", "praetorian"),
    PublicJobSource("Duolingo", "greenhouse", "duolingo"),
    PublicJobSource("Lyft", "greenhouse", "lyft"),
    PublicJobSource("Affirm", "greenhouse", "affirm"),
    PublicJobSource("Roblox", "greenhouse", "roblox"),
    PublicJobSource("Okta", "greenhouse", "okta"),
    PublicJobSource("Fastly", "greenhouse", "fastly"),
    PublicJobSource("Khan Academy", "greenhouse", "khanacademy"),
    PublicJobSource("Tripadvisor", "greenhouse", "tripadvisor"),
    PublicJobSource("HubSpot", "greenhouse", "hubspot"),
    PublicJobSource("Squarespace", "greenhouse", "squarespace"),
    PublicJobSource("Ashby", "ashby", "ashby"),
    PublicJobSource("Vanta", "ashby", "vanta"),
    PublicJobSource("Ramp", "ashby", "ramp"),
    PublicJobSource("Zip", "ashby", "zip"),
    PublicJobSource("Persona", "ashby", "persona"),
    PublicJobSource("Slope", "ashby", "slope"),
    PublicJobSource("Baseten", "ashby", "baseten"),
    PublicJobSource("Modal", "ashby", "modal"),
    PublicJobSource("E2B", "ashby", "e2b"),
    PublicJobSource("Confluent", "ashby", "confluent"),
    PublicJobSource("Plaid", "ashby", "plaid"),
    PublicJobSource("Snowflake", "ashby", "snowflake"),
    PublicJobSource("Notion", "ashby", "notion"),
    PublicJobSource("Sentry", "ashby", "sentry"),
    PublicJobSource("Material Security", "ashby", "materialsecurity"),
    PublicJobSource("HackerOne", "ashby", "hackerone"),
    PublicJobSource("Semgrep", "ashby", "semgrep"),
    PublicJobSource("OpenAI", "ashby", "openai"),
    PublicJobSource("ElevenLabs", "ashby", "elevenlabs"),
    PublicJobSource("Deel", "ashby", "deel"),
    PublicJobSource("Cursor", "ashby", "cursor"),
    PublicJobSource("Perplexity", "ashby", "perplexity"),
    PublicJobSource("Harvey", "ashby", "harvey"),
    PublicJobSource("Pinecone", "ashby", "pinecone"),
    PublicJobSource("PostHog", "ashby", "posthog"),
    PublicJobSource("Cohere", "ashby", "cohere"),
    PublicJobSource("LangChain", "ashby", "langchain"),
    PublicJobSource("Character AI", "ashby", "character"),
    PublicJobSource("Crusoe", "ashby", "crusoe"),
    PublicJobSource("Hightouch", "ashby", "hightouch"),
    PublicJobSource("Pylon", "ashby", "pylon"),
    PublicJobSource("Mercor", "ashby", "mercor"),
    PublicJobSource("Decagon", "ashby", "decagon"),
    PublicJobSource("Anyscale", "ashby", "anyscale"),
    PublicJobSource("Mistral", "lever", "mistral"),
    PublicJobSource("Palantir", "lever", "palantir"),
    PublicJobSource("Whoop", "lever", "whoop"),
    PublicJobSource("Highspot", "lever", "highspot"),
    PublicJobSource("BenchSci", "lever", "benchsci"),
    PublicJobSource("HighLevel", "lever", "gohighlevel"),
)


def validate_public_source_catalog() -> None:
    """Fail fast if the durable seed list contains a duplicate identity."""
    identities = [
        (source.source_type, source.source_identifier)
        for source in PUBLIC_JOB_SOURCES
    ]
    if len(identities) < 100:
        raise ValueError("public source catalog must contain at least 100 sources")
    if len(set(identities)) != len(identities):
        raise ValueError("public source catalog contains duplicate ATS identities")
