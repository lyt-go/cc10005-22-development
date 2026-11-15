"""字段映射冲突判定与转换预览。

设计要点：
- 预览只读解析模板、转换器和字段映射表的定义，以及样例批次的原始值；
  不写 ``store`` 里的任何模块表，也不触碰正式分发文件。
- 同一输出字段来自多条解析路径时，逐条列出候选行、优先级、最终裁决和被覆盖来源。
- 日期模板、数值精度、空值占位符按“映射行显式值 → 转换器默认 → 解析模板默认 → 系统默认”
  的顺序逐字段解析，并在预览里给出每一步的覆盖来源。
- 空值默认不覆盖已有值；只有映射行显式开启 ``allow_null_override`` 时才参与竞争。
- 空值占位符只填充最终值，不参与输出字段重命名。
- 确认后才生成转换结果；重复确认复用已有结果，只保留一份。
- 历史转换结果按生成时的输出格式固化，当前配置变化不影响历史版本。
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from app.mapping_seed import (
    BATCHES,
    CONVERTER_SPECS,
    FIELD_DISPLAY_NAMES,
    HISTORICAL_RESULTS,
    MAPPING_TABLES,
    PARSER_TEMPLATES,
    PRIORITY_RULES,
    SYSTEM_DEFAULTS,
)

DIMENSIONS: dict[str, str] = {
    "date_template": "日期模板",
    "precision": "数值精度",
    "null_placeholder": "空值占位符",
}

DEFAULT_DATE_TEMPLATE = str(SYSTEM_DEFAULTS["date_template"])
DEFAULT_PRECISION = int(SYSTEM_DEFAULTS["precision"])

DATE_INPUT_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%Y%m%d",
]

TOKEN_PATTERN = re.compile(r"YYYY|MM|DD|HH|mm|ss")


class MappingLookupError(Exception):
    """输入引用了不存在的解析模板、转换器、映射表、批次或预览。"""


class MappingActionError(Exception):
    """预览状态不允许当前动作（确认、重试等）。"""


def _explicit(value: Any) -> Any:
    """显式空字符串视为未配置，避免把空模板当成有效格式。"""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, (Decimal, datetime)):
        return str(value)
    return value


def _parse_date(raw: str) -> datetime:
    text = raw.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for fmt in DATE_INPUT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"日期「{raw}」无法识别")


def _format_date(moment: datetime, template: str) -> str:
    # 保留输入值本身的时区日期，不做时区换算，避免日期跨日漂移。
    values = {
        "YYYY": f"{moment.year:04d}",
        "MM": f"{moment.month:02d}",
        "DD": f"{moment.day:02d}",
        "HH": f"{moment.hour:02d}",
        "mm": f"{moment.minute:02d}",
        "ss": f"{moment.second:02d}",
    }
    return TOKEN_PATTERN.sub(lambda match: values[match.group(0)], template)


def _parse_decimal(raw: str) -> Decimal:
    text = raw.strip()
    for symbol in ("￥", "¥", "$", "€", "元", ",", " "):
        text = text.replace(symbol, "")
    if text == "":
        raise ValueError("空字符串不是合法数值")
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"数值「{raw}」不是合法十进制数") from error
    if not value.is_finite():
        raise ValueError(f"数值「{raw}」不是有限数")
    return value


def _quantize(value: Decimal, precision: int) -> tuple[Decimal, bool]:
    """按精度四舍五入，并标出正好落在中间值的边界情况。"""
    quantum = Decimal(1).scaleb(-precision)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    scaled = value.scaleb(precision)
    floor = scaled.to_integral_value(rounding="ROUND_FLOOR")
    tie = abs(scaled - floor) == Decimal("0.5")
    return rounded, tie


def _convert_value(raw: Any, declared_type: str) -> tuple[Any, list[str], str | None]:
    """把原始值按映射表声明的类型转换；返回转换值、提示与错误。"""
    if raw is None:
        return None, [], None
    text = str(raw).strip()
    if text == "":
        return None, ["空白字符串按空值处理"], None
    kind = (declared_type or "string").strip().lower()
    if kind.startswith("string"):
        return text, [], None
    if kind.startswith("int"):
        try:
            return int(text), [], None
        except ValueError:
            return None, [], f"「{raw}」不是合法整数"
    if kind.startswith(("decimal", "number", "float")):
        try:
            return _parse_decimal(text), [], None
        except ValueError as error:
            return None, [], str(error)
    if kind.startswith("date"):
        try:
            return _parse_date(text), [], None
        except ValueError as error:
            return None, [], str(error)
    return text, [], None


def _format_value(value: Any, declared_type: str, config: dict[str, Any]) -> tuple[Any, list[str]]:
    """按字段级配置输出最终值；日期模板与数值精度只作用于对应类型。"""
    notes: list[str] = []
    if value is None:
        placeholder = config.get("null_placeholder")
        if placeholder is None:
            return None, notes
        return placeholder, [f"空值按占位符「{placeholder}」填充"]
    kind = (declared_type or "string").strip().lower()
    if kind.startswith("decimal"):
        precision = int(config.get("precision", DEFAULT_PRECISION))
        rounded, tie = _quantize(value, precision)
        if tie:
            notes.append(f"边界值 {value} 按四舍五入取 {precision} 位得到 {rounded}")
        return f"{rounded:.{precision}f}", notes
    if kind.startswith("date"):
        template = str(config.get("date_template") or DEFAULT_DATE_TEMPLATE)
        return _format_date(value, template), notes
    return value, notes


class MappingPreviewService:
    def __init__(self) -> None:
        self._preview_seq = 0
        self._result_seq = 0
        self._runs: dict[str, dict[str, Any]] = {}
        self._results: list[dict[str, Any]] = []
        self._fingerprints: dict[str, str] = {}

    # ------------------------------------------------------------------ 选项
    def options(self) -> dict[str, Any]:
        parsers = [
            {
                "parser_id": spec["parser_id"],
                "parser_no": spec["parser_no"],
                "name": spec["name"],
                "status": spec["status"],
                "defaults": copy.deepcopy(spec["defaults"]),
                "paths": copy.deepcopy(spec["paths"]),
            }
            for _, spec in sorted(PARSER_TEMPLATES.items())
        ]
        converters = [
            {
                "converter_id": spec["converter_id"],
                "converter_no": spec["converter_no"],
                "name": spec["name"],
                "status": spec["status"],
                "input_format": spec["input_format"],
                "output_format": spec["output_format"],
                "mapping_table_id": spec["mapping_table_id"],
                "defaults": copy.deepcopy(spec["defaults"]),
            }
            for _, spec in sorted(CONVERTER_SPECS.items())
        ]
        tables = [
            {
                "table_id": spec["table_id"],
                "name": spec["name"],
                "converter_id": spec["converter_id"],
                "description": spec["description"],
                "row_count": len(spec["rows"]),
                "output_fields": sorted({row["output_field"] for row in spec["rows"]}),
            }
            for _, spec in sorted(MAPPING_TABLES.items())
        ]
        batches = [
            {
                "batch_id": spec["batch_id"],
                "label": spec["label"],
                "kind": spec["kind"],
                "description": spec["description"],
                "record_count": len(spec["records"]),
            }
            for _, spec in sorted(BATCHES.items())
        ]
        return {
            "parsers": parsers,
            "converters": converters,
            "mapping_tables": tables,
            "batches": batches,
            "field_display_names": copy.deepcopy(FIELD_DISPLAY_NAMES),
            "priority_rules": list(PRIORITY_RULES),
            "system_defaults": copy.deepcopy(SYSTEM_DEFAULTS),
            "guarantees": [
                "预览只读定义与样例批次，不写入正式分发文件。",
                "确认前不允许启用转换器；确认只生成一份转换结果草稿。",
                "历史输出格式按生成时的版本固化，不随当前配置变化。",
            ],
        }

    # ------------------------------------------------------------------ 解析
    def _resolve(
        self,
        parser_no: str,
        converter_no: str,
        mapping_table_id: str,
        batch_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        parser = next(
            (spec for spec in PARSER_TEMPLATES.values() if spec["parser_no"] == parser_no),
            None,
        )
        if parser is None:
            raise MappingLookupError(f"解析模板 {parser_no} 不存在")
        converter = next(
            (spec for spec in CONVERTER_SPECS.values() if spec["converter_no"] == converter_no),
            None,
        )
        if converter is None:
            raise MappingLookupError(f"转换器 {converter_no} 不存在")
        table = MAPPING_TABLES.get(mapping_table_id)
        if table is None:
            raise MappingLookupError(f"字段映射表 {mapping_table_id} 不存在")
        if table["converter_id"] != converter["converter_id"]:
            raise MappingLookupError(
                f"字段映射表 {mapping_table_id} 未登记给转换器 {converter_no}"
            )
        path_ids = {path["path_id"] for path in parser["paths"]}
        unknown_paths = [
            row["row_id"] for row in table["rows"] if row["parser_path"] not in path_ids
        ]
        if unknown_paths:
            raise MappingLookupError(
                f"映射行 {'、'.join(unknown_paths)} 引用了 {parser_no} 中不存在的解析路径"
            )
        batch = BATCHES.get(batch_id)
        if batch is None:
            raise MappingLookupError(f"样例批次 {batch_id} 不存在")
        return parser, converter, table, batch

    # ------------------------------------------------------------------ 预览
    def preview(
        self,
        parser_no: str,
        converter_no: str,
        mapping_table_id: str,
        batch_id: str,
    ) -> dict[str, Any]:
        parser, converter, table, batch = self._resolve(
            parser_no, converter_no, mapping_table_id, batch_id
        )
        self._preview_seq += 1
        preview_id = f"PVW-{self._preview_seq:04d}"
        run: dict[str, Any] = {
            "preview_id": preview_id,
            "parser_no": parser_no,
            "converter_no": converter_no,
            "mapping_table_id": mapping_table_id,
            "batch_id": batch_id,
            "attempt_no": 1,
            "previous_state": None,
            "resume_reason": None,
            "confirmed_result_no": None,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_state": None,
            "last_payload": None,
        }
        self._runs[preview_id] = run
        return self._evaluate(run, parser, converter, table, batch)

    def retry(self, preview_id: str) -> dict[str, Any]:
        run = self._runs.get(preview_id)
        if run is None:
            raise MappingLookupError(f"预览 {preview_id} 不存在或已过期")
        if run["confirmed_result_no"]:
            raise MappingActionError(f"预览 {preview_id} 已确认，不能重试")
        if run.get("last_state") != "failed":
            raise MappingActionError(
                f"预览 {preview_id} 当前状态为「{run.get('last_state') or '未知'}」，只有解析中断的预览可以重试"
            )
        parser, converter, table, batch = self._resolve(
            run["parser_no"], run["converter_no"], run["mapping_table_id"], run["batch_id"]
        )
        run["attempt_no"] += 1
        run["previous_state"] = "failed"
        run["resume_reason"] = "上一次预览在解析批次时中断，本次重试继续使用同一批次数据，不切换输入"
        return self._evaluate(run, parser, converter, table, batch)

    def _evaluate(
        self,
        run: dict[str, Any],
        parser: dict[str, Any],
        converter: dict[str, Any],
        table: dict[str, Any],
        batch: dict[str, Any],
    ) -> dict[str, Any]:
        attempt = {
            "attempt_no": run["attempt_no"],
            "previous_state": run["previous_state"],
            "resume_reason": run["resume_reason"],
        }
        transient_failures = int(batch.get("transient_failures") or 0)
        if run["attempt_no"] <= transient_failures:
            run["last_state"] = "failed"
            payload = self._base_payload(run, parser, converter, table, batch)
            payload.update({
                "state": "failed",
                "state_label": "解析中断",
                "message": "批次解析超时，预览未生成；可以重试，重试会说明中断原因并继续使用同一批次。",
                "failure_reason": "模拟批次解析超时：第 1 次尝试时数据源连接中断",
                "retryable": True,
                "fields": [],
                "conflicts": [],
                "records": [],
                "output_preview": None,
                "confirm_allowed": False,
                "requires_conflict_ack": False,
                "warnings": [],
                "errors": [],
                "attempt": attempt,
            })
            run["last_payload"] = copy.deepcopy(payload)
            return payload

        if len(batch["records"]) == 0:
            run["last_state"] = "empty"
            payload = self._base_payload(run, parser, converter, table, batch)
            payload.update({
                "state": "empty",
                "state_label": "暂无结果",
                "message": f"批次 {batch['batch_id']} 解析完成但没有记录，预览为空；请更换批次或先补齐采集结果。",
                "failure_reason": None,
                "retryable": False,
                "fields": [],
                "conflicts": [],
                "records": [],
                "output_preview": None,
                "confirm_allowed": False,
                "requires_conflict_ack": False,
                "warnings": ["空批次不生成转换结果，确认动作会被拒绝。"],
                "errors": [],
                "attempt": attempt,
            })
            run["last_payload"] = copy.deepcopy(payload)
            return payload

        fields, conflicts, records, errors, warnings = self._compute_fields(
            parser, converter, table, batch
        )
        state = "ready" if not errors else "blocked"
        payload = self._base_payload(run, parser, converter, table, batch)
        payload.update({
            "state": state,
            "state_label": "可确认" if state == "ready" else "存在阻断",
            "message": (
                "字段映射已判定，核对冲突后可以确认转换。"
                if state == "ready"
                else "类型转换存在阻断错误，修正映射或数据后才能确认转换。"
            ),
            "failure_reason": None,
            "retryable": False,
            "fields": fields,
            "conflicts": conflicts,
            "records": records,
            "output_preview": self._render_output(converter, table, records),
            "confirm_allowed": state == "ready",
            "requires_conflict_ack": state == "ready" and bool(conflicts),
            "warnings": warnings,
            "errors": errors,
            "attempt": attempt,
        })
        run["last_state"] = state
        run["last_payload"] = copy.deepcopy(payload)
        return payload

    def _base_payload(
        self,
        run: dict[str, Any],
        parser: dict[str, Any],
        converter: dict[str, Any],
        table: dict[str, Any],
        batch: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "preview_id": run["preview_id"],
            "created_at": run["created_at"],
            "parser": {
                "parser_id": parser["parser_id"],
                "parser_no": parser["parser_no"],
                "name": parser["name"],
                "status": parser["status"],
                "defaults": copy.deepcopy(parser["defaults"]),
            },
            "converter": {
                "converter_id": converter["converter_id"],
                "converter_no": converter["converter_no"],
                "name": converter["name"],
                "status": converter["status"],
                "input_format": converter["input_format"],
                "output_format": converter["output_format"],
                "defaults": copy.deepcopy(converter["defaults"]),
                "null_policy": converter["null_policy"],
            },
            "mapping_table": {
                "table_id": table["table_id"],
                "name": table["name"],
                "row_count": len(table["rows"]),
            },
            "batch": {
                "batch_id": batch["batch_id"],
                "label": batch["label"],
                "kind": batch["kind"],
                "record_count": len(batch["records"]),
            },
            "rules": list(PRIORITY_RULES),
            "confirmed_result_no": run.get("confirmed_result_no"),
            "preview_only": True,
            "writes_distribution": False,
            "preview_only_note": "本预览只读定义与样例批次，不会写入正式分发文件。",
        }

    # ------------------------------------------------------------ 字段判定核心
    def _config_value(
        self,
        winner_rows: list[dict[str, Any]],
        dimension: str,
        converter: dict[str, Any],
        parser: dict[str, Any],
        declared_type: str,
    ) -> tuple[Any, str, list[dict[str, Any]], bool]:
        """按回退顺序解析字段级配置，返回值、生效来源、完整候选链与是否适用。

        适用性：日期模板只作用于 date 字段，数值精度只作用于 decimal 字段，
        空值占位符对所有字段生效。不适用的维度仍然列出候选链，但标注“不参与”。
        """
        kind = (declared_type or "string").strip().lower()
        applicable = True
        if dimension == "date_template" and not kind.startswith("date"):
            applicable = False
        if dimension == "precision" and not kind.startswith("decimal"):
            applicable = False

        chain: list[dict[str, Any]] = []
        for row in winner_rows:
            explicit = _explicit(row.get(dimension))
            chain.append({
                "source_kind": "映射行",
                "source_label": f"映射行 {row['row_id']}（路径 {row['parser_path']}）",
                "row_id": row["row_id"],
                "parser_path": row["parser_path"],
                "value": explicit,
                "explicit": explicit is not None,
                "applied": False,
                "force_none": False,
                "reason": "字段类型不适用" if not applicable else "",
            })
        # 转换器“保留空值”策略优先于解析模板默认占位符：显式声明不填充，而不是忘记配置。
        if dimension == "null_placeholder" and converter.get("null_policy") == "保留空值":
            chain.append({
                "source_kind": "转换器策略",
                "source_label": f"转换器 {converter['converter_no']} 空值策略「保留空值」",
                "row_id": None,
                "parser_path": None,
                "value": None,
                "explicit": True,
                "applied": False,
                "force_none": True,
                "reason": "字段类型不适用" if not applicable else "",
            })
        for source_kind, label, defaults in (
            ("转换器默认", f"转换器 {converter['converter_no']} 默认", converter["defaults"]),
            ("解析模板默认", f"解析模板 {parser['parser_no']} 默认", parser["defaults"]),
            ("系统默认", "系统默认", SYSTEM_DEFAULTS),
        ):
            explicit = _explicit(defaults.get(dimension))
            chain.append({
                "source_kind": source_kind,
                "source_label": label,
                "row_id": None,
                "parser_path": None,
                "value": explicit,
                "explicit": explicit is not None,
                "applied": False,
                "force_none": False,
                "reason": "字段类型不适用" if not applicable else "",
            })

        effective: Any = None
        source = "系统默认（未配置）"
        applied_index: int | None = None
        if applicable:
            for index, item in enumerate(chain):
                if item["value"] is not None or item["force_none"]:
                    effective = item["value"]
                    source = item["source_label"]
                    applied_index = index
                    item["applied"] = True
                    break
        if not applicable:
            for item in chain:
                item["reason"] = f"字段类型 {declared_type} 不使用该维度"
        else:
            for index, item in enumerate(chain):
                if index == applied_index:
                    item["reason"] = (
                        "生效来源：保留空值，不填充占位符"
                        if item["force_none"]
                        else "生效来源"
                    )
                elif item["force_none"] or item["value"] is not None:
                    item["reason"] = "被更高优先级来源覆盖"
                elif item["source_kind"] == "映射行":
                    item["reason"] = "未显式配置，不参与本字段竞争"
                elif item["source_kind"] == "系统默认":
                    item["reason"] = "无有效默认值（不填充）"
                else:
                    item["reason"] = "未配置，回退到下一层"
        return effective, source, chain, applicable

    def _compute_fields(
        self,
        parser: dict[str, Any],
        converter: dict[str, Any],
        table: dict[str, Any],
        batch: dict[str, Any],
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[str],
    ]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in table["rows"]:
            grouped.setdefault(row["output_field"], []).append(copy.deepcopy(row))

        fields: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        warnings: list[str] = []
        output_fields = sorted(
            grouped,
            key=lambda field: min(row["row_order"] for row in grouped[field]),
        )

        for output_field in output_fields:
            candidates = sorted(
                grouped[output_field],
                key=lambda row: (-int(row["priority"]), int(row["row_order"])),
            )
            declared_types = {str(row["type"]) for row in candidates}
            if len(declared_types) > 1:
                warnings.append(
                    f"输出字段 {output_field} 的映射行声明类型不一致："
                    f"{'、'.join(sorted(declared_types))}；按采用行的类型转换。"
                )

            values: list[dict[str, Any]] = []
            per_record_decisions: dict[str, list[dict[str, Any]]] = {}
            config_chains_by_record: dict[str, dict[str, Any]] = {}
            source_errors: list[dict[str, Any]] = []
            applicability: dict[str, bool] = {}
            for dimension in DIMENSIONS:
                kind = str(candidates[0]["type"]).strip().lower()
                if dimension == "date_template":
                    applicability[dimension] = kind.startswith("date")
                elif dimension == "precision":
                    applicability[dimension] = kind.startswith("decimal")
                else:
                    applicability[dimension] = True

            for record in batch["records"]:
                path_values = record.get("paths", {})
                row_state: list[dict[str, Any]] = []
                for row in candidates:
                    raw = path_values.get(row["parser_path"], {}).get(row["source_field"])
                    converted, convert_notes, error = _convert_value(raw, str(row["type"]))
                    row_state.append({
                        "row_id": row["row_id"],
                        "row_order": row["row_order"],
                        "parser_path": row["parser_path"],
                        "source_field": row["source_field"],
                        "priority": row["priority"],
                        "raw": raw,
                        "converted": converted,
                        "allow_null_override": bool(row.get("allow_null_override")),
                        "type": row["type"],
                        "conversion_notes": convert_notes,
                        "conversion_error": error,
                    })
                    if error:
                        source_errors.append({
                            "record_id": record["record_id"],
                            "row_id": row["row_id"],
                            "output_field": output_field,
                            "parser_path": row["parser_path"],
                            "source_field": row["source_field"],
                            "raw": raw,
                            "error": error,
                        })

                # 空值默认不覆盖；只有显式 allow_null_override 的行才参与竞争。
                active = [
                    state for state in row_state
                    if state["converted"] is not None or state["allow_null_override"]
                ]
                winner_state = active[0] if active else row_state[0]
                winner_row = next(
                    row for row in candidates if row["row_id"] == winner_state["row_id"]
                )

                config: dict[str, Any] = {}
                source_map: dict[str, str] = {}
                chain_map: dict[str, list[dict[str, Any]]] = {}
                for dimension in DIMENSIONS:
                    effective, source, chain, _ = self._config_value(
                        candidates, dimension, converter, parser, str(winner_row["type"])
                    )
                    config[dimension] = effective
                    source_map[dimension] = source
                    chain_map[dimension] = chain

                formatted, notes = _format_value(
                    winner_state["converted"], str(winner_row["type"]), config
                )
                notes = list(notes)
                if winner_state["conversion_notes"]:
                    notes.extend(winner_state["conversion_notes"])

                blocking = winner_state["conversion_error"] is not None
                if blocking:
                    formatted = None
                    errors.append({
                        "record_id": record["record_id"],
                        "output_field": output_field,
                        "row_id": winner_state["row_id"],
                        "parser_path": winner_state["parser_path"],
                        "source_field": winner_state["source_field"],
                        "raw": winner_state["raw"],
                        "error": winner_state["conversion_error"],
                        "blocking": True,
                    })

                decisions: list[dict[str, Any]] = []
                for state in row_state:
                    candidate_row = next(
                        row for row in candidates if row["row_id"] == state["row_id"]
                    )
                    if state["row_id"] == winner_state["row_id"]:
                        if state["conversion_error"]:
                            decision = "类型异常阻断"
                            reason = state["conversion_error"]
                        elif state["converted"] is None and state["allow_null_override"]:
                            decision = "裁定采用"
                            reason = "空值覆盖：该行显式允许空值覆盖"
                        else:
                            decision = "裁定采用"
                            reason = "优先级最高且值可用"
                    elif state["conversion_error"]:
                        decision = "类型异常驳回"
                        reason = "转换失败，不参与最终值"
                    elif state["converted"] is None and not state["allow_null_override"]:
                        decision = "空值跳过"
                        reason = "空值默认不覆盖已有值"
                    elif state["converted"] is None and state["allow_null_override"]:
                        decision = "空值候选驳回"
                        reason = "允许空值覆盖，但优先级低于采用行"
                    else:
                        candidate_formatted, _ = _format_value(
                            state["converted"], str(candidate_row["type"]), config
                        )
                        if candidate_formatted == formatted:
                            decision = "同值一致"
                            reason = "与采用行最终值相同，仍记录为被覆盖来源"
                        else:
                            decision = "优先级驳回"
                            reason = "优先级低于采用行，最终值被覆盖"
                    decisions.append({
                        "row_id": state["row_id"],
                        "parser_path": state["parser_path"],
                        "source_field": state["source_field"],
                        "priority": state["priority"],
                        "raw": state["raw"],
                        "converted": _json_safe(state["converted"]),
                        "decision": decision,
                        "reason": reason,
                        "conversion_error": state["conversion_error"],
                        "allow_null_override": state["allow_null_override"],
                    })
                    if state["conversion_error"] and state["row_id"] != winner_state["row_id"]:
                        warnings.append(
                            f"{record['record_id']} 的 {output_field} 候选映射行 "
                            f"{state['row_id']} 转换失败，已按优先级驳回。"
                        )

                source_tags = {entry["row_id"]: entry["decision"] for entry in decisions}
                value_entry = {
                    "record_id": record["record_id"],
                    "value": formatted,
                    "raw": winner_state["raw"],
                    "converted": _json_safe(winner_state["converted"]),
                    "winner_row": winner_state["row_id"],
                    "winner_parser_path": winner_state["parser_path"],
                    "winner_source_field": winner_state["source_field"],
                    "winner_priority": winner_state["priority"],
                    "winner_value_kind": (
                        "空值覆盖"
                        if winner_state["converted"] is None and winner_state["allow_null_override"]
                        else "普通值"
                    ),
                    "decisions": decisions,
                    "config": {
                        "date_template": config["date_template"],
                        "date_template_source": source_map["date_template"],
                        "precision": config["precision"],
                        "precision_source": source_map["precision"],
                        "null_placeholder": config["null_placeholder"],
                        "null_placeholder_source": source_map["null_placeholder"],
                    },
                    "config_chains": chain_map,
                    "config_applicability": applicability,
                    "notes": notes,
                    "blocking": blocking,
                    "error": winner_state["conversion_error"],
                    "source_tags": source_tags,
                }
                values.append(value_entry)
                per_record_decisions[record["record_id"]] = decisions
                config_chains_by_record[record["record_id"]] = chain_map

            has_conflict = len(candidates) > 1
            format_conflicts: list[dict[str, Any]] = []
            if has_conflict:
                for dimension, label in DIMENSIONS.items():
                    explicit_values = [
                        {
                            "row_id": row["row_id"],
                            "priority": row["priority"],
                            "value": _explicit(row.get(dimension)),
                        }
                        for row in candidates
                        if _explicit(row.get(dimension)) is not None
                    ]
                    adopted_values = sorted({
                        "（保留空值）" if value["config"][dimension] is None
                        else str(value["config"][dimension])
                        for value in values
                    })
                    adopted_sources = sorted({
                        str(value["config"][f"{dimension}_source"]) for value in values
                    })
                    applicable = applicability[dimension]
                    if not applicable:
                        adopted_values = ["（本字段不适用）"]
                        conclusion = (
                            f"字段类型 {candidates[0]['type']} 不使用{label}，"
                            "候选值仅登记，不参与本字段输出"
                        )
                    elif len({str(item["value"]) for item in explicit_values}) > 1:
                        conclusion = (
                            f"{label}存在不一致："
                            + "、".join(
                                f"{item['row_id']}={item['value']}" for item in explicit_values
                            )
                            + f"；生效来源 {('、'.join(adopted_sources))}，"
                            f"最终取 {'、'.join(adopted_values)}"
                        )
                    elif explicit_values:
                        conclusion = (
                            f"{label}仅 {explicit_values[0]['row_id']} 显式配置"
                            f"（{explicit_values[0]['value']}），其余行按回退顺序解析；"
                            f"生效来源 {('、'.join(adopted_sources))}，"
                            f"最终取 {'、'.join(adopted_values)}"
                        )
                    else:
                        conclusion = (
                            f"{label}所有映射行均未配置，按回退顺序解析；"
                            f"生效来源 {('、'.join(adopted_sources))}，"
                            f"最终取 {'、'.join(adopted_values)}"
                        )
                    format_conflicts.append({
                        "dimension": dimension,
                        "label": label,
                        "applicable": applicable,
                        "values": explicit_values,
                        "adopted": adopted_values,
                        "adopted_sources": adopted_sources,
                        "conclusion": conclusion,
                    })

            conclusion_parts: list[str] = []
            for item in values:
                if item["blocking"]:
                    conclusion_parts.append(
                        f"{item['record_id']} 阻断：{item['error']}"
                    )
                    continue
                parts = [f"{item['record_id']} 采用 {item['winner_row']}"]
                groups: dict[str, list[str]] = {}
                for entry in item["decisions"]:
                    if entry["row_id"] == item["winner_row"]:
                        continue
                    groups.setdefault(entry["decision"], []).append(entry["row_id"])
                for decision in ("优先级驳回", "同值一致", "空值跳过", "空值候选驳回", "类型异常驳回"):
                    if decision in groups:
                        parts.append(f"{decision} {'、'.join(groups[decision])}")
                if item["winner_value_kind"] == "空值覆盖":
                    parts.insert(1, "空值覆盖生效")
                conclusion_parts.append("；".join(parts))
            conclusion = (
                "冲突结论：" + "；".join(conclusion_parts)
                if has_conflict
                else "单一解析路径，无字段冲突"
            )

            overridden_sources = sorted({
                entry["row_id"]
                for item in values
                for entry in item["decisions"]
                if entry["row_id"] != item["winner_row"]
                and entry["decision"] in ("优先级驳回", "同值一致", "空值候选驳回", "类型异常驳回")
            })
            skipped_sources = sorted({
                entry["row_id"]
                for item in values
                for entry in item["decisions"]
                if entry["row_id"] != item["winner_row"]
                and entry["decision"] == "空值跳过"
            })

            value_summaries = [
                {
                    "record_id": item["record_id"],
                    "value": item["value"],
                    "winner_row": item["winner_row"],
                    "winner_parser_path": item["winner_parser_path"],
                    "decision": item["winner_value_kind"],
                    "blocking": item["blocking"],
                }
                for item in values
            ]
            field_entry = {
                "output_field": output_field,
                "display_name": FIELD_DISPLAY_NAMES.get(output_field, output_field),
                "type": candidates[0]["type"],
                "distribute": bool(candidates[0].get("distribute", True)),
                "values": values,
                "value_summary": value_summaries,
                "winner_sources": sorted({item["winner_row"] for item in values}),
                "overridden_sources": overridden_sources,
                "skipped_sources": skipped_sources,
                "has_conflict": has_conflict,
                "conclusion": conclusion,
                "format_conflicts": format_conflicts,
                "config_applicability": applicability,
                "config_chains": config_chains_by_record,
                "source_errors": source_errors,
            }
            if not field_entry["distribute"]:
                warnings.append(
                    f"输出字段 {output_field} 标记为不分发，只参与预览核对，不写入输出格式。"
                )
            fields.append(field_entry)

            if has_conflict:
                conflicts.append({
                    "output_field": output_field,
                    "display_name": field_entry["display_name"],
                    "parser_paths": sorted({row["parser_path"] for row in candidates}),
                    "candidates": [
                        {
                            "row_id": row["row_id"],
                            "row_order": row["row_order"],
                            "parser_path": row["parser_path"],
                            "source_field": row["source_field"],
                            "priority": row["priority"],
                            "type": row["type"],
                            "allow_null_override": bool(row.get("allow_null_override")),
                            "decision_by_record": {
                                record_id: next(
                                    entry["decision"]
                                    for entry in decisions
                                    if entry["row_id"] == row["row_id"]
                                )
                                for record_id, decisions in per_record_decisions.items()
                            },
                            "reason_by_record": {
                                record_id: next(
                                    entry["reason"]
                                    for entry in decisions
                                    if entry["row_id"] == row["row_id"]
                                )
                                for record_id, decisions in per_record_decisions.items()
                            },
                        }
                        for row in candidates
                    ],
                    "format_conflicts": format_conflicts,
                    "conclusion": conclusion,
                    "severity": (
                        "error" if any(item["blocking"] for item in values) else "warning"
                    ),
                    "blocking": any(item["blocking"] for item in values),
                })

        unique_errors: list[dict[str, Any]] = []
        seen_errors: set[str] = set()
        for item in errors:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key not in seen_errors:
                seen_errors.add(key)
                unique_errors.append(item)

        records: list[dict[str, Any]] = []
        for record in batch["records"]:
            record_values: dict[str, Any] = {}
            for field in fields:
                match = next(
                    value
                    for value in field["values"]
                    if value["record_id"] == record["record_id"]
                )
                record_values[field["output_field"]] = match["value"]
            records.append({"record_id": record["record_id"], "values": record_values})

        return fields, conflicts, records, unique_errors, sorted(set(warnings))

    def _render_output(
        self,
        converter: dict[str, Any],
        table: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        output_fields = sorted(
            {row["output_field"] for row in table["rows"]},
            key=lambda item: min(
                row["row_order"] for row in table["rows"] if row["output_field"] == item
            ),
        )
        distributed_fields = [
            field
            for field in output_fields
            if next(
                bool(row.get("distribute", True))
                for row in table["rows"]
                if row["output_field"] == field
            )
        ]
        dropped_fields = [field for field in output_fields if field not in distributed_fields]
        payloads = [
            {field: record["values"].get(field) for field in distributed_fields}
            for record in records
        ]
        output_format = converter["output_format"].upper()
        if output_format == "CSV":
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(distributed_fields)
            for payload in payloads:
                writer.writerow([
                    "" if payload[field] is None else payload[field]
                    for field in distributed_fields
                ])
            content = buffer.getvalue()
        elif output_format == "NDJSON":
            content = "\n".join(
                json.dumps(payload, ensure_ascii=False) for payload in payloads
            ) + ("\n" if payloads else "")
        else:
            content = json.dumps(payloads, ensure_ascii=False, indent=2)
        return {
            "output_format": converter["output_format"],
            "output_fields": distributed_fields,
            "dropped_fields": dropped_fields,
            "record_count": len(records),
            "content": content,
            "writes_distribution": False,
        }

    # ------------------------------------------------------------------ 确认
    def confirm(
        self,
        preview_id: str,
        ack_conflicts: bool,
        remark: str | None = None,
    ) -> dict[str, Any]:
        run = self._runs.get(preview_id)
        if run is None:
            raise MappingLookupError(f"预览 {preview_id} 不存在或已过期")
        if run.get("confirmed_result_no"):
            existing = self._find_result(run["confirmed_result_no"])
            return {
                "ok": True,
                "duplicate": True,
                "message": (
                    f"预览 {preview_id} 已确认，复用转换结果 {run['confirmed_result_no']}，"
                    "不会重复生成"
                ),
                "result": copy.deepcopy(existing),
            }
        state = run.get("last_state")
        if state == "failed":
            raise MappingActionError(
                "预览解析中断，不能确认；请先重试预览，重试后会说明中断原因。"
            )
        if state == "empty":
            raise MappingActionError(
                "预览为空数据，不能确认；请更换批次或补齐采集结果后重新预览。"
            )
        if state == "blocked":
            payload = run.get("last_payload") or {}
            reasons = "；".join(
                f"{item['record_id']}.{item['output_field']}：{item['error']}"
                for item in payload.get("errors", [])
            )
            raise MappingActionError(f"预览存在类型转换阻断，不能确认：{reasons}")
        if state != "ready":
            raise MappingActionError("预览尚未完成，不能确认")
        payload = run["last_payload"]
        if payload.get("requires_conflict_ack") and not ack_conflicts:
            raise MappingActionError("预览存在字段冲突，请先核对冲突列表并确认冲突结论")
        if not payload.get("confirm_allowed"):
            raise MappingActionError("当前预览不允许确认")

        fingerprint = self._fingerprint(payload)
        existing_no = self._fingerprints.get(fingerprint)
        if existing_no:
            existing = self._find_result(existing_no)
            run["confirmed_result_no"] = existing_no
            run["last_state"] = "confirmed"
            return {
                "ok": True,
                "duplicate": True,
                "message": (
                    f"相同预览输入已生成转换结果 {existing_no}，复用已有结果，不会重复生成"
                ),
                "result": copy.deepcopy(existing),
            }

        self._result_seq += 1
        result_no = f"CONV-{2000 + self._result_seq:04d}"
        converter = payload["converter"]
        table_id = payload["mapping_table"]["table_id"]
        historical_versions = [
            int(item["output_version"])
            for item in HISTORICAL_RESULTS
            if item["converter_id"] == converter["converter_id"]
            and item["mapping_table_id"] == table_id
        ]
        current_versions = [
            int(item["output_version"])
            for item in self._results
            if item["converter_id"] == converter["converter_id"]
            and item["mapping_table_id"] == table_id
        ]
        output_version = max(historical_versions + current_versions + [0]) + 1
        result = {
            "result_no": result_no,
            "converter_id": converter["converter_id"],
            "converter_no": converter["converter_no"],
            "parser_no": payload["parser"]["parser_no"],
            "mapping_table_id": table_id,
            "batch_id": payload["batch"]["batch_id"],
            "preview_id": preview_id,
            "output_version": output_version,
            "output_format": payload["output_preview"]["output_format"],
            "record_count": payload["output_preview"]["record_count"],
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "created_by": "值班管理员（预览确认）",
            "status": "已确认（转换草稿）",
            "output_content": payload["output_preview"]["content"],
            "field_snapshot": [
                {
                    "output_field": field["output_field"],
                    "type": field["type"],
                    "distribute": field["distribute"],
                    "values": copy.deepcopy(field["value_summary"]),
                    "conclusion": field["conclusion"],
                }
                for field in payload["fields"]
            ],
            "conflicts": copy.deepcopy(payload["conflicts"]),
            "spec_snapshot": {
                "output_format": payload["output_preview"]["output_format"],
                "field_order": payload["output_preview"]["output_fields"],
                "dropped_fields": payload["output_preview"]["dropped_fields"],
                "converter_defaults": copy.deepcopy(payload["converter"]["defaults"]),
                "parser_defaults": copy.deepcopy(payload["parser"]["defaults"]),
                "mapping_table_id": table_id,
                "mapping_table_rows": len(MAPPING_TABLES[table_id]["rows"]),
                "null_policy": payload["converter"]["null_policy"],
            },
            "remark": remark,
            "preview_only": False,
            "writes_distribution": False,
            "distribution_note": (
                "确认只生成转换结果草稿，未写入正式分发文件；如需分发请走数据分发审批。"
            ),
            "is_history": False,
            "immutable": True,
        }
        self._results.append(result)
        self._fingerprints[fingerprint] = result_no
        run["confirmed_result_no"] = result_no
        run["last_state"] = "confirmed"
        return {
            "ok": True,
            "duplicate": False,
            "message": (
                f"已确认预览 {preview_id}，生成转换结果 {result_no}（版本 v{output_version}）；"
                "结果仅作为转换草稿，未写入正式分发文件。"
            ),
            "result": copy.deepcopy(result),
        }

    def _fingerprint(self, payload: dict[str, Any]) -> str:
        material = {
            "parser_no": payload["parser"]["parser_no"],
            "converter_no": payload["converter"]["converter_no"],
            "mapping_table_id": payload["mapping_table"]["table_id"],
            "batch_id": payload["batch"]["batch_id"],
            "output_format": payload["output_preview"]["output_format"],
            "output": payload["output_preview"]["content"],
            "spec": payload["converter"]["defaults"],
            "field_conclusions": [
                {
                    "field": field["output_field"],
                    "values": field["value_summary"],
                    "conclusion": field["conclusion"],
                }
                for field in payload["fields"]
            ],
        }
        raw = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _find_result(self, result_no: str) -> dict[str, Any]:
        for item in self._results:
            if item["result_no"] == result_no:
                return item
        raise MappingLookupError(f"转换结果 {result_no} 不存在")

    # ------------------------------------------------------------------ 查询
    def get_preview(self, preview_id: str) -> dict[str, Any]:
        run = self._runs.get(preview_id)
        if run is None:
            raise MappingLookupError(f"预览 {preview_id} 不存在或已过期")
        payload = copy.deepcopy(run.get("last_payload") or {})
        payload["confirmed_result_no"] = run.get("confirmed_result_no")
        return payload

    def results(self) -> dict[str, Any]:
        generated = [copy.deepcopy(item) for item in self._results]
        history = [
            {
                **copy.deepcopy(item),
                "status": "历史输出（只读）",
                "is_history": True,
                "preview_only": False,
                "writes_distribution": False,
                "immutable": True,
            }
            for item in HISTORICAL_RESULTS
        ]
        items = generated + history
        items.sort(key=lambda item: (item["converter_no"], int(item["output_version"])))
        return {
            "items": items,
            "generated": generated,
            "history": history,
            "distribution_untouched": True,
            "note": "历史输出格式按生成时的版本固化；当前转换器配置变化不会改写历史结果。",
        }

    def get_result(self, result_no: str) -> dict[str, Any] | None:
        for item in self._results:
            if item["result_no"] == result_no:
                return copy.deepcopy(item)
        for item in HISTORICAL_RESULTS:
            if item["result_no"] == result_no:
                return {
                    **copy.deepcopy(item),
                    "status": "历史输出（只读）",
                    "is_history": True,
                    "writes_distribution": False,
                    "immutable": True,
                }
        return None

    def gate_message(self, converter_no: str) -> str | None:
        """启用转换器前的门禁：配置了映射预览定义的转换器必须已有确认过的预览。"""
        if not any(
            spec["converter_no"] == converter_no for spec in CONVERTER_SPECS.values()
        ):
            # 没有映射预览定义的转换器没有字段映射冲突可核对，不额外拦截。
            return None
        if any(item["converter_no"] == converter_no for item in self._results):
            return None
        return (
            f"转换器 {converter_no} 尚未在映射预览中确认字段映射与冲突结论，不能启用；"
            "请先选择解析模板、该转换器和字段映射表完成预览并确认。"
        )


mapping_preview_service = MappingPreviewService()
