#!/usr/bin/env python3
"""Snapshot and compare mechanically protected facts in Chinese prose.

This is a narrow guard, not a semantic verifier. It protects exact numerical,
date, policy-title, URL, and user-supplied tokens. The calling agent must still
review names, attribution, conditions, negation, tense, and policy force.

Exit codes: 0 pass, 10 review, 20 data or I/O error, and 2 for invalid
command-line syntax reported by argparse. Requires Python 3.11 or newer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections import Counter, deque
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_DIFFERENT = 10
EXIT_ERROR = 20
if sys.version_info < (3, 11):
    print("fact_guard requires Python 3.11 or newer", file=sys.stderr)
    raise SystemExit(EXIT_ERROR)
MAX_TEXT_BYTES = 25 * 1024 * 1024
MAX_CUSTOM_TERMS = 1_000
MAX_CUSTOM_TERM_LENGTH = 500
MAX_CUSTOM_TERM_CHARACTERS = 100_000
SIGN_MARK = r"[-+＋－−正负]?"
UNSIGNED_NUMBER = r"(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
ARABIC_LEFT_BOUNDARY = r"(?<!\d)(?<!\d\.)"
ARABIC_RIGHT_BOUNDARY = r"(?!\d)(?!\.\d)"
SIGNED_NUMBER = (
    rf"{ARABIC_LEFT_BOUNDARY}{SIGN_MARK}{UNSIGNED_NUMBER}{ARABIC_RIGHT_BOUNDARY}"
)
CHINESE_DIGIT_CHARS = "零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億"
CHINESE_DIGITS = rf"[{CHINESE_DIGIT_CHARS}]+"
CHINESE_NUMBER = (
    rf"(?<![{CHINESE_DIGIT_CHARS}点點])[正负]?{CHINESE_DIGITS}"
    rf"(?:[点點]{CHINESE_DIGITS})?(?![{CHINESE_DIGIT_CHARS}点點])"
)
CHINESE_OR_ARABIC_NUMBER = rf"(?:{CHINESE_NUMBER}|{SIGNED_NUMBER})"
QUALIFIER_REQUIRED = (
    r"(?:不超过|不低于|不高于|不少于|不多于|不满|不到|最多|最少|"
    r"超过|不足|至少|至多|大于|小于|高于|低于|多于|少于|约|近|逾|"
    r">=|<=|≥|≤|>|<)"
)
QUALIFIER_PREFIX = rf"(?:{QUALIFIER_REQUIRED})?"
WS = r"\s*+"
QUANTITY_SUFFIX = r"(?:及以上|及以下|以内|以外|以上|以下|左右|余|多)?"
QUANTITY_UNIT = (
    r"(?:个百分点|平方公里|平方千米|平方米|立方公里|立方米|吨公里|"
    r"千瓦时|摄氏度|公顷|公斤|千克|毫升|兆瓦|千瓦|公里|千米|个月|小时|分钟|人次|台次|"
    r"季度|阶段|人|户|家|项目|项|次数|次|件|批|名|所|台|套|米|亩|吨|天|"
    r"日|周|月|年|个|条|章|款|倍|期|号|级|类|场|笔|届|轮|等奖|奖|份|页|"
    r"册|座|辆|例|起|宗|部|篇|门|科|种|升|克|度)"
)
CURRENCY_UNIT = r"(?:美元|港元|欧元|元)"
PERCENT_UNIT = r"(?:%|％|‰)"
SCALE_UNIT = r"(?:万亿|萬億|万|萬|亿|億)?"
SCALED_SUFFIX = rf"{QUANTITY_SUFFIX}\s*+{SCALE_UNIT}\s*+{QUANTITY_SUFFIX}"
MEASURE_TAIL = (
    rf"(?:{SCALED_SUFFIX}\s*+{CURRENCY_UNIT}\s*+{QUANTITY_SUFFIX}"
    rf"|{PERCENT_UNIT}\s*+{QUANTITY_SUFFIX}"
    rf"|{SCALED_SUFFIX}\s*+{QUANTITY_UNIT}\s*+{QUANTITY_SUFFIX})"
)


PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pending_marker", re.compile(r"【(?:待核实|待补)[^】\n]{0,80}】")),
    ("document_title", re.compile(r"《(?:[^《》\n]|《[^《》\n]*》)+》")),
    ("url", re.compile(r"https?://[^\s<>\]\[\)（），。；》]+")),
    (
        "date",
        re.compile(
            r"(?<!\d)(?<!\d\.)(?:"
            r"(?:19|20)\d{2}年(?:(?:0?[1-9]|1[0-2])月"
            r"(?:(?:0?[1-9]|[12]\d|3[01])日?)?)?"
            r"|(?:19|20)\d{2}[./-](?:0?[1-9]|1[0-2])"
            r"(?:[./-](?:0?[1-9]|[12]\d|3[01]))?"
            r"(?![\d\u3400-\u9fff%％])"
            r"|(?:19|20)\d{2}(?!\.\d)(?![\d\u3400-\u9fff%％])"
            r")(?!\d)"
        ),
    ),
    (
        "numeric_range",
        re.compile(
            rf"(?=\S){QUALIFIER_PREFIX}{WS}{SIGN_MARK}{WS}"
            rf"(?:人民币{WS})?[¥￥]?{WS}{CHINESE_OR_ARABIC_NUMBER}{WS}"
            rf"(?:至|到|—|–|-|~|～){WS}{CHINESE_OR_ARABIC_NUMBER}{WS}"
            rf"(?:{MEASURE_TAIL})?"
        ),
    ),
    (
        "numeric_list",
        re.compile(
            rf"{CHINESE_OR_ARABIC_NUMBER}(?:{WS}[、,，]{WS}"
            rf"{CHINESE_OR_ARABIC_NUMBER})+{WS}(?:{MEASURE_TAIL})?"
        ),
    ),
    (
        "numeric_ratio",
        re.compile(
            rf"{CHINESE_OR_ARABIC_NUMBER}{WS}(?:比|:|：){WS}"
            rf"{CHINESE_OR_ARABIC_NUMBER}{WS}(?:{MEASURE_TAIL})?"
        ),
    ),
    (
        "percentage",
        re.compile(
            rf"(?=\S){QUALIFIER_PREFIX}{WS}{SIGNED_NUMBER}{WS}"
            rf"{PERCENT_UNIT}{WS}{QUANTITY_SUFFIX}"
        ),
    ),
    (
        "money",
        re.compile(
            rf"(?=\S){QUALIFIER_PREFIX}{WS}{SIGN_MARK}{WS}"
            rf"(?:人民币{WS})?[¥￥]?{WS}{SIGNED_NUMBER}{WS}"
            rf"{SCALED_SUFFIX}{WS}{CURRENCY_UNIT}{WS}{QUANTITY_SUFFIX}"
        ),
    ),
    (
        "rank_or_clause",
        re.compile(rf"(?:排名|位列|第){WS}{SIGNED_NUMBER}{WS}(?:名|位|项|条|章|款)?"),
    ),
    (
        "quantity",
        re.compile(
            rf"(?=\S){QUALIFIER_PREFIX}{WS}{SIGNED_NUMBER}{WS}"
            rf"{SCALED_SUFFIX}{WS}{QUANTITY_UNIT}{WS}"
            rf"{QUANTITY_SUFFIX}"
        ),
    ),
    (
        "chinese_percentage",
        re.compile(
            rf"(?=\S){QUALIFIER_PREFIX}{WS}(?:百分之|千分之|万分之){WS}"
            rf"(?:{CHINESE_NUMBER}|{SIGNED_NUMBER}){WS}{QUANTITY_SUFFIX}"
        ),
    ),
    (
        "chinese_fraction",
        re.compile(
            rf"(?=\S){QUALIFIER_PREFIX}{WS}{CHINESE_OR_ARABIC_NUMBER}{WS}分之{WS}"
            rf"{CHINESE_OR_ARABIC_NUMBER}{WS}{QUANTITY_SUFFIX}"
        ),
    ),
    (
        "chinese_proportion",
        re.compile(
            rf"(?=\S){QUALIFIER_PREFIX}{WS}{CHINESE_NUMBER}{WS}成"
            rf"(?:{WS}(?:{CHINESE_DIGITS}|半))?{WS}{QUANTITY_SUFFIX}"
        ),
    ),
    (
        "chinese_date",
        re.compile(
            rf"{CHINESE_NUMBER}年(?:{CHINESE_NUMBER}月(?:{CHINESE_NUMBER}日)?)?"
        ),
    ),
    (
        "chinese_ordinal",
        re.compile(rf"第{WS}{CHINESE_NUMBER}{WS}(?:章|条|款|项|目|节|名|位|次)"),
    ),
    (
        "chinese_rank",
        re.compile(
            rf"(?:(?:排名|位列){WS}第?{WS}{CHINESE_NUMBER}{WS}(?:名|位)?"
            rf"|第{WS}{CHINESE_NUMBER})"
        ),
    ),
    (
        "chinese_money",
        re.compile(
            rf"(?=\S){QUALIFIER_PREFIX}{WS}{SIGN_MARK}{WS}(?:人民币{WS})?"
            rf"[¥￥]?{WS}{CHINESE_NUMBER}{WS}{SCALED_SUFFIX}{WS}"
            rf"{CURRENCY_UNIT}{WS}{QUANTITY_SUFFIX}"
        ),
    ),
    (
        "chinese_quantity",
        re.compile(
            rf"(?=\S){QUALIFIER_PREFIX}{WS}{CHINESE_NUMBER}{WS}{QUANTITY_SUFFIX}{WS}"
            rf"{SCALE_UNIT}{WS}{QUANTITY_SUFFIX}{WS}{QUANTITY_UNIT}{WS}{QUANTITY_SUFFIX}"
        ),
    ),
    (
        "chinese_decimal",
        re.compile(
            rf"(?<![{CHINESE_DIGIT_CHARS}点點])[正负]?{CHINESE_DIGITS}"
            rf"[点點]{CHINESE_DIGITS}(?![{CHINESE_DIGIT_CHARS}点點])"
        ),
    ),
    (
        "contextual_chinese_number",
        re.compile(
            rf"(?:系数|比值|数值|数量|总数|合计|总计|累计|共|计|为)"
            rf"{WS}(?:为|有)?{WS}{CHINESE_NUMBER}(?![\d\u3400-\u9fff])"
        ),
    ),
    (
        "qualified_number",
        re.compile(rf"{QUALIFIER_REQUIRED}{WS}(?:{SIGNED_NUMBER}|{CHINESE_NUMBER})"),
    ),
    (
        "number",
        re.compile(SIGNED_NUMBER),
    ),
]

SEMANTIC_TERMS = (
    "不得",
    "不应",
    "不能",
    "未",
    "无需",
    "必须",
    "应当",
    "应",
    "可以",
    "可",
    "拟",
    "计划",
    "预计",
    "已",
)


class GuardError(ValueError):
    pass


def read_text(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        with handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise GuardError(f"input must be a regular file: {path}")
            raw = handle.read(MAX_TEXT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > MAX_TEXT_BYTES:
        raise GuardError(f"input exceeds {MAX_TEXT_BYTES} bytes: {path}")
    return raw.decode("utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reject_constant(value: str) -> None:
    raise GuardError(f"non-finite JSON number is not allowed: {value}")


def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GuardError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def loads_strict(raw: str) -> object:
    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except GuardError:
        raise
    except (ValueError, RecursionError) as exc:
        raise GuardError(f"invalid JSON: {exc}") from exc


def validate_custom_terms(terms: object) -> list[str]:
    if not isinstance(terms, list) or not all(isinstance(x, str) for x in terms):
        raise GuardError("custom_terms must be a string list")
    if len(terms) > MAX_CUSTOM_TERMS:
        raise GuardError(f"custom_terms exceeds {MAX_CUSTOM_TERMS} entries")
    if any(not term or len(term) > MAX_CUSTOM_TERM_LENGTH for term in terms):
        raise GuardError(
            f"custom terms must contain 1 to {MAX_CUSTOM_TERM_LENGTH} characters"
        )
    if len(set(terms)) != len(terms):
        raise GuardError("custom_terms must not contain duplicates")
    if sum(map(len, terms)) > MAX_CUSTOM_TERM_CHARACTERS:
        raise GuardError(
            f"custom_terms exceed {MAX_CUSTOM_TERM_CHARACTERS} total characters"
        )
    return terms


def load_custom_terms(path: Path | None) -> list[str]:
    if path is None:
        return []
    terms: list[str] = []
    for raw in read_text(path).splitlines():
        term = raw.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    return validate_custom_terms(list(dict.fromkeys(terms)))


def count_overlapping_terms(text: str, terms: list[str]) -> dict[str, int]:
    """Count all term occurrences with an Aho-Corasick failure automaton."""

    if not terms:
        return {}
    transitions: list[dict[str, int]] = [{}]
    failures = [0]
    terminal_states: dict[str, int] = {}
    for term in terms:
        state = 0
        for character in term:
            next_state = transitions[state].get(character)
            if next_state is None:
                next_state = len(transitions)
                transitions[state][character] = next_state
                transitions.append({})
                failures.append(0)
            state = next_state
        terminal_states[term] = state

    queue: deque[int] = deque(transitions[0].values())
    breadth_first = [0, *queue]
    while queue:
        state = queue.popleft()
        for character, next_state in transitions[state].items():
            fallback = failures[state]
            while fallback and character not in transitions[fallback]:
                fallback = failures[fallback]
            failures[next_state] = transitions[fallback].get(character, 0)
            queue.append(next_state)
            breadth_first.append(next_state)

    visits = [0] * len(transitions)
    state = 0
    for character in text:
        while state and character not in transitions[state]:
            state = failures[state]
        state = transitions[state].get(character, 0)
        visits[state] += 1
    for state in reversed(breadth_first[1:]):
        visits[failures[state]] += visits[state]
    return {term: visits[state] for term, state in terminal_states.items()}


def extract_tokens(
    text: str, custom_terms: Iterable[str] = ()
) -> Counter[tuple[str, str]]:
    occupied = bytearray(len(text))
    found: Counter[tuple[str, str]] = Counter()

    def overlaps(start: int, end: int) -> bool:
        return occupied.find(b"\x01", start, end) != -1

    for category, pattern in PATTERNS:
        for match in pattern.finditer(text):
            if overlaps(match.start(), match.end()):
                continue
            token = re.sub(r"\s+", "", match.group(0))
            found[(category, token)] += 1
            occupied[match.start() : match.end()] = b"\x01" * (
                match.end() - match.start()
            )

    terms = validate_custom_terms(list(custom_terms))
    for term, count in count_overlapping_terms(text, terms).items():
        if count:
            found[("custom", term)] = count
    return found


def semantic_counts(text: str) -> dict[str, int]:
    return {term: text.count(term) for term in SEMANTIC_TERMS if text.count(term)}


def serialize_counter(counter: Counter[tuple[str, str]]) -> list[dict[str, object]]:
    return [
        {"category": category, "text": token, "count": count}
        for (category, token), count in sorted(counter.items())
    ]


def deserialize_counter(items: object) -> Counter[tuple[str, str]]:
    if not isinstance(items, list):
        raise GuardError("manifest tokens must be a list")
    counter: Counter[tuple[str, str]] = Counter()
    for item in items:
        if not isinstance(item, dict):
            raise GuardError("manifest token entry must be an object")
        category = item.get("category")
        token = item.get("text")
        count = item.get("count")
        if (
            not isinstance(category, str)
            or not category
            or not isinstance(token, str)
            or not token
        ):
            raise GuardError("manifest token category and text must be strings")
        allowed_categories = {name for name, _ in PATTERNS} | {"custom"}
        if category not in allowed_categories:
            raise GuardError(f"unknown manifest token category: {category}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise GuardError("manifest token count must be a positive integer")
        key = (category, token)
        if key in counter:
            raise GuardError(f"duplicate manifest token: {category}:{token}")
        counter[key] = count
    return counter


def validate_manifest(manifest: dict[str, object]) -> None:
    version = manifest.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise GuardError("unsupported fact manifest schema_version")
    source_hash = manifest.get("source_sha256")
    if not isinstance(source_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", source_hash
    ):
        raise GuardError("manifest source_sha256 must be lowercase SHA-256")
    source_size = manifest.get("source_utf8_bytes")
    if (
        isinstance(source_size, bool)
        or not isinstance(source_size, int)
        or not 0 <= source_size <= MAX_TEXT_BYTES
    ):
        raise GuardError("manifest source_utf8_bytes is invalid")
    custom_terms = validate_custom_terms(manifest.get("custom_terms"))
    tokens = deserialize_counter(manifest.get("tokens"))
    for category, token in tokens:
        if category == "custom" and token not in custom_terms:
            raise GuardError("custom manifest token is absent from custom_terms")
    signals = manifest.get("semantic_signals")
    if not isinstance(signals, dict):
        raise GuardError("manifest semantic_signals must be an object")
    for term, count in signals.items():
        if term not in SEMANTIC_TERMS:
            raise GuardError(f"unknown semantic signal: {term}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise GuardError("semantic signal counts must be positive integers")
    if not isinstance(manifest.get("created_at"), str):
        raise GuardError("manifest created_at must be a string")
    if not isinstance(manifest.get("scope_note"), str):
        raise GuardError("manifest scope_note must be a string")


def write_json(path: Path, payload: dict[str, object]) -> None:
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


def ensure_output_distinct(output: Path | None, inputs: Iterable[Path | None]) -> None:
    if output is None:
        return
    expanded_output = output.expanduser()
    resolved_output = expanded_output.resolve(strict=False)
    for input_path in inputs:
        if input_path is None:
            continue
        expanded_input = input_path.expanduser()
        if resolved_output == expanded_input.resolve(strict=False):
            raise GuardError(f"output must not overwrite input: {input_path}")
        try:
            if expanded_output.exists() and os.path.samefile(
                expanded_output, expanded_input
            ):
                raise GuardError(f"output must not overwrite input: {input_path}")
        except FileNotFoundError:
            pass


def build_manifest(text: str, custom_terms: list[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": sha256_text(text),
        "source_utf8_bytes": len(text.encode("utf-8")),
        "custom_terms": custom_terms,
        "tokens": serialize_counter(extract_tokens(text, custom_terms)),
        "semantic_signals": semantic_counts(text),
        "scope_note": (
            "Mechanical exact-token guard only. Manually verify names, attribution, "
            "conditions, negation, tense, policy force, and standalone Chinese "
            "numerals without a recognized cue or unit."
        ),
    }


def compare(text: str, manifest: dict[str, object]) -> dict[str, object]:
    validate_manifest(manifest)
    expected = deserialize_counter(manifest.get("tokens"))
    custom_terms = validate_custom_terms(manifest.get("custom_terms"))
    actual = extract_tokens(text, custom_terms)
    missing = expected - actual
    added = actual - expected

    prior_signals = manifest.get("semantic_signals", {})
    if not isinstance(prior_signals, dict):
        raise GuardError("manifest semantic_signals must be an object")
    current_signals = semantic_counts(text)
    signal_changes = []
    for term in sorted(set(prior_signals) | set(current_signals)):
        before = prior_signals.get(term, 0)
        after = current_signals.get(term, 0)
        if before != after:
            signal_changes.append({"term": term, "before": before, "after": after})

    passed = not missing and not added and not signal_changes
    return {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "PASS" if passed else "REVIEW",
        "passed": passed,
        "candidate_sha256": sha256_text(text),
        "source_sha256": manifest.get("source_sha256"),
        "missing_tokens": serialize_counter(missing),
        "added_tokens": serialize_counter(added),
        "semantic_signal_changes": signal_changes,
        "manual_review_required": [
            "names and organizations",
            "source attribution and quotations",
            "conditions and exceptions",
            "negation",
            "completed versus planned status",
            "policy force such as 可, 应, 必须, 拟, 不得",
            "standalone Chinese numerals without a recognized cue or unit",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="create a fact-token manifest")
    snapshot.add_argument("--input", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--protect-file", type=Path)

    check = subparsers.add_parser("check", help="compare a candidate with a manifest")
    check.add_argument("--input", type=Path, required=True)
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "snapshot":
            ensure_output_distinct(args.output, (args.input, args.protect_file))
        else:
            ensure_output_distinct(args.output, (args.input, args.manifest))
        text = read_text(args.input)
        if args.command == "snapshot":
            payload = build_manifest(text, load_custom_terms(args.protect_file))
            write_json(args.output, payload)
            print(f"manifest={args.output} sha256={payload['source_sha256']}")
            return EXIT_OK

        manifest_raw = read_text(args.manifest)
        manifest = loads_strict(manifest_raw)
        if not isinstance(manifest, dict):
            raise GuardError("manifest root must be an object")
        payload = compare(text, manifest)
        if args.output:
            write_json(args.output, payload)
            print(f"verdict={payload['verdict']} output={args.output}")
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return EXIT_OK if payload["passed"] else EXIT_DIFFERENT
    except (OSError, UnicodeError, json.JSONDecodeError, GuardError) as exc:
        print(f"fact_guard error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
