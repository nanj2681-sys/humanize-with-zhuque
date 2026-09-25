#!/usr/bin/env python3
"""Call Tencent Zhuque text detection once or evaluate a bound saved response.

Exit codes:
  2  invalid command-line syntax (argparse)
  0  PASS
  10 REVISE
  20 invalid response or data
  21 result unknown or asynchronous PENDING state
  22 blocked operation or invalid configuration

The API key is read only from ZHUQUE_API_KEY. The gateway is read from
ZHUQUE_GATEWAY. This script never retries a request that could consume quota or
incur charges. Saved responses must be bound to --input by both a matching
input_sha256 and exact full segment coverage. Neither mechanism is sufficient
by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
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
    if not raw_gateway or not api_key:
        raise BlockedOperation(
            "missing_credentials",
            "set ZHUQUE_GATEWAY and ZHUQUE_API_KEY before API mode",
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

    if source == "saved_response":
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
    return {
        "schema_version": 1,
        "scope": "zhuque_detector_gate",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "BLOCKED",
        "passed": False,
        "overall_article_finalizable": False,
        "blocker_category": category,
        "blocker": message,
        "input_sha256": sha256_text(input_text) if input_text is not None else None,
        "request_attempted": request_attempted,
        "may_have_been_billed": False,
        "retry_allowed": False,
    }


def result_unknown_payload(
    category: str, message: str, input_text: str | None
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": "zhuque_detector_gate",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "RESULT_UNKNOWN",
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
        "passed": False,
        "overall_article_finalizable": False,
        "input_sha256": sha256_text(input_text),
        "request_attempted": False,
        "may_have_been_billed": False,
        "retry_allowed": False,
        "operator_action": "if this file remains, verify provider state before retrying",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, help="UTF-8 canonical text; required in API mode"
    )
    parser.add_argument(
        "--response",
        type=Path,
        help="evaluate a saved JSON response without network access",
    )
    parser.add_argument(
        "--live-api",
        action="store_true",
        help="authorize one live request that may use quota or incur charges",
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


def safe_emit(payload: dict[str, Any], output: Path | None) -> bool:
    try:
        emit(payload, output)
        return True
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        fallback = json.loads(json.dumps(payload, ensure_ascii=False))
        targets = fallback.get("rewrite_targets")
        if isinstance(targets, list):
            for target in targets:
                if isinstance(target, dict):
                    target.pop("text", None)
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


def finish(payload: dict[str, Any], output: Path | None, exit_code: int) -> int:
    return exit_code if safe_emit(payload, output) else EXIT_INVALID


def main() -> int:
    args = parse_args()
    input_text: str | None = None
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
        if not args.response and not args.live_api:
            raise ConfigurationError("choose exactly one of --response or --live-api")
        if args.live_api and args.output is None:
            raise ConfigurationError("--output is required with --live-api")
        if args.input:
            input_text = read_text(args.input, MAX_TEXT_BYTES)
            if not input_text.strip():
                raise InvalidData("input text is empty")

        if args.response:
            if input_text is None:
                raise ConfigurationError("--input is required with --response")
            raw = read_text(args.response, MAX_RESPONSE_BYTES)
            loaded = loads_strict(raw)
            if not isinstance(loaded, dict):
                raise InvalidData("response root must be an object")
            source = "saved_response"
        else:
            if input_text is None:
                raise ConfigurationError("--input is required in API mode")
            gateway, api_key = resolve_api_config()
            if args.output is None:  # guarded above; keeps the type invariant explicit.
                raise ConfigurationError("--output is required with --live-api")
            try:
                write_json(args.output, submission_intent_payload(input_text))
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
        )
    except PendingResult as exc:
        payload = {
            "schema_version": 1,
            "scope": "zhuque_detector_gate",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "verdict": "PENDING",
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
        return finish(payload, args.output, EXIT_TEMPORARY)
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
        )
    except ResultUnknown as exc:
        return finish(
            result_unknown_payload(exc.category, str(exc), input_text),
            args.output,
            EXIT_TEMPORARY,
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
        )


if __name__ == "__main__":
    raise SystemExit(main())
