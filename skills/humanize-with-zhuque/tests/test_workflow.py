from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SKILL_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fact_guard = load_module("tested_fact_guard", SKILL_ROOT / "scripts/fact_guard.py")
zhuque_gate = load_module("tested_zhuque_gate", SKILL_ROOT / "scripts/zhuque_gate.py")


def make_response(
    text: str,
    *,
    human: float = 1.0,
    ai: float = 0.0,
    suspected: float = 0.0,
    label: int = 0,
) -> dict[str, object]:
    return {
        "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "status": "success",
        "labels_ratio": {"0": human, "1": ai, "2": suspected},
        "segment_labels": [
            {
                "label": label,
                "conf": 0.9,
                "order": 1,
                "position": [0, len(text)],
                "text": text,
            }
        ],
    }


class FactGuardTests(unittest.TestCase):
    def assert_review(self, source: str, candidate: str) -> None:
        manifest = fact_guard.build_manifest(source, [])
        result = fact_guard.compare(candidate, manifest)
        self.assertFalse(result["passed"], (source, candidate, result))
        self.assertEqual(result["verdict"], "REVIEW")

    def test_unchanged_text_passes(self) -> None:
        text = "2024年5月投入100万元，连续三年完成12项检查。"
        result = fact_guard.compare(text, fact_guard.build_manifest(text, []))
        self.assertTrue(result["passed"])

    def test_arabic_fact_regressions(self) -> None:
        cases = [
            ("2024年5月", "2024年5日"),
            ("2024年", "2024"),
            ("2024万元", "2024元"),
            ("2024人", "2024户"),
            ("1—2人", "9—2人"),
            ("1.5", "2.5"),
            (".5", ".6"),
            ("－100万元", "100万元"),
            ("负100万元", "100万元"),
            ("2024.5元", "2024.5"),
            ("2024.5人", "2024.5"),
            ("2024.5%", "2024.5"),
            ("2024.5元", "2024.5人"),
            ("2024.5元", "2024.5美元"),
            ("2024.5%", "2024.5人"),
            ("1,000万元", "2,000万元"),
            ("-10%", "10%"),
            ("排名3", "排名4"),
            ("3个月", "3个"),
            ("A.100元", "A.900元"),
            ("A.2025年", "A.2026年"),
            ("100.", "900."),
        ]
        for source, candidate in cases:
            with self.subTest(source=source, candidate=candidate):
                self.assert_review(source, candidate)

    def test_chinese_numeral_regressions(self) -> None:
        cases = [
            ("连续三年", "连续四年"),
            ("第十四条", "第十五条"),
            ("增长百分之三", "增长百分之四"),
            ("增长百分之3", "增长3"),
            ("千分之5", "5"),
            ("负一百万元", "一百万元"),
            ("位列第十", "位列第九"),
            ("排名第三", "排名第四"),
            ("占比三成", "占比四成"),
            ("占四分之一", "占三分之一"),
            ("二〇二四年五月一日", "二〇二四年五月二日"),
            ("限期三日", "限期四日"),
            ("约三万余人", "约四万余人"),
            ("系数为三点五", "系数为四点五"),
            ("占比三成五", "占比三成六"),
            ("四分之1", "五分之1"),
            ("一季度完成任务", "二季度完成任务"),
            ("启动三期工程", "启动四期工程"),
            ("发布五号文件", "发布六号文件"),
            ("开展三场活动", "开展四场活动"),
            ("采用二级响应", "采用三级响应"),
            ("比例一比二", "比例一比三"),
            ("分为三类", "分为四类"),
            ("三等奖", "四等奖"),
            ("三至五年", "四至五年"),
            ("三、五、七项", "四、五、七项"),
            ("二十多亿元", "三十多亿元"),
            ("一百余万人", "两百余万人"),
            ("二十余万亿元", "二十多万亿元"),
            ("二十余万亿元", "二十万亿元"),
            ("系数为三", "系数为四"),
        ]
        for source, candidate in cases:
            with self.subTest(source=source, candidate=candidate):
                self.assert_review(source, candidate)

    def test_titles_urls_and_semantic_force_are_protected(self) -> None:
        cases = [
            ("《2024年行动方案》", "《2024年完全不同文件》"),
            ("《关于https://example.com/a》", "《其他https://example.com/a》"),
            ("https://example.cn/2024/a", "https://example.cn/2024/b"),
            ("https://example.cn/a;b", "https://example.cn/a;c"),
            ("《外层《内层》标题》", "《外层《内层》新题》"),
            (f"《{'a' * 130}甲》", f"《{'a' * 130}乙》"),
            ("项目必须完成", "项目可以完成"),
        ]
        for source, candidate in cases:
            with self.subTest(source=source, candidate=candidate):
                self.assert_review(source, candidate)

        source = "《办法》和《细则》"
        candidate = "《办法》及《细则》"
        result = fact_guard.compare(candidate, fact_guard.build_manifest(source, []))
        self.assertTrue(result["passed"])

    def test_compound_numeric_measure_suffixes_are_protected(self) -> None:
        cases = [
            ("预算3-5万元", "预算3-5万美元"),
            ("金额3至5美元", "金额3至5欧元"),
            ("3-5万元", "3-5万港元"),
            ("金额3、5、7美元", "金额3、5、7欧元"),
            ("增长3至5%", "增长3至5‰"),
            ("增长3至5%", "增长3至5"),
            ("占比3、5、7%", "占比3、5、7‰"),
            ("100—200万元", "100—200万美元"),
            ("5—10%", "5—10元"),
            ("1、2、3%", "1、2、3元"),
            ("1:2元", "1:2人"),
            ("-￥100元", "￥100元"),
            ("20余万亿元", "20多万亿元"),
            ("20余万亿元", "20万亿元"),
            ("3-5万亿元", "3-5万亿美元"),
            ("1、2、3万亿元", "1、2、3万亿港元"),
            ("1:2万亿元", "1:2万亿人"),
            ("3平方公里", "3公顷"),
            ("1,2,3", "1,9,3"),
            ("甲,2人", "甲,9人"),
            ("A,2025年", "A,2026年"),
            ("A,100元", "A,900元"),
        ]
        for source, candidate in cases:
            with self.subTest(source=source, candidate=candidate):
                self.assert_review(source, candidate)

    def test_fifo_input_is_rejected_without_reading(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO is unavailable on this platform")
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "input.fifo"
            os.mkfifo(fifo)
            with self.assertRaises(fact_guard.GuardError):
                fact_guard.read_text(fifo)

    def test_long_digit_runs_are_processed_in_linear_time(self) -> None:
        started = time.perf_counter()
        fact_guard.extract_tokens("一" * 8_000)
        fact_guard.extract_tokens("1" * 8_000)
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_long_whitespace_failure_paths_are_bounded(self) -> None:
        padding = " " * 1_000
        cases = [
            padding,
            "1" + padding,
            "一" + padding,
            "1-2" + padding,
            "1,2" + padding,
            "1:2" + padding,
            "-￥100" + padding,
        ]
        started = time.perf_counter()
        for text in cases:
            fact_guard.extract_tokens(text)
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_conditions_and_comparison_signs_are_protected(self) -> None:
        cases = [
            ("不超过100人", "超过100人"),
            ("不满100人", "满100人"),
            ("≥100人", "≤100人"),
            ("100人以内", "100人以外"),
        ]
        for source, candidate in cases:
            with self.subTest(source=source, candidate=candidate):
                self.assert_review(source, candidate)

    def test_overlapping_custom_terms_are_counted(self) -> None:
        source = "市项目办负责。"
        manifest = fact_guard.build_manifest(source, ["市项目办", "项目办"])
        result = fact_guard.compare("市办公室负责。", manifest)
        self.assertFalse(result["passed"])
        missing = {
            (item["category"], item["text"]) for item in result["missing_tokens"]
        }
        self.assertIn(("custom", "市项目办"), missing)
        self.assertIn(("custom", "项目办"), missing)

        overlap_manifest = fact_guard.build_manifest("哈哈哈", ["哈哈"])
        self.assertFalse(fact_guard.compare("哈哈", overlap_manifest)["passed"])

    def test_manifest_schema_and_entries_fail_closed(self) -> None:
        with self.assertRaises(fact_guard.GuardError):
            fact_guard.compare("正文", {"schema_version": 2, "tokens": []})
        with self.assertRaises(fact_guard.GuardError):
            fact_guard.deserialize_counter(
                [{"category": "number", "text": "1", "count": 0}]
            )

        baseline = fact_guard.build_manifest("正文", [])
        malformed = []
        for key, value in (
            ("schema_version", True),
            ("source_sha256", None),
            ("source_utf8_bytes", -1),
            ("semantic_signals", {"必须": True}),
        ):
            case = dict(baseline)
            case[key] = value
            malformed.append(case)
        for case in malformed:
            with self.subTest(case=case), self.assertRaises(fact_guard.GuardError):
                fact_guard.compare("正文", case)

        with self.assertRaises(fact_guard.GuardError):
            fact_guard.loads_strict('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(fact_guard.GuardError):
            fact_guard.loads_strict('{"source_utf8_bytes":' + "9" * 5_000 + "}")

    def test_huge_manifest_integer_returns_documented_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            manifest_path = Path(directory) / "manifest.json"
            input_path.write_text("正文", encoding="utf-8")
            manifest_path.write_text(
                '{"source_utf8_bytes":' + "9" * 5_000 + "}", encoding="utf-8"
            )
            argv = [
                "fact_guard.py",
                "check",
                "--input",
                str(input_path),
                "--manifest",
                str(manifest_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(fact_guard.main(), fact_guard.EXIT_ERROR)

    def test_output_cannot_alias_an_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.txt"
            path.write_text("正文", encoding="utf-8")
            with self.assertRaises(fact_guard.GuardError):
                fact_guard.ensure_output_distinct(path, [path])


class ZhuqueGateTests(unittest.TestCase):
    def evaluate_saved(self, payload: dict[str, object], text: str):
        return zhuque_gate.evaluate(
            payload,
            input_text=text,
            source="saved_response",
            include_segment_text=False,
        )

    def test_exact_acceptance_boundary(self) -> None:
        text = "完整正文"
        payload = make_response(text)
        payload["input_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        result = self.evaluate_saved(payload, text)
        self.assertTrue(result["passed"])
        self.assertEqual(result["scope"], "zhuque_detector_gate")
        self.assertFalse(result["overall_article_finalizable"])
        self.assertEqual(
            result["input_binding"], "segment_text_and_position+declared_sha256"
        )
        self.assertEqual(
            result["threshold_version"],
            "human==1;suspected==0;aggregate-ai==0;no-segment-label-1-or-2",
        )

    def test_any_suspected_ratio_fails(self) -> None:
        result = self.evaluate_saved(
            make_response("正文", human=0.9999, ai=0.0, suspected=0.0001), "正文"
        )
        self.assertFalse(result["passed"])
        self.assertIn("human_content_is_100_percent", result["failed_checks"])
        self.assertIn("suspected_ai_is_zero", result["failed_checks"])

    def test_nonzero_aggregate_ai_noise_fails(self) -> None:
        result = self.evaluate_saved(
            make_response("正文", human=0.999, ai=0.001, suspected=0.0), "正文"
        )
        self.assertFalse(result["passed"])
        self.assertIn("human_content_is_100_percent", result["failed_checks"])
        self.assertIn("aggregate_ai_is_zero", result["failed_checks"])

    def test_ratio_sum_tolerance_never_relaxes_the_pass_gate(self) -> None:
        boundary = self.evaluate_saved(
            make_response("正文", human=1.0, ai=0.001, suspected=0.0), "正文"
        )
        self.assertFalse(boundary["passed"])
        self.assertIn("aggregate_ai_is_zero", boundary["failed_checks"])

        with self.assertRaises(zhuque_gate.InvalidData):
            self.evaluate_saved(
                make_response("正文", human=1.0, ai=0.0011, suspected=0.0),
                "正文",
            )

    def test_json_decimal_precision_cannot_round_across_thresholds(self) -> None:
        text = "正文"
        digest = hashlib.sha256(text.encode()).hexdigest()
        segment = (
            '"segment_labels":[{"label":0,"conf":0.9,"order":1,'
            '"position":[0,2],"text":"正文"}]'
        )
        human_below = zhuque_gate.loads_strict(
            '{"input_sha256":"'
            + digest
            + '","status":"success","labels_ratio":'
            + '{"0":0.99999999999999999,"1":0,'
            + '"2":0.00000000000000001},'
            + segment
            + "}"
        )
        human_below_result = self.evaluate_saved(human_below, text)
        self.assertFalse(human_below_result["passed"])
        self.assertEqual(
            human_below_result["metrics"]["human_percent_exact"],
            "99.99999999999999900",
        )

        ai_above_zero = zhuque_gate.loads_strict(
            '{"input_sha256":"'
            + digest
            + '","status":"success","labels_ratio":'
            + '{"0":0.99999999999999999,"1":0.00000000000000001,'
            + '"2":0},'
            + segment
            + "}"
        )
        result = self.evaluate_saved(ai_above_zero, text)
        self.assertFalse(result["passed"])
        self.assertIn("aggregate_ai_is_zero", result["failed_checks"])

    def test_definite_ai_segment_fails(self) -> None:
        result = self.evaluate_saved(
            make_response("正文", human=1.0, ai=0.0, suspected=0.0, label=1),
            "正文",
        )
        self.assertFalse(result["passed"])
        self.assertIn("no_definite_ai_segments", result["failed_checks"])

    def test_suspected_ai_segment_fails_even_with_zero_suspected_ratio(self) -> None:
        result = self.evaluate_saved(
            make_response("正文", human=1.0, ai=0.0, suspected=0.0, label=2),
            "正文",
        )
        self.assertFalse(result["passed"])
        self.assertIn("no_suspected_ai_segments", result["failed_checks"])

    def test_nonzero_aggregate_ai_ratio_fails_without_ai_segment(self) -> None:
        result = self.evaluate_saved(
            make_response("正文", human=0.8, ai=0.2, suspected=0.0, label=0),
            "正文",
        )
        self.assertFalse(result["passed"])
        self.assertIn("aggregate_ai_is_zero", result["failed_checks"])

    def test_self_declared_hash_does_not_override_stale_segments(self) -> None:
        text = "当前正文"
        payload = make_response("完全旧稿")
        payload["input_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        with self.assertRaises(zhuque_gate.InvalidData):
            self.evaluate_saved(payload, text)

    def test_hash_only_is_not_enough(self) -> None:
        text = "当前正文"
        payload = make_response(text)
        payload["input_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        del payload["segment_labels"][0]["text"]
        with self.assertRaises(zhuque_gate.InvalidData):
            self.evaluate_saved(payload, text)

    def test_segments_may_skip_only_whitespace(self) -> None:
        text = "甲\n\n乙"
        payload = {
            "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "status": "success",
            "labels_ratio": {"0": 1.0, "1": 0.0, "2": 0.0},
            "segment_labels": [
                {"label": 0, "position": [0, 1], "text": "甲"},
                {"label": 0, "position": [3, 4], "text": "乙"},
            ],
        }
        self.assertTrue(self.evaluate_saved(payload, text)["passed"])

    def test_saved_response_requires_hash_and_exact_whitespace(self) -> None:
        text = "甲 乙"
        payload = {
            "status": "success",
            "labels_ratio": {"0": 1.0, "1": 0.0, "2": 0.0},
            "segment_labels": [
                {"label": 0, "position": [0, 1], "text": "甲"},
                {"label": 0, "position": [2, 3], "text": "乙"},
            ],
        }
        with self.assertRaises(zhuque_gate.InvalidData):
            self.evaluate_saved(payload, text)

        payload["input_sha256"] = hashlib.sha256("甲\n乙".encode()).hexdigest()
        with self.assertRaises(zhuque_gate.InvalidData):
            self.evaluate_saved(payload, text)

    def test_hash_mismatch_fails_even_with_exact_segments(self) -> None:
        payload = make_response("正文")
        payload["input_sha256"] = "0" * 64
        with self.assertRaises(zhuque_gate.InvalidData):
            self.evaluate_saved(payload, "正文")

    def test_async_envelopes(self) -> None:
        with self.assertRaises(zhuque_gate.PendingResult) as queued:
            zhuque_gate.unwrap_response({"TaskId": "task-1"})
        self.assertEqual(queued.exception.status, "QUEUED")
        with self.assertRaises(zhuque_gate.PendingResult) as processing:
            zhuque_gate.unwrap_response({"TaskId": "task-1", "Status": "PROCESSING"})
        self.assertEqual(processing.exception.status, "PROCESSING")
        with self.assertRaises(zhuque_gate.InvalidData):
            zhuque_gate.unwrap_response(
                {"Status": "SUCCEEDED", "Output": json.dumps(make_response("正文"))}
            )

        inner = make_response("正文")
        envelope = {
            "TaskId": "task-2",
            "Status": "SUCCEEDED",
            "Output": json.dumps(inner, ensure_ascii=False),
        }
        result = self.evaluate_saved(envelope, "正文")
        self.assertTrue(result["passed"])
        self.assertEqual(result["task_id"], "task-2")

    def test_official_response_requires_bound_segments(self) -> None:
        payload = {
            "status": "success",
            "labels_ratio": {"0": 1.0, "1": 0.0, "2": 0.0},
            "segment_labels": [{"label": 0}],
        }
        with self.assertRaises(zhuque_gate.InvalidData):
            zhuque_gate.evaluate(
                payload,
                input_text="正文",
                source="official_api",
                include_segment_text=False,
            )

        payload["segment_labels"][0].update({"position": [0, 99], "text": "正文"})
        with self.assertRaises(zhuque_gate.InvalidData):
            zhuque_gate.evaluate(
                payload,
                input_text="正文",
                source="official_api",
                include_segment_text=False,
            )

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with self.assertRaises(zhuque_gate.InvalidData):
            zhuque_gate.loads_strict('{"status":"success","status":"success"}')
        with self.assertRaises(zhuque_gate.InvalidData):
            zhuque_gate.loads_strict('{"value":' + "9" * 5_000 + "}")

    def test_fifo_response_is_rejected_without_reading(self) -> None:
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO is unavailable on this platform")
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "response.fifo"
            os.mkfifo(fifo)
            with self.assertRaises(zhuque_gate.InvalidData):
                zhuque_gate.read_text(fifo, zhuque_gate.MAX_RESPONSE_BYTES)

    def test_output_failure_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent_file = Path(directory) / "not-a-directory"
            parent_file.write_text("x", encoding="utf-8")
            stderr = io.StringIO()
            payload = {
                "verdict": "PASS",
                "metrics": {"human_percent": 90},
                "request_completed": True,
                "rewrite_targets": [{"text": "sensitive", "label": 2}],
            }
            with contextlib.redirect_stderr(stderr):
                self.assertFalse(
                    zhuque_gate.safe_emit(payload, parent_file / "result.json")
                )
            fallback = json.loads(stderr.getvalue())
            self.assertEqual(fallback["verdict"], "PASS")
            self.assertEqual(fallback["metrics"]["human_percent"], 90)
            self.assertTrue(fallback["do_not_retry_same_text"])
            self.assertNotIn("text", fallback["rewrite_targets"][0])

    def test_default_mode_prepares_official_webpage_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            input_path.write_text("正文", encoding="utf-8")
            environments = (
                {},
                {"ZHUQUE_API_KEY": "key"},
                {
                    "ZHUQUE_GATEWAY": "https://gateway.example",
                    "ZHUQUE_API_KEY": "key",
                },
            )
            for index, environment in enumerate(environments):
                with self.subTest(environment=environment):
                    output_path = Path(directory) / f"web-{index}.json"
                    argv = [
                        "zhuque_gate.py",
                        "--input",
                        str(input_path),
                        "--output",
                        str(output_path),
                    ]
                    with (
                        mock.patch.object(sys, "argv", argv),
                        mock.patch.dict(os.environ, environment, clear=True),
                        mock.patch.object(
                            zhuque_gate,
                            "resolve_api_config",
                            side_effect=AssertionError("API config was inspected"),
                        ),
                        mock.patch.object(
                            zhuque_gate,
                            "call_api",
                            side_effect=AssertionError("network call was attempted"),
                        ),
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        self.assertEqual(
                            zhuque_gate.main(), zhuque_gate.EXIT_TEMPORARY
                        )
                    payload = json.loads(output_path.read_text(encoding="utf-8"))
                    self.assertEqual(payload["verdict"], "PENDING")
                    self.assertEqual(payload["status"], "WEBPAGE_RESULT_REQUIRED")
                    self.assertEqual(payload["adapter"], "official_webpage")
                    self.assertEqual(payload["webpage_url"], zhuque_gate.OFFICIAL_WEB_URL)
                    self.assertFalse(payload["api_credentials_required"])
                    self.assertFalse(payload["request_attempted"])
                    self.assertTrue(payload["submission_allowed"])
                    self.assertFalse(payload["retry_allowed"])

    def test_webpage_handoff_can_be_replaced_by_bound_result(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            response_path = Path(directory) / "visible-result.json"
            current_path = Path(directory) / "detection-current.json"
            input_path.write_text(text, encoding="utf-8")
            response_path.write_text(
                json.dumps(make_response(text), ensure_ascii=False), encoding="utf-8"
            )

            handoff_argv = [
                "zhuque_gate.py",
                "--input",
                str(input_path),
                "--output",
                str(current_path),
            ]
            with (
                mock.patch.object(sys, "argv", handoff_argv),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_TEMPORARY)
            self.assertEqual(
                json.loads(current_path.read_text(encoding="utf-8"))["status"],
                "WEBPAGE_RESULT_REQUIRED",
            )

            response_argv = [
                "zhuque_gate.py",
                "--input",
                str(input_path),
                "--response",
                str(response_path),
                "--web-response",
                "--output",
                str(current_path),
            ]
            with (
                mock.patch.object(sys, "argv", response_argv),
                mock.patch.object(
                    zhuque_gate,
                    "call_api",
                    side_effect=AssertionError("network call was attempted"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_PASS)
            current = json.loads(current_path.read_text(encoding="utf-8"))
            self.assertEqual(current["verdict"], "PASS")
            self.assertEqual(current["source"], "official_webpage")
            self.assertEqual(current["adapter"], "official_webpage")
            self.assertTrue(current["source_evidence_required"])
            self.assertFalse(current["source_verified_by_script"])
            self.assertNotIn("status", current)

    def test_result_unknown_current_state_cannot_be_overwritten(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            response_path = Path(directory) / "visible-result.json"
            current_path = Path(directory) / "detection-current.json"
            input_path.write_text(text, encoding="utf-8")
            response_path.write_text(
                json.dumps(make_response(text), ensure_ascii=False), encoding="utf-8"
            )
            existing = zhuque_gate.result_unknown_payload(
                "network_or_timeout", "request outcome is unknown", text
            )
            current_path.write_text(
                json.dumps(existing, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
            before = current_path.read_bytes()
            argv = [
                "zhuque_gate.py",
                "--input",
                str(input_path),
                "--response",
                str(response_path),
                "--web-response",
                "--output",
                str(current_path),
            ]
            stdout = io.StringIO()
            with (
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_CONFIG)
            self.assertEqual(current_path.read_bytes(), before)
            refusal = json.loads(stdout.getvalue())
            self.assertEqual(refusal["verdict"], "ERROR")
            self.assertEqual(
                refusal["error_category"], "protected_output_transition"
            )

    def test_stale_submission_intent_blocks_a_new_live_request(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            current_path = Path(directory) / "detection-current.json"
            input_path.write_text(text, encoding="utf-8")
            current_path.write_text(
                json.dumps(
                    zhuque_gate.submission_intent_payload(text), ensure_ascii=False
                ),
                encoding="utf-8",
            )
            before = current_path.read_bytes()
            argv = [
                "zhuque_gate.py",
                "--live-api",
                "--input",
                str(input_path),
                "--output",
                str(current_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    zhuque_gate,
                    "resolve_api_config",
                    return_value=("https://gateway.example", "key"),
                ),
                mock.patch.object(
                    zhuque_gate,
                    "call_api",
                    side_effect=AssertionError("duplicate API request was attempted"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_CONFIG)
            self.assertEqual(current_path.read_bytes(), before)

    def test_billable_error_without_adapter_cannot_be_overwritten(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            response_path = Path(directory) / "visible-result.json"
            current_path = Path(directory) / "detection-current.json"
            input_path.write_text(text, encoding="utf-8")
            response_path.write_text(
                json.dumps(make_response(text), ensure_ascii=False), encoding="utf-8"
            )
            existing = zhuque_gate.error_payload(
                "invalid_data",
                "response failed after submission",
                text,
                request_attempted=True,
                may_have_been_billed=True,
            )
            self.assertNotIn("adapter", existing)
            current_path.write_text(
                json.dumps(existing, ensure_ascii=False), encoding="utf-8"
            )
            before = current_path.read_bytes()
            argv = [
                "zhuque_gate.py",
                "--input",
                str(input_path),
                "--response",
                str(response_path),
                "--web-response",
                "--output",
                str(current_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_CONFIG)
            self.assertEqual(current_path.read_bytes(), before)

    def test_api_blocker_can_be_replaced_by_webpage_result(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            response_path = Path(directory) / "visible-result.json"
            current_path = Path(directory) / "detection-current.json"
            input_path.write_text(text, encoding="utf-8")
            response_path.write_text(
                json.dumps(make_response(text), ensure_ascii=False), encoding="utf-8"
            )
            current_path.write_text(
                json.dumps(
                    zhuque_gate.blocked_payload(
                        "missing_api_key",
                        "API key is missing",
                        text,
                        request_attempted=False,
                    ),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            argv = [
                "zhuque_gate.py",
                "--input",
                str(input_path),
                "--response",
                str(response_path),
                "--web-response",
                "--output",
                str(current_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_PASS)
            current = json.loads(current_path.read_text(encoding="utf-8"))
            self.assertEqual(current["verdict"], "PASS")
            self.assertEqual(current["adapter"], "official_webpage")

    def test_api_config_reports_each_missing_variable(self) -> None:
        cases = (
            ({}, "missing_api_credentials"),
            ({"ZHUQUE_GATEWAY": "https://gateway.example"}, "missing_api_key"),
            ({"ZHUQUE_API_KEY": "key"}, "missing_api_gateway"),
        )
        for environment, category in cases:
            with self.subTest(environment=environment):
                with (
                    mock.patch.dict(os.environ, environment, clear=True),
                    self.assertRaises(zhuque_gate.BlockedOperation) as caught,
                ):
                    zhuque_gate.resolve_api_config()
                self.assertEqual(caught.exception.category, category)

    def test_live_api_flag_does_not_accept_abbreviations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            output_path = Path(directory) / "result.json"
            input_path.write_text("正文", encoding="utf-8")
            for abbreviated in ("--l", "--live"):
                with self.subTest(flag=abbreviated):
                    argv = [
                        "zhuque_gate.py",
                        abbreviated,
                        "--input",
                        str(input_path),
                        "--output",
                        str(output_path),
                    ]
                    with (
                        mock.patch.object(sys, "argv", argv),
                        mock.patch.object(
                            zhuque_gate,
                            "call_api",
                            side_effect=AssertionError("network call was attempted"),
                        ),
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit) as caught,
                    ):
                        zhuque_gate.main()
                    self.assertEqual(caught.exception.code, 2)
                    self.assertFalse(output_path.exists())

    def test_api_submission_path_is_reserved_atomically(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "api-attempt.json"
            payload = zhuque_gate.submission_intent_payload(text)
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def reserve() -> None:
                barrier.wait()
                try:
                    zhuque_gate.reserve_submission_intent(output_path, payload)
                except zhuque_gate.ProtectedOutput:
                    outcomes.append("blocked")
                else:
                    outcomes.append("reserved")

            threads = [threading.Thread(target=reserve) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

            self.assertCountEqual(outcomes, ["reserved", "blocked"])
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["verdict"], "SUBMISSION_INTENT")
            self.assertEqual(saved["input_sha256"], hashlib.sha256(text.encode()).hexdigest())

    def test_lock_contention_message_is_request_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "shared-output.json"
            with (
                zhuque_gate.output_lock(output_path),
                self.assertRaises(zhuque_gate.ProtectedOutput) as caught,
                zhuque_gate.output_lock(output_path),
            ):
                self.fail("the second writer unexpectedly acquired the lock")
            self.assertNotIn("no request was made", str(caught.exception))

    def test_finish_locks_transition_through_write(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "shared-output.json"
            transition_checked = threading.Event()
            release_finish = threading.Event()
            outcomes: list[int] = []
            original = zhuque_gate.ensure_output_transition

            def pause_after_check(*args, **kwargs) -> None:
                original(*args, **kwargs)
                transition_checked.set()
                if not release_finish.wait(timeout=5):
                    raise AssertionError("test did not release finish")

            def write_webpage_handoff() -> None:
                outcomes.append(
                    zhuque_gate.finish(
                        zhuque_gate.webpage_handoff_payload(text),
                        output_path,
                        zhuque_gate.EXIT_TEMPORARY,
                    )
                )

            with (
                mock.patch.object(
                    zhuque_gate,
                    "ensure_output_transition",
                    side_effect=pause_after_check,
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                writer = threading.Thread(target=write_webpage_handoff)
                writer.start()
                self.assertTrue(transition_checked.wait(timeout=5))
                try:
                    with self.assertRaises(zhuque_gate.ProtectedOutput):
                        zhuque_gate.reserve_submission_intent(
                            output_path,
                            zhuque_gate.submission_intent_payload(text),
                        )
                finally:
                    release_finish.set()
                writer.join(timeout=5)
                self.assertFalse(writer.is_alive())

            self.assertEqual(outcomes, [zhuque_gate.EXIT_TEMPORARY])
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "WEBPAGE_RESULT_REQUIRED")

    def test_post_request_lock_timeout_preserves_recovery_result(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "api-attempt.json"
            intent = zhuque_gate.submission_intent_payload(text)
            output_path.write_text(json.dumps(intent), encoding="utf-8")
            result = zhuque_gate.result_unknown_payload(
                "network_or_timeout", "request outcome is unknown", text
            )
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    zhuque_gate,
                    "output_lock",
                    side_effect=zhuque_gate.ProtectedOutput("lock stayed busy"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(
                    zhuque_gate.finish(
                        result,
                        output_path,
                        zhuque_gate.EXIT_TEMPORARY,
                        allow_submission_intent=True,
                    ),
                    zhuque_gate.EXIT_CONFIG,
                )

            self.assertEqual(
                json.loads(output_path.read_text(encoding="utf-8"))["verdict"],
                "SUBMISSION_INTENT",
            )
            report = json.loads(stdout.getvalue())
            self.assertNotIn("no request was made", report["error"])
            self.assertTrue(report["request_attempted"])
            self.assertTrue(report["may_have_been_billed"])
            self.assertTrue(report["do_not_retry_same_text"])
            recovery = Path(report["recovery_output"])
            self.assertEqual(json.loads(recovery.read_text(encoding="utf-8")), result)

    def test_recovery_write_failure_redacts_nested_segment_text(self) -> None:
        secret = "CUSTOMER-SECRET-123"
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "api-attempt.json"
            result = zhuque_gate.evaluate(
                make_response(secret, label=2),
                input_text=secret,
                source="official_api",
                include_segment_text=True,
            )
            self.assertEqual(result["rewrite_targets"][0]["text"], secret)
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    zhuque_gate,
                    "output_lock",
                    side_effect=zhuque_gate.ProtectedOutput("lock stayed busy"),
                ),
                mock.patch.object(
                    zhuque_gate,
                    "write_recovery_json",
                    side_effect=OSError("disk unavailable"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(
                    zhuque_gate.finish(
                        result,
                        output_path,
                        zhuque_gate.EXIT_TEMPORARY,
                        allow_submission_intent=True,
                    ),
                    zhuque_gate.EXIT_CONFIG,
                )

            raw_report = stdout.getvalue()
            self.assertNotIn(secret, raw_report)
            report = json.loads(raw_report)
            self.assertTrue(report["recovery_write_failed"])
            target = report["unwritten_payload"]["rewrite_targets"][0]
            self.assertNotIn("text", target)
            self.assertEqual(target["position"], [0, len(secret)])
            self.assertTrue(report["may_have_been_billed"])
            self.assertTrue(report["do_not_retry_same_text"])

    def test_live_api_requires_explicit_mode_and_preserves_result(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            output_path = Path(directory) / "result.json"
            input_path.write_text(text, encoding="utf-8")

            def fake_call_api(
                submitted: str, timeout: float, gateway: str, api_key: str
            ):
                self.assertEqual(submitted, text)
                self.assertGreater(timeout, 0)
                self.assertEqual(gateway, "https://gateway.example")
                self.assertEqual(api_key, "key")
                intent = json.loads(output_path.read_text(encoding="utf-8"))
                self.assertEqual(intent["verdict"], "SUBMISSION_INTENT")
                return make_response(text)

            argv = [
                "zhuque_gate.py",
                "--live-api",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    zhuque_gate,
                    "resolve_api_config",
                    return_value=("https://gateway.example", "key"),
                ),
                mock.patch.object(zhuque_gate, "call_api", side_effect=fake_call_api),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_PASS)
            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["verdict"], "PASS")
            self.assertEqual(result["adapter"], "official_api")
            self.assertTrue(result["request_completed"])
            self.assertTrue(result["do_not_retry_same_text"])

    def test_live_api_blocker_and_unknown_result_states(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            output_path = Path(directory) / "result.json"
            input_path.write_text(text, encoding="utf-8")
            argv = [
                "zhuque_gate.py",
                "--live-api",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
            ]

            def missing_config():
                reserved = json.loads(output_path.read_text(encoding="utf-8"))
                self.assertEqual(reserved["verdict"], "SUBMISSION_INTENT")
                raise zhuque_gate.BlockedOperation(
                    "missing_api_credentials", "credentials are missing"
                )

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    zhuque_gate,
                    "resolve_api_config",
                    side_effect=missing_config,
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_CONFIG)
            blocked = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(blocked["verdict"], "BLOCKED")
            self.assertEqual(blocked["adapter"], "official_api")
            self.assertEqual(blocked["blocker_scope"], "official_api")
            self.assertEqual(blocked["fallback_adapter"], "official_webpage")
            self.assertEqual(blocked["fallback_url"], zhuque_gate.OFFICIAL_WEB_URL)
            self.assertFalse(blocked["request_attempted"])

            unknown_output_path = Path(directory) / "result-unknown.json"
            unknown_argv = [
                "zhuque_gate.py",
                "--live-api",
                "--input",
                str(input_path),
                "--output",
                str(unknown_output_path),
            ]
            with (
                mock.patch.object(sys, "argv", unknown_argv),
                mock.patch.object(
                    zhuque_gate,
                    "resolve_api_config",
                    return_value=("https://gateway.example", "key"),
                ),
                mock.patch.object(
                    zhuque_gate,
                    "call_api",
                    side_effect=zhuque_gate.ResultUnknown(
                        "network_or_timeout", "request outcome is unknown"
                    ),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_TEMPORARY)
            unknown = json.loads(unknown_output_path.read_text(encoding="utf-8"))
            self.assertEqual(unknown["verdict"], "RESULT_UNKNOWN")
            self.assertEqual(unknown["adapter"], "official_api")
            self.assertTrue(unknown["may_have_been_billed"])
            self.assertFalse(unknown["retry_allowed"])
            self.assertNotIn("fallback_adapter", unknown)

    def test_saved_response_mode_never_builds_network_opener(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            response_path = Path(directory) / "response.json"
            input_path.write_text(text, encoding="utf-8")
            response_path.write_text(
                json.dumps(make_response(text), ensure_ascii=False), encoding="utf-8"
            )
            argv = [
                "zhuque_gate.py",
                "--input",
                str(input_path),
                "--response",
                str(response_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    zhuque_gate.urllib.request,
                    "build_opener",
                    side_effect=AssertionError("network opener was built"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_PASS)

    def test_output_collision_does_not_modify_input(self) -> None:
        text = "正文"
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "candidate.txt"
            response_path = Path(directory) / "response.json"
            input_path.write_text(text, encoding="utf-8")
            response_path.write_text(
                json.dumps(make_response(text), ensure_ascii=False), encoding="utf-8"
            )
            argv = [
                "zhuque_gate.py",
                "--input",
                str(input_path),
                "--response",
                str(response_path),
                "--output",
                str(input_path),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(zhuque_gate.main(), zhuque_gate.EXIT_CONFIG)
            self.assertEqual(input_path.read_text(encoding="utf-8"), text)


if __name__ == "__main__":
    unittest.main()
