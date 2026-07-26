"""ATS adapter contracts and implementations (M1.5-M1.6).

Each adapter implements detect/list_jobs/fetch_job contract,
saving raw response hash, fetched_at, source URL and parser version.
"""

# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from xml.etree import ElementTree  # nosec B405 -- guarded by _UNSAFE_XML_RE pre-parse check


@dataclass(frozen=True, slots=True)
class RawJobRecord:
    """A raw job record from an adapter before normalization."""

    external_id: str
    title: str
    location: str = ""
    url: str = ""
    description: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterFetchResult:
    """Result of fetching jobs from a source."""

    jobs: tuple[RawJobRecord, ...] = ()
    response_hash: str = ""
    fetched_at: datetime | None = None
    source_url: str = ""
    parser_version: str = ""


class JobSourceAdapter(Protocol):
    """Contract for all job source adapters."""

    @property
    def source_type(self) -> str: ...

    @property
    def parser_version(self) -> str: ...

    def detect(self, base_url: str) -> bool: ...

    def list_jobs(self, response_data: object) -> AdapterFetchResult: ...


class DetailJobSourceAdapter(Protocol):
    """Contract for adapters that parse a single job's detail response.

    Only adapters backed by a per-job detail endpoint implement this. The
    adapter parses the detail response only; it holds no HTTP client and does
    not own provenance (``source_url``/``fetched_at``/``response_hash`` are
    attached by the outer ``FetchedResponse`` layer). ``JobSourceAdapter``
    remains list-only -- JsonLd/Sitemap/StaticHtml and the Greenhouse/Lever/
    Ashby list adapters are not forced to implement ``fetch_job``.
    """

    @property
    def source_type(self) -> str: ...

    @property
    def parser_version(self) -> str: ...

    def fetch_job(
        self,
        detail_response: object,
        *,
        source_url: str,
        fetched_at: datetime,
    ) -> RawJobRecord: ...


def _hash_response(data: object) -> str:
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class GreenhouseAdapter:
    """Adapter for Greenhouse ATS JSON API."""

    source_type = "greenhouse"
    parser_version = "greenhouse-v1"

    def detect(self, base_url: str) -> bool:
        return "greenhouse.io" in base_url or "/boards/" in base_url

    def list_jobs(self, response_data: object) -> AdapterFetchResult:
        if not isinstance(response_data, dict):
            return AdapterFetchResult(parser_version=self.parser_version)
        data: dict[str, Any] = response_data  # pyright: ignore[reportAssignmentType]
        jobs_raw: Any = data.get("jobs", [])
        if not isinstance(jobs_raw, list):
            return AdapterFetchResult(parser_version=self.parser_version)
        records: list[RawJobRecord] = []
        for job_item in jobs_raw:
            if not isinstance(job_item, dict):
                continue
            job: dict[str, Any] = job_item
            ext_id = str(job.get("id", ""))
            title = str(job.get("title", ""))
            location_obj: Any = job.get("location", {})
            location = ""
            if isinstance(location_obj, dict):
                location = str(location_obj.get("name", ""))
            # absolute_url is the apply URL from Greenhouse
            url = str(job.get("absolute_url", ""))
            records.append(
                RawJobRecord(
                    external_id=ext_id,
                    title=title,
                    location=location,
                    url=url,
                    raw_data=job,
                )
            )
        return AdapterFetchResult(
            jobs=tuple(records),
            response_hash=_hash_response(response_data),
            source_url="",
            parser_version=self.parser_version,
        )


class GreenhouseDetailAdapter:
    """Detail parser for a single Greenhouse job.

    Parses the Greenhouse ``/boards/{board}/jobs/{id}`` response. The list
    endpoint omits the JD body; the detail response carries it in ``content``
    (HTML/text), which becomes ``RawJobRecord.description``. ``external_id``
    and ``url`` are taken from the detail response, falling back to the
    ``source_url`` that was fetched when the response omits ``absolute_url``.
    """

    source_type = "greenhouse_detail"
    parser_version = "greenhouse-detail-v1"

    def fetch_job(
        self,
        detail_response: object,
        *,
        source_url: str,
        fetched_at: datetime,
    ) -> RawJobRecord:
        if not isinstance(detail_response, dict):
            return RawJobRecord(external_id="", title="", url=source_url)
        data: dict[str, Any] = detail_response  # pyright: ignore[reportAssignmentType]
        ext_id = str(data.get("id", ""))
        title = str(data.get("title", ""))
        location_obj: Any = data.get("location", {})
        location = ""
        if isinstance(location_obj, dict):
            location = str(location_obj.get("name", ""))
        url = str(data.get("absolute_url", "")) or source_url
        description = html.unescape(str(data.get("content", "")))
        return RawJobRecord(
            external_id=ext_id,
            title=title,
            location=location,
            url=url,
            description=description,
            raw_data=data,
        )


class LeverAdapter:
    """Adapter for Lever ATS JSON API."""

    source_type = "lever"
    parser_version = "lever-v1"

    def detect(self, base_url: str) -> bool:
        return "lever.co" in base_url

    def list_jobs(self, response_data: object) -> AdapterFetchResult:
        if not isinstance(response_data, list):
            return AdapterFetchResult(parser_version=self.parser_version)
        records: list[RawJobRecord] = []
        for job_item in response_data:
            if not isinstance(job_item, dict):
                continue
            job: dict[str, Any] = job_item
            ext_id = str(job.get("id", ""))
            title = str(job.get("text", ""))
            categories: Any = job.get("categories", {})
            location = ""
            if isinstance(categories, dict):
                location = str(categories.get("location", ""))
            # applyUrl is the direct application link
            url = str(job.get("applyUrl", "") or job.get("hostedUrl", ""))
            description = html.unescape(
                str(job.get("descriptionPlain", "") or job.get("description", ""))
            )
            records.append(
                RawJobRecord(
                    external_id=ext_id,
                    title=title,
                    location=location,
                    url=url,
                    description=description,
                    raw_data=job,
                )
            )
        return AdapterFetchResult(
            jobs=tuple(records),
            response_hash=_hash_response(response_data),
            parser_version=self.parser_version,
        )


class AshbyAdapter:
    """Adapter for Ashby ATS JSON API."""

    source_type = "ashby"
    parser_version = "ashby-v1"

    def detect(self, base_url: str) -> bool:
        return "ashbyhq.com" in base_url

    def list_jobs(self, response_data: object) -> AdapterFetchResult:
        if not isinstance(response_data, dict):
            return AdapterFetchResult(parser_version=self.parser_version)
        data: dict[str, Any] = response_data  # pyright: ignore[reportAssignmentType]
        jobs_raw: Any = data.get("jobs", [])
        if not isinstance(jobs_raw, list):
            return AdapterFetchResult(parser_version=self.parser_version)
        records: list[RawJobRecord] = []
        for job_item in jobs_raw:
            if not isinstance(job_item, dict):
                continue
            job: dict[str, Any] = job_item
            ext_id = str(job.get("id", ""))
            title = str(job.get("title", ""))
            location = str(job.get("location", ""))
            # applyUrl is the direct application link; jobUrl is the board page
            url = str(job.get("applyUrl", "") or job.get("jobUrl", "") or job.get("url", ""))
            description = html.unescape(
                str(
                    job.get("descriptionPlain", "")
                    or job.get("descriptionHtml", "")
                    or job.get("description", "")
                )
            )
            records.append(
                RawJobRecord(
                    external_id=ext_id,
                    title=title,
                    location=location,
                    url=url,
                    description=description,
                    raw_data=job,
                )
            )
        return AdapterFetchResult(
            jobs=tuple(records),
            response_hash=_hash_response(response_data),
            parser_version=self.parser_version,
        )


class AshbyDetailAdapter:
    """Detail parser for a single Ashby job.

    Parses the Ashby posting-api job detail response. The list endpoint omits
    the JD body; the detail response carries it in ``descriptionPlain`` (text)
    falling back to ``description`` (HTML), which becomes
    ``RawJobRecord.description``. Ashby ``location`` is a plain string, unlike
    Greenhouse's nested ``location.name``.
    """

    source_type = "ashby_detail"
    parser_version = "ashby-detail-v1"

    def fetch_job(
        self,
        detail_response: object,
        *,
        source_url: str,
        fetched_at: datetime,
    ) -> RawJobRecord:
        if not isinstance(detail_response, dict):
            return RawJobRecord(external_id="", title="", url=source_url)
        data: dict[str, Any] = detail_response  # pyright: ignore[reportAssignmentType]
        ext_id = str(data.get("id", ""))
        title = str(data.get("title", ""))
        location = str(data.get("location", ""))
        url = str(data.get("url", "")) or source_url
        description = str(data.get("descriptionPlain", "")) or str(data.get("description", ""))
        return RawJobRecord(
            external_id=ext_id,
            title=title,
            location=location,
            url=url,
            description=description,
            raw_data=data,
        )


class JsonLdAdapter:
    """Adapter for JSON-LD structured data in HTML pages."""

    source_type = "json_ld"
    parser_version = "json-ld-v1"

    _JSON_LD_RE = re.compile(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )

    def detect(self, base_url: str) -> bool:
        return True

    def list_jobs(self, response_data: object) -> AdapterFetchResult:
        if not isinstance(response_data, str):
            return AdapterFetchResult(parser_version=self.parser_version)
        records: list[RawJobRecord] = []
        for match in self._JSON_LD_RE.finditer(response_data):
            try:
                parsed: Any = json.loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(parsed, dict):
                continue
            data: dict[str, Any] = parsed
            if data.get("@type") != "JobPosting":
                continue
            title = str(data.get("title", ""))
            url = str(data.get("url", ""))
            location = ""
            job_location: Any = data.get("jobLocation", {})
            if isinstance(job_location, dict):
                address: Any = job_location.get("address", {})
                if isinstance(address, dict):
                    location = str(address.get("addressLocality", ""))
            ext_id = hashlib.sha256(url.encode()).hexdigest()[:16] if url else ""
            description = str(data.get("description", ""))
            records.append(
                RawJobRecord(
                    external_id=ext_id,
                    title=title,
                    location=location,
                    url=url,
                    description=description,
                    raw_data=data,
                )
            )
        return AdapterFetchResult(
            jobs=tuple(records),
            response_hash=_hash_response(response_data),
            parser_version=self.parser_version,
        )


class SitemapAdapter:
    """Adapter for XML sitemaps containing job URLs.

    Rejects XML with DTD or entity declarations to prevent XXE attacks.
    """

    source_type = "sitemap"
    parser_version = "sitemap-v1"

    _JOB_PATH_RE = re.compile(r"/(jobs|careers|positions)/", re.IGNORECASE)
    _UNSAFE_XML_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)

    def detect(self, base_url: str) -> bool:
        return "sitemap" in base_url.lower()

    def list_jobs(self, response_data: object) -> AdapterFetchResult:
        if not isinstance(response_data, str):
            return AdapterFetchResult(parser_version=self.parser_version)
        if self._UNSAFE_XML_RE.search(response_data):
            return AdapterFetchResult(parser_version=self.parser_version)
        records: list[RawJobRecord] = []
        try:
            root = ElementTree.fromstring(response_data)  # nosec B314 -- XXE guarded above
        except ElementTree.ParseError:
            return AdapterFetchResult(parser_version=self.parser_version)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for url_elem in root.findall(".//sm:url/sm:loc", ns):
            loc = url_elem.text or ""
            if self._JOB_PATH_RE.search(loc):
                ext_id = hashlib.sha256(loc.encode()).hexdigest()[:16]
                records.append(
                    RawJobRecord(
                        external_id=ext_id,
                        title="",
                        url=loc,
                    )
                )
        return AdapterFetchResult(
            jobs=tuple(records),
            response_hash=_hash_response(response_data),
            parser_version=self.parser_version,
        )


class StaticHtmlAdapter:
    """Adapter for static HTML pages with job listings."""

    source_type = "static_html"
    parser_version = "static-html-v1"

    _JOB_TITLE_RE = re.compile(
        r'class=["\'][^"\']*job[-_]?title[^"\']*["\'][^>]*>(.*?)<',
        re.IGNORECASE | re.DOTALL,
    )
    _JOB_LOCATION_RE = re.compile(
        r'class=["\'][^"\']*job[-_]?location[^"\']*["\'][^>]*>(.*?)<',
        re.IGNORECASE | re.DOTALL,
    )

    def detect(self, base_url: str) -> bool:
        return True

    def list_jobs(self, response_data: object) -> AdapterFetchResult:
        if not isinstance(response_data, str):
            return AdapterFetchResult(parser_version=self.parser_version)
        records: list[RawJobRecord] = []
        titles = self._JOB_TITLE_RE.findall(response_data)
        locations = self._JOB_LOCATION_RE.findall(response_data)
        for i, title in enumerate(titles):
            location = locations[i] if i < len(locations) else ""
            ext_id = hashlib.sha256(title.strip().encode()).hexdigest()[:16]
            records.append(
                RawJobRecord(
                    external_id=ext_id,
                    title=title.strip(),
                    location=location.strip(),
                )
            )
        return AdapterFetchResult(
            jobs=tuple(records),
            response_hash=_hash_response(response_data),
            parser_version=self.parser_version,
        )


ALL_ADAPTERS: tuple[JobSourceAdapter, ...] = (
    GreenhouseAdapter(),
    LeverAdapter(),
    AshbyAdapter(),
    JsonLdAdapter(),
    SitemapAdapter(),
    StaticHtmlAdapter(),
)
