"""字段映射冲突判定与转换预览的回归测试。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services import mapping_preview
from app.services.mapping_preview import (
    MappingActionError,
    MappingLookupError,
    MappingPreviewService,
)


class MappingPreviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MappingPreviewService()

    def preview(self, converter: str, table: str, batch: str = "BATCH-NORMAL"):
        return self.service.preview("PARS-0002", converter, table, batch)

    def field(self, payload, name: str):
        return next(field for field in payload["fields"] if field["output_field"] == name)

    # ---------------------------------------------------------------- 基本判定
    def test_conflict_list_covers_multi_path_fields(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002")
        self.assertEqual(payload["state"], "ready")
        conflicted = {item["output_field"] for item in payload["conflicts"]}
        self.assertEqual(conflicted, {"product_id", "title", "price", "listed_at"})
        title = next(item for item in payload["conflicts"] if item["output_field"] == "title")
        self.assertEqual(title["parser_paths"], ["P-01", "P-02", "P-03"])
        self.assertIn("冲突结论", title["conclusion"])

    def test_priority_wins_and_source_is_recorded(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002")
        title = self.field(payload, "title")
        first = title["values"][0]
        self.assertEqual(first["winner_row"], "M-0203")
        self.assertIn("M-0204", title["overridden_sources"])
        self.assertIn("M-0205", title["overridden_sources"])

    def test_null_does_not_override_by_default(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002")
        title = self.field(payload, "title")
        second = next(item for item in title["values"] if item["record_id"] == "R-002")
        self.assertEqual(second["winner_row"], "M-0205")
        self.assertEqual(second["value"], "折叠键盘 K1")
        skipped = {
            entry["row_id"]
            for entry in second["decisions"]
            if entry["decision"] == "空值跳过"
        }
        self.assertEqual(skipped, {"M-0203", "M-0204"})

    def test_explicit_null_override_wins(self) -> None:
        payload = self.preview("FORM-0003", "MAP-0003")
        title = self.field(payload, "title")
        second = next(item for item in title["values"] if item["record_id"] == "R-002")
        self.assertEqual(second["winner_row"], "M-0303")
        self.assertEqual(second["winner_value_kind"], "空值覆盖")
        self.assertIsNone(second["value"])
        self.assertIn("空值覆盖生效", title["conclusion"])

    def test_date_and_precision_override_order_per_field(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002")
        date_field = self.field(payload, "listed_at")
        date_conflict = next(
            item for item in date_field["format_conflicts"]
            if item["dimension"] == "date_template"
        )
        self.assertEqual(date_conflict["adopted"], ["YYYY-MM-DD"])
        self.assertIn("M-0208（路径 P-01）", date_conflict["adopted_sources"][0])
        chain = date_field["values"][0]["config_chains"]["date_template"]
        reasons = {item["source_label"]: item["reason"] for item in chain}
        self.assertEqual(reasons["映射行 M-0208（路径 P-01）"], "生效来源")
        self.assertEqual(reasons["映射行 M-0209（路径 P-07）"], "被更高优先级来源覆盖")

        price_field = self.field(payload, "price")
        price_conflict = next(
            item for item in price_field["format_conflicts"]
            if item["dimension"] == "precision"
        )
        self.assertEqual(price_conflict["adopted"], ["2"])
        self.assertEqual(price_field["value_summary"][0]["value"], "129.90")

    def test_null_placeholder_does_not_rename_fields(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002")
        seller = self.field(payload, "seller_name")
        self.assertEqual(seller["output_field"], "seller_name")
        self.assertEqual(seller["values"][1]["value"], "未知卖家")
        self.assertIn("seller_name", payload["output_preview"]["output_fields"])

    def test_non_conflicting_fields_are_kept(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002")
        self.assertFalse(self.field(payload, "stock")["has_conflict"])
        self.assertEqual(self.field(payload, "stock")["conclusion"], "单一解析路径，无字段冲突")
        self.assertIn("stock", payload["output_preview"]["output_fields"])

    def test_undistributed_field_is_not_written(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002")
        self.assertIn("internal_hash", payload["output_preview"]["dropped_fields"])
        self.assertNotIn("internal_hash", payload["output_preview"]["content"])
        self.assertFalse(payload["writes_distribution"])

    # ------------------------------------------------------------ 空态与边界
    def test_empty_batch_returns_empty_state(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002", "BATCH-EMPTY")
        self.assertEqual(payload["state"], "empty")
        self.assertEqual(payload["fields"], [])
        self.assertFalse(payload["confirm_allowed"])
        with self.assertRaises(MappingActionError):
            self.service.confirm(payload["preview_id"], True)

    def test_boundary_rounding_and_max_date(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002", "BATCH-BOUNDARY")
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(self.field(payload, "price")["values"][0]["value"], "0.01")
        self.assertEqual(self.field(payload, "listed_at")["values"][0]["value"], "2026-12-31")
        self.assertEqual(self.field(payload, "upc")["values"][0]["value"], "00000000000000")
        self.assertEqual(self.field(payload, "rating")["values"][0]["value"], "5.0")

    def test_invalid_types_block_confirmation(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002", "BATCH-INVALID")
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(len(payload["errors"]), 4)
        with self.assertRaises(MappingActionError):
            self.service.confirm(payload["preview_id"], True)

    # ------------------------------------------------------------- 中断与重试
    def test_interrupted_preview_can_retry_with_reason(self) -> None:
        failed = self.preview("FORM-0002", "MAP-0002", "BATCH-TRANSIENT")
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["retryable"])
        self.assertFalse(failed["confirm_allowed"])
        with self.assertRaises(MappingActionError):
            self.service.confirm(failed["preview_id"], True)

        resumed = self.service.retry(failed["preview_id"])
        self.assertEqual(resumed["state"], "ready")
        self.assertEqual(resumed["attempt"]["attempt_no"], 2)
        self.assertEqual(resumed["attempt"]["previous_state"], "failed")
        self.assertIn("中断", resumed["attempt"]["resume_reason"])

    def test_only_interrupted_preview_can_retry(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002", "BATCH-EMPTY")
        with self.assertRaises(MappingActionError):
            self.service.retry(payload["preview_id"])

    # --------------------------------------------------------------- 确认规则
    def test_conflict_ack_required(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002")
        self.assertTrue(payload["requires_conflict_ack"])
        with self.assertRaises(MappingActionError):
            self.service.confirm(payload["preview_id"], False)

    def test_confirm_creates_single_result(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002")
        first = self.service.confirm(payload["preview_id"], True)
        second = self.service.confirm(payload["preview_id"], True)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["result"]["result_no"], second["result"]["result_no"])
        self.assertEqual(len(self.service.results()["generated"]), 1)

        # 另一个预览的相同输入也复用同一份结果。
        again = self.preview("FORM-0002", "MAP-0002")
        third = self.service.confirm(again["preview_id"], True)
        self.assertTrue(third["duplicate"])
        self.assertEqual(third["result"]["result_no"], first["result"]["result_no"])
        self.assertEqual(len(self.service.results()["generated"]), 1)

    def test_confirm_marks_draft_not_distribution(self) -> None:
        payload = self.preview("FORM-0002", "MAP-0002")
        result = self.service.confirm(payload["preview_id"], True)["result"]
        self.assertFalse(result["writes_distribution"])
        self.assertIn("未写入正式分发文件", result["distribution_note"])
        self.assertIn("product_id", result["output_content"])

    def test_history_keeps_its_own_output_format(self) -> None:
        payload = self.preview("FORM-0003", "MAP-0003")
        self.service.confirm(payload["preview_id"], True)
        history = {item["result_no"]: item for item in self.service.results()["history"]}
        self.assertEqual(history["CONV-0001"]["output_format"], "NDJSON")
        self.assertEqual(history["CONV-0001"]["spec_snapshot"]["date_template"], "YYYY/MM/DD")
        self.assertEqual(history["CONV-0002"]["output_format"], "CSV")
        self.assertTrue(all(item["immutable"] for item in history.values()))
        generated = self.service.results()["generated"][0]
        self.assertEqual(generated["output_version"], 3)
        self.assertEqual(generated["output_format"], "CSV")

    def test_gate_blocks_enable_before_confirm(self) -> None:
        self.assertIsNotNone(self.service.gate_message("FORM-0002"))
        payload = self.preview("FORM-0002", "MAP-0002")
        self.service.confirm(payload["preview_id"], True)
        self.assertIsNone(self.service.gate_message("FORM-0002"))
        self.assertIsNone(self.service.gate_message("FORM-0001"))

    # ----------------------------------------------------------------- 异常输入
    def test_unknown_references_raise(self) -> None:
        with self.assertRaises(MappingLookupError):
            self.service.preview("PARS-9999", "FORM-0002", "MAP-0002", "BATCH-NORMAL")
        with self.assertRaises(MappingLookupError):
            self.service.preview("PARS-0002", "FORM-9999", "MAP-0002", "BATCH-NORMAL")
        with self.assertRaises(MappingLookupError):
            self.service.preview("PARS-0002", "FORM-0002", "MAP-0003", "BATCH-NORMAL")
        with self.assertRaises(MappingLookupError):
            self.service.preview("PARS-0002", "FORM-0002", "MAP-0002", "BATCH-NONE")

    def test_preview_does_not_mutate_definitions(self) -> None:
        before = mapping_preview.MAPPING_TABLES["MAP-0002"]["rows"][0].copy()
        payload = self.preview("FORM-0002", "MAP-0002")
        payload["fields"][0]["values"][0]["decisions"].clear()
        self.assertEqual(mapping_preview.MAPPING_TABLES["MAP-0002"]["rows"][0], before)


if __name__ == "__main__":
    unittest.main()
