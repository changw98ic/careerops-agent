from __future__ import annotations

import ipaddress
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

PROVENANCE_KEYS: tuple[str, ...] = (
    "content_sha256",
    "snippet_sha256",
    "payload_sha256",
    "source_url",
    "url_or_thread_ref",
    "official_careers_entry",
    "external_id",
    "request_fingerprint",
    "canonical_url",
)
ALLOWED_SPLITS: frozenset[str] = frozenset({"development", "validation", "holdout"})
DEVELOPMENT_SPLIT = "development"
DEFAULT_MAX_FINDINGS = 50
DEFAULT_MAX_ROWS = 100_000
DEFAULT_MAX_NODES = 1_000_000
DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_UNIQUE_SIGNALS = 250_000
MAX_GENERIC_SIGNAL_LENGTH = 4_096
MAX_URL_LENGTH = 8_192
MAX_URL_COMPONENT_LENGTH = 4_096

_PROVENANCE_KEY_SET = frozenset(PROVENANCE_KEYS)
_URL_KEYS = frozenset(
    {"source_url", "url_or_thread_ref", "official_careers_entry", "canonical_url"}
)
_CONTENT_HASH_KEYS = frozenset({"content_sha256", "snippet_sha256"})
_SHA256_KEYS = _CONTENT_HASH_KEYS | {"payload_sha256"}
_HEX_64_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*\Z")
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_THREAD_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_RAW_INVALID_URI_COMPONENT_CHARS = frozenset('"<>\\^`{|}')
_DEFAULT_PORTS = {"http": 80, "https": 443}


class D0LeakageError(ValueError):
    """Raised when D0 leakage analysis cannot safely validate the input rows."""


@dataclass(frozen=True, slots=True)
class D0LeakageFinding:
    key: str
    splits: tuple[str, ...]
    collision_count: int


@dataclass(frozen=True, slots=True)
class D0LeakageReport:
    checked_row_count: int
    findings: tuple[D0LeakageFinding, ...]
    truncated: bool

    @property
    def passed(self) -> bool:
        return not self.findings and not self.truncated


@dataclass(slots=True)
class _TraversalBudget:
    max_nodes: int
    visited_nodes: int = 0

    def visit(self, row_index: int) -> None:
        self.visited_nodes += 1
        if self.visited_nodes > self.max_nodes:
            raise D0LeakageError(f"row {row_index}: traversal node limit exceeded")

    def ensure_schedulable(self, child_count: int, pending_count: int, row_index: int) -> None:
        if self.visited_nodes + pending_count + child_count > self.max_nodes:
            raise D0LeakageError(f"row {row_index}: traversal node limit exceeded")


def analyze_d0_leakage(
    rows: Iterable[Any],
    *,
    max_findings: int = DEFAULT_MAX_FINDINGS,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_unique_signals: int = DEFAULT_MAX_UNIQUE_SIGNALS,
) -> D0LeakageReport:
    """Analyze parsed D0 dataset rows for redacted cross-split provenance leakage.

    URL fields share one canonical namespace, while content, payload, and generic
    identifiers remain separate to avoid false equivalence between unrelated signal
    types. All limits are global to one analysis call.
    """

    _require_positive_limit(max_findings, "max_findings")
    _require_positive_limit(max_rows, "max_rows")
    _require_positive_limit(max_nodes, "max_nodes")
    _require_non_negative_limit(max_depth, "max_depth")
    _require_positive_limit(max_unique_signals, "max_unique_signals")

    rows_by_key_value: dict[tuple[str, tuple[str, str]], set[str]] = defaultdict(set)
    checked_row_count = 0
    budget = _TraversalBudget(max_nodes=max_nodes)

    for row_index, row in enumerate(rows):
        if row_index >= max_rows:
            raise D0LeakageError("row limit exceeded")
        if not isinstance(row, dict):
            raise D0LeakageError(f"row {row_index}: row must be an object")
        row = cast(dict[str, Any], row)

        split = _required_split(row, row_index)
        synthetic = row.get("synthetic")
        if not isinstance(synthetic, bool):
            raise D0LeakageError(f"row {row_index}: synthetic must be a boolean")
        if synthetic and split != DEVELOPMENT_SPLIT:
            raise D0LeakageError(
                f"row {row_index}: synthetic rows are only allowed in {DEVELOPMENT_SPLIT}"
            )

        checked_row_count += 1
        for key, value in _iter_provenance_values(
            row,
            row_index=row_index,
            budget=budget,
            max_depth=max_depth,
        ):
            signal = (key, value)
            if signal not in rows_by_key_value and len(rows_by_key_value) >= max_unique_signals:
                raise D0LeakageError("unique provenance signal limit exceeded")
            rows_by_key_value[signal].add(split)

    grouped: dict[tuple[str, tuple[str, ...]], int] = defaultdict(int)
    for (key, _value), splits in rows_by_key_value.items():
        if len(splits) > 1:
            grouped[(key, tuple(sorted(splits)))] += 1

    findings: list[D0LeakageFinding] = []
    truncated = False
    for (key, splits), collision_count in sorted(grouped.items()):
        if len(findings) == max_findings:
            truncated = True
            break
        findings.append(D0LeakageFinding(key=key, splits=splits, collision_count=collision_count))

    return D0LeakageReport(
        checked_row_count=checked_row_count,
        findings=tuple(findings),
        truncated=truncated,
    )


def _require_positive_limit(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise D0LeakageError(f"{name} must be a positive integer")


def _require_non_negative_limit(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise D0LeakageError(f"{name} must be a non-negative integer")


def _required_split(row: dict[str, Any], row_index: int) -> str:
    split = row.get("split")
    if not isinstance(split, str) or not split:
        raise D0LeakageError(f"row {row_index}: split must be a non-empty string")
    if split not in ALLOWED_SPLITS:
        allowed = ", ".join(sorted(ALLOWED_SPLITS))
        raise D0LeakageError(f"row {row_index}: unrecognized split; allowed: {allowed}")
    return split


def _iter_provenance_values(
    row: dict[str, Any],
    *,
    row_index: int,
    budget: _TraversalBudget,
    max_depth: int,
) -> Iterator[tuple[str, tuple[str, str]]]:
    # The explicit stack makes hostile nesting a bounded validation error instead
    # of a Python RecursionError. A provenance field is a leaf by contract.
    stack: list[tuple[Any, int, str | None, str | None]] = [(row, 0, None, None)]
    while stack:
        value, depth, provenance_key, object_key = stack.pop()
        budget.visit(row_index)
        if depth > max_depth:
            raise D0LeakageError(f"row {row_index}: nesting depth limit exceeded")

        if provenance_key is not None:
            normalized = _normalize_signal_value(
                provenance_key,
                value,
                context=f"row {row_index} field {provenance_key}",
            )
            if normalized is not None:
                yield _signal_namespace(provenance_key), normalized
            continue

        if isinstance(value, dict):
            value_dict = cast(dict[Any, Any], value)
            if value_dict and depth >= max_depth:
                raise D0LeakageError(f"row {row_index}: nesting depth limit exceeded")
            budget.ensure_schedulable(len(value_dict), len(stack), row_index)
            children: list[tuple[Any, int, str | None, str | None]] = []
            for raw_key, child in value_dict.items():
                if not isinstance(raw_key, str):
                    raise D0LeakageError(f"row {row_index}: object keys must be strings")
                key = _provenance_key_for(
                    raw_key,
                    object_key=object_key,
                    source_kind=value_dict.get("kind"),
                )
                children.append((child, depth + 1, key, raw_key))
            stack.extend(reversed(children))
        elif isinstance(value, list):
            value_list = cast(list[Any], value)
            if value_list and depth >= max_depth:
                raise D0LeakageError(f"row {row_index}: nesting depth limit exceeded")
            budget.ensure_schedulable(len(value_list), len(stack), row_index)
            stack.extend((child, depth + 1, None, None) for child in reversed(value_list))


def _provenance_key_for(
    raw_key: str,
    *,
    object_key: str | None,
    source_kind: Any,
) -> str | None:
    if (
        raw_key == "url_or_thread_ref"
        and object_key == "source"
        and source_kind == "established_thread"
    ):
        return "established_thread_ref"
    if raw_key in _PROVENANCE_KEY_SET:
        return raw_key
    if object_key == "source" and raw_key == "url":
        return "source_url"
    return None


def _signal_namespace(key: str) -> str:
    if key in _URL_KEYS:
        # Preserve the established redacted report label while all URL-bearing
        # fields share this single comparison namespace.
        return "source_url"
    if key in _CONTENT_HASH_KEYS:
        return "content_sha256"
    if key == "established_thread_ref":
        return "thread_ref"
    return key


def _normalize_signal_value(
    key: str,
    value: Any,
    *,
    context: str,
) -> tuple[str, str] | None:
    if value is None:
        return None
    if key in _URL_KEYS or key == "established_thread_ref":
        if not isinstance(value, str):
            raise D0LeakageError(f"{context}: URL provenance value must be a string")
        stripped = value.strip()
        if not stripped:
            return None
        if key == "established_thread_ref":
            if _THREAD_REF_RE.fullmatch(stripped) is None:
                raise D0LeakageError(f"{context}: malformed URL or thread provenance value")
            return "thread_ref", stripped
        return "url", _normalize_url(value, context=context)
    if key in _SHA256_KEYS:
        if not isinstance(value, str):
            raise D0LeakageError(f"{context}: SHA-256 provenance value must be a string")
        stripped = value.strip()
        if not stripped:
            return None
        if stripped != value or _HEX_64_RE.fullmatch(value) is None:
            raise D0LeakageError(f"{context}: malformed SHA-256 provenance value")
        return "sha256", value.lower()
    return _normalize_generic_scalar(value, context=context)


def _normalize_generic_scalar(value: Any, *, context: str) -> tuple[str, str] | None:
    if isinstance(value, str):
        if _contains_control(value):
            raise D0LeakageError(f"{context}: provenance value contains control characters")
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) > MAX_GENERIC_SIGNAL_LENGTH:
            raise D0LeakageError(f"{context}: provenance value length limit exceeded")
        return "str", stripped
    if isinstance(value, bool):
        raise D0LeakageError(f"{context}: provenance value must not be boolean")
    if isinstance(value, int):
        return "int", str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise D0LeakageError(f"{context}: provenance value must be finite")
        return "float", repr(value)
    raise D0LeakageError(f"{context}: provenance value must be a scalar")


def _normalize_url(value: str, *, context: str) -> str:
    if len(value) > MAX_URL_LENGTH:
        raise D0LeakageError(f"{context}: URL provenance value length limit exceeded")
    if _contains_forbidden_url_character(value) or "\\" in value:
        raise D0LeakageError(f"{context}: malformed URL provenance value")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise D0LeakageError(f"{context}: malformed URL provenance value") from None

    scheme = parts.scheme.lower()
    if _SCHEME_RE.fullmatch(parts.scheme) is None or parts.hostname is None:
        raise D0LeakageError(f"{context}: malformed URL provenance value")
    if parts.username is not None or parts.password is not None:
        raise D0LeakageError(f"{context}: malformed URL provenance value")
    if parts.netloc.endswith(":"):
        raise D0LeakageError(f"{context}: malformed URL provenance value")

    try:
        port = parts.port
        host = _normalize_host(parts.hostname)
    except (UnicodeError, ValueError):
        raise D0LeakageError(f"{context}: malformed URL provenance value") from None

    if port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"

    try:
        path = _remove_dot_segments(_normalize_percent_encoding(parts.path))
        query = _normalize_percent_encoding(parts.query)
        _normalize_percent_encoding(parts.fragment)
    except ValueError:
        raise D0LeakageError(f"{context}: malformed URL provenance value") from None
    if not path:
        path = "/"

    normalized = SplitResult(scheme, netloc, path, query, "")
    return urlunsplit(normalized)


def _normalize_host(host: str) -> str:
    if not host or _contains_forbidden_url_character(host) or "%" in host:
        raise ValueError("invalid host")
    if ":" in host:
        return ipaddress.IPv6Address(host).compressed.lower()

    trailing_dot = host.endswith(".")
    host_without_dot = host[:-1] if trailing_dot else host
    if not host_without_dot:
        raise ValueError("invalid host")
    if not host_without_dot.isascii():
        raise ValueError("invalid host")
    ascii_host = host_without_dot.lower()
    if len(ascii_host) > 253:
        raise ValueError("invalid host")
    labels = ascii_host.split(".")
    if any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels):
        raise ValueError("invalid host")
    return ascii_host


def _normalize_percent_encoding(component: str) -> str:
    if len(component) > MAX_URL_COMPONENT_LENGTH:
        raise ValueError("URL component length limit exceeded")
    output: list[str] = []
    index = 0
    while index < len(component):
        character = component[index]
        if character != "%":
            if character in _RAW_INVALID_URI_COMPONENT_CHARS:
                raise ValueError("invalid raw URI character")
            if ord(character) > 0x7F:
                output.extend(f"%{byte:02X}" for byte in character.encode("utf-8"))
            else:
                output.append(character)
            index += 1
            continue
        if index + 2 >= len(component):
            raise ValueError("incomplete percent encoding")
        encoded = component[index + 1 : index + 3]
        try:
            byte = int(encoded, 16)
        except ValueError:
            raise ValueError("invalid percent encoding") from None
        if byte < 0x20 or byte == 0x7F:
            raise ValueError("encoded control character")
        decoded = chr(byte)
        output.append(decoded if decoded in _UNRESERVED else f"%{byte:02X}")
        index += 3
    return "".join(output)


def _remove_dot_segments(path: str) -> str:
    trailing_dot_segment = path.endswith("/.") or path.endswith("/..")
    output: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if output and output[-1] != "":
                output.pop()
            continue
        output.append(segment)
    if trailing_dot_segment and (not output or output[-1] != ""):
        output.append("")
    return "/".join(output)


def _contains_forbidden_url_character(value: str) -> bool:
    return any(
        character.isspace() or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    )


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)
