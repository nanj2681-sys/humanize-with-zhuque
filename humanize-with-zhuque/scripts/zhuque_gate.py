#!/usr/bin/env python3
"""Prepare the default Zhuque webpage flow, call its API, or evaluate a response.

Exit codes:
  2  invalid command-line syntax (argparse)
  0  PASS
  10 REVISE
  20 invalid response or data
  21 result unknown or asynchronous PENDING state
  22 blocked operation or invalid configuration

With only --input, this script performs no network request and returns a
WEBPAGE_RESULT_REQUIRED handoff for Tencent's official free webpage. API mode
is opt-in through --live-api. The API key is read only from ZHUQUE_API_KEY and
the gateway from ZHUQUE_GATEWAY. This script never retries a request that could
consume quota or incur charges. Saved responses must be bound to --input by
both a matching input_sha256 and exact full segment coverage. Neither mechanism
is sufficient by itself.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

EXIT_PASS = 0
EXIT_REVISE = 10
EXIT_INVALID = 20
EXIT_TEMPORARY = 21
EXIT_CONFIG = 22
MAX_TEXT_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_SEGMENTS = 20_000
RATIO_SUM_TOLERANCE = Decimal("0.001")
AGGREGATE_AI_NOISE_MAX = Decimal("0.001")
OFFICIAL_WEB_URL = "https://matrix.tencent.com/ai-detect/ai_gen"


class InvalidData(ValueError):
    pass


class PendingResult(RuntimeError):
    def __init__(self, status: str, task_id: str | None = None) -> None:
        super().__init__(status)
        self.status = status
        self.task_id = task_id


class BlockedOperation(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class ResultUnknown(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class ConfigurationError(RuntimeError):
    pass


class ProtectedOutput(RuntimeError):
    pass


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise InvalidData(f"Zhuque gateway redirect refused ({code})")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reject_constant(value: str) -> None:
    raise InvalidData(f"non-finite JSON number is not allowed: {value}")


def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidData(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def loads_strict(raw: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
            parse_float=Decimal,
        )
    except InvalidData:
        raise
    except (ValueError, RecursionError) as exc:
        raise InvalidData(f"invalid JSON: {exc}") from exc


def read_text(path: Path, maximum: int) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        with handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise InvalidData(f"input must be a regular file: {path}")
            raw = handle.read(maximum + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > maximum:
        raise InvalidData(f"file exceeds {maximum} bytes: {path}")
    return raw.decode("utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_recovery_json(output: Path, payload: dict[str, Any]) -> Path:
    """Persist a unique request outcome when the primary output stays locked."""

    expanded = output.expanduser()
    expanded.parent.mkdir(parents=True, exist_ok=True)
    recovery: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=expanded.parent,
            prefix=f"{expanded.name}.recovery-",
            suffix=".json",
            delete=False,
        ) as handle:
            recovery = Path(handle.name)
            handle.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(
            expanded.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return recovery
    except Exception:
        if recovery is not None:
            recovery.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def output_lock(path: Path, *, wait_seconds: float = 0.0):
    """Serialize one output path, optionally waiting briefly for a local writer."""

    resolved = path.expanduser().resolve(strict=False)
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / (
        f"humanize-with-zhuque-{os.getuid()}-{digest}.lock"
    )
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtectedOutput(
                        "another process is updating this output path"
                    ) from exc
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def reserve_submission_intent(path: Path, payload: dict[str, Any]) -> None:
    """Atomically reserve a new immutable API-attempt path before transmission."""

    expanded = path.expanduser()
    expanded.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    with output_lock(expanded):
        descriptor = -1
        created = False
        try:
            try:
                descriptor = os.open(expanded, flags, 0o600)
                created = True
            except FileExistsError as exc:
                raise ProtectedOutput(
                    "API output path already exists; preserve it and use a new "
                    "immutable path for this attempt"
                ) from exc
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            directory_fd = os.open(
                expanded.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            if created:
                expanded.unlink(missing_ok=True)
            raise


def ensure_output_distinct(output: Path | None, inputs: list[Path | None]) -> None:
    if output is None:
        return
    expanded_output = output.expanduser()
    resolved_output = expanded_output.resolve(strict=False)
    for input_path in inputs:
        if input_path is None:
            continue
        expanded_input = input_path.expanduser()
        if resolved_output == expanded_input.resolve(strict=False):
            raise ConfigurationError(f"output must not overwrite input: {input_path}")
        try:
            if expanded_output.exists() and os.path.samefile(
                expanded_output, expanded_input
            ):
                raise ConfigurationError(
                    f"output must not overwrite input: {input_path}"
                )
        except FileNotFoundError:
            pass


def ensure_output_transition(
    output: Path,
    payload: dict[str, Any],
    *,
    allow_submission_intent: bool = False,
) -> None:
    """Prevent an unresolved API state from being hidden by a later result."""

    expanded = output.expanduser()
    if not expanded.exists():
        return
    raw = read_text(expanded, MAX_RESPONSE_BYTES)
    existing = loads_strict(raw)
    if not isinstance(existing, dict):
        raise ProtectedOutput("existing output is not a JSON object; preserve it")

    existing_verdict = existing.get("verdict")
    same_input = (
        isinstance(existing.get("input_sha256"), str)
        and existing.get("input_sha256") == payload.get("input_sha256")
    )
    continuing_live_request = allow_submission_intent and same_input

    if existing_verdict == "SUBMISSION_INTENT" and continuing_live_request:
        return
    if existing_verdict == "SUBMISSION_INTENT":
        raise ProtectedOutput(
            "existing SUBMISSION_INTENT may represent an in-flight API request; "
            "verify provider state and use a new output path"
        )
    if existing_verdict == "RESULT_UNKNOWN":
        raise ProtectedOutput(
            "existing RESULT_UNKNOWN must not be overwritten; verify provider state "
            "and preserve the original event"
        )
    if (
        existing.get("may_have_been_billed") is True
        and existing.get("do_not_retry_same_text") is True
    ):
        raise ProtectedOutput(
            "existing API event may have consumed quota and is marked immutable; "
            "preserve it and use a new output path"
        )


def validate_gateway(raw: str) -> str:
    gateway = raw.rstrip("/")
    parsed = urllib.parse.urlparse(gateway)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigurationError("ZHUQUE_GATEWAY must be an HTTPS origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "ZHUQUE_GATEWAY must not contain credentials, query, or fragment"
        )
    if parsed.path not in ("", "/"):
        raise ConfigurationError("ZHUQUE_GATEWAY must be an origin without a path")
    return gateway


def resolve_api_config() -> tuple[str, str]:
    raw_gateway = os.environ.get("ZHUQUE_GATEWAY", "").strip()
    api_key = os.environ.get("ZHUQUE_API_KEY", "").strip()
    missing = [
        name
        for name, value in (
            ("ZHUQUE_GATEWAY", raw_gateway),
            ("ZHUQUE_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        category = {
            ("ZHUQUE_GATEWAY",): "missing_api_gateway",
            ("ZHUQUE_API_KEY",): "missing_api_key",
        }.get(tuple(missing), "missing_api_credentials")
        raise BlockedOperation(
            category,
            "API mode is not configured; missing "
            + " and ".join(missing)
            + ". Use the official Zhuque webpage unless API-only was requested.",
        )
    if "\r" in api_key or "\n" in api_key:
        raise ConfigurationError("ZHUQUE_API_KEY must not contain line breaks")
    return validate_gateway(raw_gateway), api_key


def call_api(text: str, timeout: float, gateway: str, api_key: str) -> dict[str, Any]:
    endpoint = gateway + "/v1/providers/zhuque-text/classify"
    body = json.dumps({"text": text, "is_merge": False}, ensure_ascii=False).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(RejectRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise BlockedOperation(
                "authorization_or_entitlement",
                f"Zhuque authorization or entitlement failed (HTTP {exc.code})",
            ) from exc
        if exc.code == 429:
            raise BlockedOperation(
                "rate_limit_or_quota", "Zhuque rate limit or quota blocked the request"
            ) from exc
        if 500 <= exc.code <= 599:
            raise ResultUnknown(
                "service_response_unknown",
                f"Zhuque returned HTTP {exc.code}; request result is unknown",
            ) from exc
        raise InvalidData(f"Zhuque HTTP error {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ResultUnknown(
            "network_or_timeout",
            "Zhuque request outcome is unknown after a network error or timeout",
        ) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise InvalidData("Zhuque response exceeds size limit")
    payload = loads_strict(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise InvalidData("Zhuque response root must be an object")
    return payload


def unwrap_response(payload: dict[str, Any]) -> dict[str, Any]:
    if "Status" not in payload and "TaskId" not in payload:
        return payload

    status = payload.get("Status")
    raw_task_id = payload.get("TaskId")
    if not isinstance(raw_task_id, str) or not raw_task_id.strip():
        raise InvalidData("asynchronous response TaskId must be a non-empty string")
    task_id = raw_task_id.strip()
    if "Status" not in payload:
        raise PendingResult("QUEUED", task_id)
    if not isinstance(status, str):
        raise InvalidData("asynchronous response Status must be a string")
    normalized = status.upper()
    if normalized in {"QUEUED", "PROCESSING"}:
        raise PendingResult(normalized, task_id)
    if normalized == "FAILED":
        raise InvalidData("asynchronous Zhuque task failed")
    if normalized != "SUCCEEDED":
        raise InvalidData(f"unknown asynchronous Status: {status}")
    output = payload.get("Output")
    if not isinstance(output, str):
        raise InvalidData("SUCCEEDED asynchronous response must contain string Output")
    decoded = loads_strict(output)
    if isinstance(decoded, str):
        raise InvalidData("asynchronous Output remained a string after one decode")
    if not isinstance(decoded, dict):
        raise InvalidData("asynchronous Output must decode to an object")
    return decoded


def exact_ratio(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise InvalidData(f"{name} must be a JSON number")
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite() or not Decimal(0) <= number <= Decimal(1):
        raise InvalidData(f"{name} must be finite and within [0, 1]")
    return number


def optional_number(value: Any, name: str, warnings: list[str]) -> float | None:
    if value is None:
        warnings.append(f"{name} is missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        warnings.append(f"{name} is not numeric")
        return None
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite() or not Decimal(0) <= number <= Decimal(1):
        warnings.append(f"{name} is outside [0, 1]")
        return None
    return float(number)


def fingerprint_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"__exact_decimal__": str(value)}
    if isinstance(value, dict):
        return {key: fingerprint_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [fingerprint_safe(item) for item in value]
    return value


def validate_position(value: Any, warnings: list[str], index: int) -> list[int] | None:
    if value is None:
        warnings.append(f"segment {index} position is missing")
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(x, bool) or not isinstance(x, int) for x in value)
        or value[0] < 0
        or value[1] < value[0]
    ):
        warnings.append(f"segment {index} position is invalid")
        return None
    return [value[0], value[1]]


def validate_declared_hash(value: Any, location: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise InvalidData(f"{location} input_sha256 must be 64 hexadecimal characters")
    return value.lower()


def verify_segment_coverage(
    input_text: str, segments: list[Any], *, context: str
) -> None:
    covered = bytearray(len(input_text))
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise InvalidData(f"segment {index} must be an object")
        text = segment.get("text")
        position = segment.get("position")
        if not isinstance(text, str):
            raise InvalidData(f"{context} requires text on every segment")
        if (
            not isinstance(position, list)
            or len(position) != 2
            or any(isinstance(x, bool) or not isinstance(x, int) for x in position)
        ):
            raise InvalidData(f"{context} requires a valid position on every segment")
        start, end = position
        if start < 0 or end <= start or end > len(input_text):
            raise InvalidData(f"segment {index} position is outside --input")
        if input_text[start:end] != text:
            raise InvalidData(f"segment {index} text does not match --input position")
        if any(covered[start:end]):
            raise InvalidData(f"segment {index} overlaps an earlier segment")
        covered[start:end] = b"\x01" * (end - start)

    if any(
        not covered[index]
        for index, character in enumerate(input_text)
        if not character.isspace()
    ):
        raise InvalidData(f"{context} segments do not cover all non-whitespace input")


def verify_saved_response_binding(
    outer_payload: dict[str, Any],
    response: dict[str, Any],
    input_text: str,
    segments: list[Any],
) -> str:
    """Prove that an offline response belongs to the supplied candidate text."""

    expected = sha256_text(input_text)
    declared: list[tuple[str, str]] = []
    if "input_sha256" in outer_payload:
        declared.append(
            (
                "outer",
                validate_declared_hash(outer_payload["input_sha256"], "outer response"),
            )
        )
    if response is not outer_payload and "input_sha256" in response:
        declared.append(
            (
                "inner",
                validate_declared_hash(response["input_sha256"], "inner response"),
            )
        )
    if not declared:
        raise InvalidData("saved response requires input_sha256")
    if declared and any(value != expected for _, value in declared):
        raise InvalidData("saved response input_sha256 does not match --input")
    verify_segment_coverage(input_text, segments, context="saved response")
    return "segment_text_and_position+declared_sha256"


def evaluate(
    payload: dict[str, Any],
    *,
    input_text: str | None,
    source: str,
    include_segment_text: bool,
) -> dict[str, Any]:
    response = unwrap_response(payload)
    status = response.get("status")
    if not isinstance(status, str) or status.lower() != "success":
        raise InvalidData("Zhuque status is not success")

    ratios = response.get("labels_ratio")
    if not isinstance(ratios, dict):
        raise InvalidData("labels_ratio must be an object")
    if set(ratios) != {"0", "1", "2"}:
        raise InvalidData("labels_ratio must contain exactly string keys 0, 1, and 2")
    human = exact_ratio(ratios["0"], 'labels_ratio["0"]')
    ai = exact_ratio(ratios["1"], 'labels_ratio["1"]')
    suspected = exact_ratio(ratios["2"], 'labels_ratio["2"]')
    ratio_sum = sum((human, ai, suspected), Decimal(0))
    if abs(ratio_sum - Decimal(1)) > RATIO_SUM_TOLERANCE:
        raise InvalidData(f"labels_ratio sum {ratio_sum:.6f} is outside tolerance")

    segments = response.get("segment_labels")
    if not isinstance(segments, list) or not segments:
        raise InvalidData("segment_labels must be a non-empty list")
    if len(segments) > MAX_SEGMENTS:
        raise InvalidData(f"segment_labels exceeds {MAX_SEGMENTS} entries")

    if source in {"saved_response", "official_webpage"}:
        if input_text is None:
            raise ConfigurationError("--input is required with --response")
        input_binding = verify_saved_response_binding(
            payload, response, input_text, segments
        )
    elif source == "official_api":
        if input_text is None:
            raise ConfigurationError("--input is required in API mode")
        if "input_sha256" in response:
            declared = validate_declared_hash(response["input_sha256"], "API response")
            if declared != sha256_text(input_text):
                raise InvalidData("API response input_sha256 does not match request")
        verify_segment_coverage(input_text, segments, context="official API response")
        input_binding = "same_process_api_submission+segment_text_and_position"
    else:
        input_binding = "test_fixture_unbound"

    warnings: list[str] = []
    normalized_segments: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise InvalidData(f"segment {index} must be an object")
        label = segment.get("label")
        if (
            isinstance(label, bool)
            or not isinstance(label, int)
            or label not in (0, 1, 2)
        ):
            raise InvalidData(f"segment {index} label must be exact integer 0, 1, or 2")
        order = segment.get("order")
        if order is not None and (
            isinstance(order, bool) or not isinstance(order, int)
        ):
            warnings.append(f"segment {index} order is invalid")
            order = None
        confidence = optional_number(
            segment.get("conf"), f"segment {index} conf", warnings
        )
        position = validate_position(segment.get("position"), warnings, index)
        item: dict[str, Any] = {
            "index": index,
            "order": order,
            "label": label,
            "label_name": {0: "human", 1: "ai", 2: "suspected_ai"}[label],
            "confidence": confidence,
            "position": position,
        }
        if include_segment_text and isinstance(segment.get("text"), str):
            item["text"] = segment["text"][:20_000]
        normalized_segments.append(item)

    definite = [item for item in normalized_segments if item["label"] == 1]
    suspicious = [item for item in normalized_segments if item["label"] == 2]
    checks = {
        "human_at_least_80_percent": human >= Decimal("0.80"),
        "suspected_ai_below_20_percent": suspected < Decimal("0.20"),
        "aggregate_ai_within_noise_tolerance": ai <= AGGREGATE_AI_NOISE_MAX,
        "no_definite_ai_segments": not definite,
    }
    failed_checks = [name for name, passed in checks.items() if not passed]
    if 0 < ai <= AGGREGATE_AI_NOISE_MAX and not definite:
        warnings.append(
            "aggregate AI ratio is non-zero but within the allowed smoothing tolerance"
        )
    elif ai > AGGREGATE_AI_NOISE_MAX and not definite:
        warnings.append(
            "aggregate AI ratio exceeds the smoothing tolerance despite no segment label 1"
        )

    if input_text is not None:
        for item in normalized_segments:
            position = item["position"]
            if position and position[1] > len(input_text):
                warnings.append(
                    f"segment {item['index']} position exceeds input length"
                )

    passed = not failed_checks
    response_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_safe(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    result = {
        "schema_version": 1,
        "scope": "zhuque_detector_gate",
        "threshold_version": "human>=0.80;suspected<0.20;aggregate-ai<=0.001;no-segment-label-1",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "adapter": {
            "official_api": "official_api",
            "official_webpage": "official_webpage",
        }.get(source, "saved_response_validation"),
        "input_binding": input_binding,
        "verdict": "PASS" if passed else "REVISE",
        "detector_verdict": "PASS" if passed else "REVISE",
        "passed": passed,
        "overall_article_finalizable": False,
        "input_sha256": sha256_text(input_text) if input_text is not None else None,
        "response_sha256": response_fingerprint,
        "task_id": payload.get("TaskId"),
        "metrics": {
            "human_ratio": float(human),
            "ai_ratio": float(ai),
            "suspected_ai_ratio": float(suspected),
            "human_ratio_exact": str(human),
            "ai_ratio_exact": str(ai),
            "suspected_ai_ratio_exact": str(suspected),
            "human_percent": round(float(human * 100), 4),
            "ai_percent": round(float(ai * 100), 4),
            "suspected_ai_percent": round(float(suspected * 100), 4),
            "definite_ai_segment_count": len(definite),
            "suspected_ai_segment_count": len(suspicious),
            "segment_count": len(normalized_segments),
        },
        "checks": checks,
        "failed_checks": failed_checks,
        "rewrite_targets": definite + suspicious,
        "warnings": list(dict.fromkeys(warnings)),
    }
    if source == "official_api":
        result.update(
            {
                "request_completed": True,
                "may_have_been_billed": True,
                "do_not_retry_same_text": True,
            }
        )
    elif source == "official_webpage":
        result.update(
            {
                "source_attestation": "visible_official_webpage",
                "source_verified_by_script": False,
                "source_evidence_required": True,
            }
        )
    return result


def error_payload(
    category: str,
    message: str,
    input_text: str | None,
    *,
    request_attempted: bool = False,
    may_have_been_billed: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": "zhuque_detector_gate",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "ERROR",
        "passed": False,
        "overall_article_finalizable": False,
        "error_category": category,
        "error": message,
        "input_sha256": sha256_text(input_text) if input_text is not None else None,
        "request_attempted": request_attempted,
        "may_have_been_billed": may_have_been_billed,
        "do_not_retry_same_text": may_have_been_billed,
    }


def blocked_payload(
    category: str,
    message: str,
    input_text: str | None,
    *,
    request_attempted: bool,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "scope": "zhuque_detector_gate",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "BLOCKED",
        "adapter": "official_api",
        "blocker_scope": "official_api",
        "passed": False,
        "overall_article_finalizable": False,
        "blocker_category": category,
        "blocker": message,
        "input_sha256": sha256_text(input_text) if input_text is not None else None,
        "request_attempted": request_attempted,
        "may_have_been_billed": False,
        "retry_allowed": False,
    }
    if category in {
        "missing_api_credentials",
        "missing_api_gateway",
        "missing_api_key",
        "authorization_or_entitlement",
        "rate_limit_or_quota",
    }:
        payload.update(
            {
                "fallback_adapter": "official_webpage",
                "fallback_url": OFFICIAL_WEB_URL,
                "operator_action": (
                    "use the official webpage unless the user explicitly requested "
                    "API-only; do not report Zhuque itself as unavailable"
                ),
            }
        )
    return payload


def result_unknown_payload(
    category: str, message: str, input_text: str | None
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": "zhuque_detector_gate",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "RESULT_UNKNOWN",
        "adapter": "official_api",
        "blocker_scope": "official_api",
        "passed": False,
        "overall_article_finalizable": False,
        "error_category": category,
        "error": message,
        "input_sha256": sha256_text(input_text) if input_text is not None else None,
        "request_attempted": True,
        "request_completed": False,
        "may_have_been_billed": True,
        "retry_allowed": False,
        "operator_action": "check provider state before any retry",
    }


def submission_intent_payload(input_text: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": "zhuque_detector_gate",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "SUBMISSION_INTENT",
        "adapter": "official_api",
        "passed": False,
        "overall_article_finalizable": False,
        "input_sha256": sha256_text(input_text),
        "request_attempted": False,
        "may_have_been_billed": False,
        "retry_allowed": False,
        "operator_action": "if this file remains, verify provider state before retrying",
    }


def webpage_handoff_payload(input_text: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": "zhuque_detector_gate",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "PENDING",
        "status": "WEBPAGE_RESULT_REQUIRED",
        "adapter": "official_webpage",
        "passed": False,
        "overall_article_finalizable": False,
        "input_sha256": sha256_text(input_text),
        "webpage_url": OFFICIAL_WEB_URL,
        "api_credentials_required": False,
        "request_attempted": False,
        "may_have_been_billed": False,
        "submission_allowed": True,
        "retry_allowed": False,
        "operator_action": (
            "open the official webpage in a visible browser, read the displayed "
            "remaining attempts, submit the exact full text once, preserve visible "
            "evidence, then evaluate the normalized response with --response "
            "and --web-response"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--input",
        type=Path,
        help="UTF-8 canonical text; required for webpage, response, and API modes",
    )
    parser.add_argument(
        "--response",
        type=Path,
        help="evaluate a saved JSON response without network access",
    )
    parser.add_argument(
        "--web-response",
        action="store_true",
        help=(
            "attest that --response was transcribed from the visible official "
            "webpage; screenshot or browser evidence is still required"
        ),
    )
    parser.add_argument(
        "--live-api",
        action="store_true",
        help="explicitly opt in to one API request that may use quota or incur charges",
    )
    parser.add_argument("--output", type=Path, help="write normalized gate JSON")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--include-segment-text", action="store_true")
    return parser.parse_args()


def emit(payload: dict[str, Any], output: Path | None) -> None:
    if output:
        write_json(output, payload)
        print(f"verdict={payload.get('verdict')} output={output}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def redact_rewrite_target_text(value: Any) -> Any:
    """Copy a report while removing detector segment text at every nesting level."""

    if isinstance(value, list):
        return [redact_rewrite_target_text(item) for item in value]
    if not isinstance(value, dict):
        return value

    redacted = {
        key: redact_rewrite_target_text(item) for key, item in value.items()
    }
    targets = redacted.get("rewrite_targets")
    if isinstance(targets, list):
        for target in targets:
            if isinstance(target, dict):
                target.pop("text", None)
    return redacted


def safe_emit(payload: dict[str, Any], output: Path | None) -> bool:
    try:
        emit(payload, output)
        return True
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        fallback = redact_rewrite_target_text(payload)
        fallback.update(
            {
                "output_write_failed": True,
                "output_write_error": type(exc).__name__,
                "do_not_retry_same_text": bool(
                    fallback.get("request_completed")
                    or fallback.get("may_have_been_billed")
                ),
            }
        )
        try:
            print(
                json.dumps(fallback, ensure_ascii=False, sort_keys=True),
                file=sys.stderr,
            )
        except (OSError, UnicodeError):
            pass
        return False


def protected_transition_payload(
    error: ProtectedOutput,
    payload: dict[str, Any],
    output: Path,
    *,
    recovery_output: Path | None = None,
) -> dict[str, Any]:
    request_completed = payload.get("request_completed") is True
    may_have_been_billed = payload.get("may_have_been_billed") is True
    request_attempted = (
        payload.get("request_attempted") is True
        or request_completed
        or may_have_been_billed
    )
    do_not_retry = (
        payload.get("do_not_retry_same_text") is True or may_have_been_billed
    )
    result = {
        "schema_version": 1,
        "scope": "zhuque_detector_gate",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "ERROR",
        "passed": False,
        "overall_article_finalizable": False,
        "error_category": "protected_output_transition",
        "error": str(error),
        "preserved_output": str(output),
        "attempted_verdict": payload.get("verdict"),
        "input_sha256": payload.get("input_sha256"),
        "request_attempted": request_attempted,
        "request_completed": request_completed,
        "may_have_been_billed": may_have_been_billed,
        "do_not_retry_same_text": do_not_retry,
        "operator_action": (
            "preserve the existing output and any recovery artifact; check provider "
            "state before retrying"
            if request_attempted
            else "preserve the existing output and retry only after its state is clear"
        ),
    }
    if recovery_output is not None:
        result["recovery_output"] = str(recovery_output)
    return result


def finish(
    payload: dict[str, Any],
    output: Path | None,
    exit_code: int,
    *,
    allow_submission_intent: bool = False,
) -> int:
    if output is None:
        return exit_code if safe_emit(payload, None) else EXIT_INVALID

    try:
        with output_lock(output, wait_seconds=5.0):
            ensure_output_transition(
                output,
                payload,
                allow_submission_intent=allow_submission_intent,
            )
            return exit_code if safe_emit(payload, output) else EXIT_INVALID
    except (OSError, UnicodeError, InvalidData, ProtectedOutput) as exc:
        if isinstance(exc, ProtectedOutput):
            message = exc
        else:
            message = ProtectedOutput(
                f"could not validate existing output safely: {type(exc).__name__}"
            )
        recovery_output: Path | None = None
        recovery_error: str | None = None
        request_outcome = (
            payload.get("request_attempted") is True
            or payload.get("request_completed") is True
            or payload.get("may_have_been_billed") is True
        )
        if request_outcome:
            try:
                recovery_output = write_recovery_json(output, payload)
            except (OSError, UnicodeError, TypeError, ValueError) as recovery_exc:
                recovery_error = type(recovery_exc).__name__
        report = protected_transition_payload(
            message,
            payload,
            output,
            recovery_output=recovery_output,
        )
        if recovery_error is not None:
            report.update(
                {
                    "recovery_write_failed": True,
                    "recovery_write_error": recovery_error,
                    "unwritten_payload": redact_rewrite_target_text(payload),
                }
            )
        safe_emit(report, None)
        return EXIT_CONFIG


def main() -> int:
    args = parse_args()
    input_text: str | None = None
    api_attempt_reserved = False
    live_request_started = False
    try:
        ensure_output_distinct(args.output, [args.input, args.response])
    except (OSError, ConfigurationError) as exc:
        return finish(error_payload("configuration", str(exc), None), None, EXIT_CONFIG)

    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ConfigurationError("--timeout must be positive")
        if args.response and args.live_api:
            raise ConfigurationError("choose exactly one of --response or --live-api")
        if args.web_response and not args.response:
            raise ConfigurationError("--web-response requires --response")
        if args.live_api and args.output is None:
            raise ConfigurationError("--output is required with --live-api")
        if args.input:
            input_text = read_text(args.input, MAX_TEXT_BYTES)
            if not input_text.strip():
                raise InvalidData("input text is empty")

        if not args.response and not args.live_api:
            if input_text is None:
                raise ConfigurationError("--input is required for webpage mode")
            return finish(
                webpage_handoff_payload(input_text), args.output, EXIT_TEMPORARY
            )

        if args.response:
            if input_text is None:
                raise ConfigurationError("--input is required with --response")
            raw = read_text(args.response, MAX_RESPONSE_BYTES)
            loaded = loads_strict(raw)
            if not isinstance(loaded, dict):
                raise InvalidData("response root must be an object")
            source = "official_webpage" if args.web_response else "saved_response"
        else:
            if input_text is None:
                raise ConfigurationError("--input is required in API mode")
            if args.output is None:  # guarded above; keeps the type invariant explicit.
                raise ConfigurationError("--output is required with --live-api")
            try:
                intent = submission_intent_payload(input_text)
                reserve_submission_intent(args.output, intent)
                api_attempt_reserved = True
            except ProtectedOutput as exc:
                return finish(
                    protected_transition_payload(exc, intent, args.output),
                    None,
                    EXIT_CONFIG,
                )
            except (OSError, UnicodeError, TypeError, ValueError) as exc:
                return finish(
                    error_payload(
                        "output_preflight",
                        f"could not reserve output before request: {type(exc).__name__}",
                        input_text,
                    ),
                    None,
                    EXIT_INVALID,
                )
            gateway, api_key = resolve_api_config()
            live_request_started = True
            loaded = call_api(input_text, args.timeout, gateway, api_key)
            source = "official_api"

        payload = evaluate(
            loaded,
            input_text=input_text,
            source=source,
            include_segment_text=args.include_segment_text,
        )
        return finish(
            payload,
            args.output,
            EXIT_PASS if payload["passed"] else EXIT_REVISE,
            allow_submission_intent=api_attempt_reserved,
        )
    except PendingResult as exc:
        payload = {
            "schema_version": 1,
            "scope": "zhuque_detector_gate",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "verdict": "PENDING",
            "adapter": (
                "official_api" if live_request_started else "saved_response_validation"
            ),
            "passed": False,
            "overall_article_finalizable": False,
            "status": exc.status,
            "task_id": exc.task_id,
            "input_sha256": sha256_text(input_text) if input_text is not None else None,
        }
        if live_request_started:
            payload.update(
                {
                    "request_attempted": True,
                    "request_completed": True,
                    "may_have_been_billed": True,
                    "do_not_retry_same_text": True,
                }
            )
        return finish(
            payload,
            args.output,
            EXIT_TEMPORARY,
            allow_submission_intent=api_attempt_reserved,
        )
    except BlockedOperation as exc:
        return finish(
            blocked_payload(
                exc.category,
                str(exc),
                input_text,
                request_attempted=live_request_started,
            ),
            args.output,
            EXIT_CONFIG,
            allow_submission_intent=api_attempt_reserved,
        )
    except ResultUnknown as exc:
        return finish(
            result_unknown_payload(exc.category, str(exc), input_text),
            args.output,
            EXIT_TEMPORARY,
            allow_submission_intent=api_attempt_reserved,
        )
    except ConfigurationError as exc:
        return finish(
            error_payload(
                "configuration",
                str(exc),
                input_text,
                request_attempted=live_request_started,
                may_have_been_billed=live_request_started,
            ),
            args.output,
            EXIT_CONFIG,
            allow_submission_intent=api_attempt_reserved,
        )
    except (OSError, UnicodeError, InvalidData) as exc:
        return finish(
            error_payload(
                "invalid_data",
                str(exc),
                input_text,
                request_attempted=live_request_started,
                may_have_been_billed=live_request_started,
            ),
            args.output,
            EXIT_INVALID,
            allow_submission_intent=api_attempt_reserved,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary must fail closed without a traceback.
        return finish(
            error_payload(
                "internal",
                f"unexpected {type(exc).__name__}",
                input_text,
                request_attempted=live_request_started,
                may_have_been_billed=live_request_started,
            ),
            args.output,
            EXIT_INVALID,
            allow_submission_intent=api_attempt_reserved,
        )


if __name__ == "__main__":
    raise SystemExit(main())
