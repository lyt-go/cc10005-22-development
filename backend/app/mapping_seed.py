"""映射预览的示例定义：解析路径、字段映射表、转换器默认值与样例批次。

这里刻意围绕 PARS-0002、FORM-0002、FORM-0003 设计：
同一输出字段可以从多条解析路径汇聚，日期模板、数值精度和空值占位符存在多层覆盖，
空值既可能被跳过也可能按显式规则覆盖。预览引擎只读取这些定义，不写回任何模块表。

数据全部是演示样例，真实项目里应由解析模板与转换器配置表落库后生成。
"""
from __future__ import annotations

from typing import Any

# 输出字段的展示名只用于页面阅读，不参与字段重命名。
FIELD_DISPLAY_NAMES: dict[str, str] = {
    "product_id": "商品编号",
    "title": "标题",
    "price": "价格",
    "listed_at": "上架日期",
    "stock": "库存",
    "rating": "评分",
    "seller_name": "卖家名称",
    "upc": "条码",
    "warehouse": "仓库",
    "internal_hash": "内部哈希",
}

SYSTEM_DEFAULTS: dict[str, Any] = {
    "date_template": "YYYY-MM-DD",
    "precision": 2,
    "null_placeholder": None,
}

PRIORITY_RULES: list[str] = [
    "同一输出字段的多条映射行按优先级从高到低裁决，优先级相同则按映射行登记顺序（行号小者优先）。",
    "映射行没有显式日期模板、数值精度或空值占位符时，依次回退到转换器默认值、解析模板默认值、系统默认值。",
    "空值默认不覆盖已有值；只有映射行显式开启“允许空值覆盖”时才参与竞争，并在预览中标记来源。",
    "空值占位符只用于填充最终值，不参与输出字段重命名，字段名始终取映射行的输出字段名。",
    "未被冲突波及的输出字段照常保留；显式标记为不分发的字段只参与预览核对，不写入输出格式。",
]

PARSER_TEMPLATES: dict[int, dict[str, Any]] = {
    2: {
        "parser_id": 2,
        "parser_no": "PARS-0002",
        "name": "商品详情页解析模板",
        "status": "模板验证中",
        "defaults": {
            "date_template": "YYYY/MM/DD",
            "precision": 2,
            "null_placeholder": "N/A",
        },
        "paths": [
            {
                "path_id": "P-01",
                "label": "列表页商品容器",
                "xpath": '//div[@class="goods-item"]',
                "extract": "属性+文本",
                "type_rule": "string/decimal/integer/date 自动判定",
            },
            {
                "path_id": "P-02",
                "label": "详情页 JSON 接口",
                "xpath": '//script[@id="__DETAIL__"]/text()',
                "extract": "JSON 字段",
                "type_rule": "string",
            },
            {
                "path_id": "P-03",
                "label": "旧版列表页标题",
                "xpath": '//div[@class="goods-item"]/@data-legacy-title',
                "extract": '正则 data-legacy-title="([^"]+)"',
                "type_rule": "string",
            },
            {
                "path_id": "P-04",
                "label": "价格文本",
                "xpath": '//span[@class="price"]/text()',
                "extract": r"正则 ￥([\d,]+\.\d{2})",
                "type_rule": "decimal（含货币符号与千分位）",
            },
            {
                "path_id": "P-05",
                "label": "评分标签",
                "xpath": '//span[@class="rating"]/text()',
                "extract": "文本",
                "type_rule": "decimal",
            },
            {
                "path_id": "P-06",
                "label": "条码元信息",
                "xpath": '//meta[@name="upc"]/@content',
                "extract": "属性",
                "type_rule": "string（保留前导零）",
            },
            {
                "path_id": "P-07",
                "label": "结构化时间",
                "xpath": '//time/@datetime',
                "extract": "属性",
                "type_rule": "date",
            },
            {
                "path_id": "P-08",
                "label": "详情页内部指纹",
                "xpath": '//script[@id="__DETAIL__"]/text()',
                "extract": "JSON internal_hash",
                "type_rule": "string",
            },
        ],
    }
}

CONVERTER_SPECS: dict[int, dict[str, Any]] = {
    2: {
        "converter_id": 2,
        "converter_no": "FORM-0002",
        "name": "商品主数据转换器",
        "status": "转换测试中",
        "input_format": "JSON",
        "output_format": "JSON",
        "mapping_table_id": "MAP-0002",
        "defaults": {
            "date_template": None,
            "precision": 4,
            "null_placeholder": None,
        },
        "null_policy": "填充占位符",
        "notes": "转换测试中的主数据转换器，输出 JSON，价格保留两位小数并输出为字符串。",
    },
    3: {
        "converter_id": 3,
        "converter_no": "FORM-0003",
        "name": "商品主数据 CSV 转换器",
        "status": "已启用",
        "input_format": "JSON",
        "output_format": "CSV",
        "mapping_table_id": "MAP-0003",
        "defaults": {
            "date_template": None,
            "precision": 3,
            "null_placeholder": None,
        },
        "null_policy": "保留空值",
        "notes": "已启用的 CSV 输出转换器，空值按列保留为空字符串。",
    },
}

MAPPING_TABLES: dict[str, dict[str, Any]] = {
    "MAP-0002": {
        "table_id": "MAP-0002",
        "name": "商品主数据字段映射表",
        "converter_id": 2,
        "description": "FORM-0002 使用的字段映射表，标题与价格存在多解析路径汇聚。",
        "rows": [
            {"row_id": "M-0201", "row_order": 1, "parser_path": "P-01", "source_field": "sku",
             "output_field": "product_id", "type": "string", "priority": 30},
            {"row_id": "M-0202", "row_order": 2, "parser_path": "P-02", "source_field": "sku_alt",
             "output_field": "product_id", "type": "string", "priority": 20},
            {"row_id": "M-0203", "row_order": 3, "parser_path": "P-02", "source_field": "title_alt",
             "output_field": "title", "type": "string", "priority": 40, "null_placeholder": "未命名"},
            {"row_id": "M-0204", "row_order": 4, "parser_path": "P-01", "source_field": "title",
             "output_field": "title", "type": "string", "priority": 30},
            {"row_id": "M-0205", "row_order": 5, "parser_path": "P-03", "source_field": "title_legacy",
             "output_field": "title", "type": "string", "priority": 10},
            {"row_id": "M-0206", "row_order": 6, "parser_path": "P-01", "source_field": "price",
             "output_field": "price", "type": "decimal", "priority": 40, "precision": 2},
            {"row_id": "M-0207", "row_order": 7, "parser_path": "P-04", "source_field": "price_raw",
             "output_field": "price", "type": "decimal", "priority": 20, "precision": 2},
            {"row_id": "M-0208", "row_order": 8, "parser_path": "P-01", "source_field": "listed_at",
             "output_field": "listed_at", "type": "date", "priority": 40, "date_template": "YYYY-MM-DD"},
            {"row_id": "M-0209", "row_order": 9, "parser_path": "P-07", "source_field": "listed_at_alt",
             "output_field": "listed_at", "type": "date", "priority": 20, "date_template": "YYYY/MM/DD"},
            {"row_id": "M-0210", "row_order": 10, "parser_path": "P-01", "source_field": "stock",
             "output_field": "stock", "type": "integer", "priority": 30},
            {"row_id": "M-0211", "row_order": 11, "parser_path": "P-05", "source_field": "rating",
             "output_field": "rating", "type": "decimal", "priority": 30, "precision": 1},
            {"row_id": "M-0212", "row_order": 12, "parser_path": "P-01", "source_field": "seller_name",
             "output_field": "seller_name", "type": "string", "priority": 30,
             "null_placeholder": "未知卖家"},
            {"row_id": "M-0213", "row_order": 13, "parser_path": "P-06", "source_field": "upc",
             "output_field": "upc", "type": "string", "priority": 30},
            {"row_id": "M-0214", "row_order": 14, "parser_path": "P-01", "source_field": "warehouse",
             "output_field": "warehouse", "type": "string", "priority": 30},
            {"row_id": "M-0215", "row_order": 15, "parser_path": "P-08", "source_field": "internal_hash",
             "output_field": "internal_hash", "type": "string", "priority": 30, "distribute": False},
        ],
    },
    "MAP-0003": {
        "table_id": "MAP-0003",
        "name": "商品主数据 CSV 字段映射表",
        "converter_id": 3,
        "description": "FORM-0003 使用的字段映射表，标题允许空值覆盖，价格优先取价格文本路径。",
        "rows": [
            {"row_id": "M-0301", "row_order": 1, "parser_path": "P-01", "source_field": "sku",
             "output_field": "product_id", "type": "string", "priority": 30},
            {"row_id": "M-0302", "row_order": 2, "parser_path": "P-02", "source_field": "sku_alt",
             "output_field": "product_id", "type": "string", "priority": 20},
            {"row_id": "M-0303", "row_order": 3, "parser_path": "P-02", "source_field": "title_alt",
             "output_field": "title", "type": "string", "priority": 40,
             "allow_null_override": True},
            {"row_id": "M-0304", "row_order": 4, "parser_path": "P-01", "source_field": "title",
             "output_field": "title", "type": "string", "priority": 30},
            {"row_id": "M-0305", "row_order": 5, "parser_path": "P-03", "source_field": "title_legacy",
             "output_field": "title", "type": "string", "priority": 10},
            {"row_id": "M-0306", "row_order": 6, "parser_path": "P-04", "source_field": "price_raw",
             "output_field": "price", "type": "decimal", "priority": 40, "precision": 2},
            {"row_id": "M-0307", "row_order": 7, "parser_path": "P-01", "source_field": "price",
             "output_field": "price", "type": "decimal", "priority": 30, "precision": 2},
            {"row_id": "M-0308", "row_order": 8, "parser_path": "P-07", "source_field": "listed_at_alt",
             "output_field": "listed_at", "type": "date", "priority": 40,
             "date_template": "YYYY-MM-DDTHH:mm:ssZ"},
            {"row_id": "M-0309", "row_order": 9, "parser_path": "P-01", "source_field": "listed_at",
             "output_field": "listed_at", "type": "date", "priority": 30,
             "date_template": "YYYY/MM/DD"},
            {"row_id": "M-0310", "row_order": 10, "parser_path": "P-01", "source_field": "stock",
             "output_field": "stock", "type": "integer", "priority": 30},
            {"row_id": "M-0311", "row_order": 11, "parser_path": "P-05", "source_field": "rating",
             "output_field": "rating", "type": "decimal", "priority": 30, "precision": 1},
            {"row_id": "M-0312", "row_order": 12, "parser_path": "P-01", "source_field": "seller_name",
             "output_field": "seller_name", "type": "string", "priority": 30,
             "null_placeholder": "（未知卖家）"},
            {"row_id": "M-0313", "row_order": 13, "parser_path": "P-06", "source_field": "upc",
             "output_field": "upc", "type": "string", "priority": 30},
            {"row_id": "M-0314", "row_order": 14, "parser_path": "P-01", "source_field": "warehouse",
             "output_field": "warehouse", "type": "string", "priority": 30},
        ],
    },
}

# 样例批次：records 里的 paths 模拟解析模板已经产出的原始值。
BATCHES: dict[str, dict[str, Any]] = {
    "BATCH-NORMAL": {
        "batch_id": "BATCH-NORMAL",
        "label": "正常样例（2 条）",
        "kind": "normal",
        "description": "两条商品记录，包含多路径标题/价格冲突、空值与日期覆盖。",
        "transient_failures": 0,
        "records": [
            {
                "record_id": "R-001",
                "paths": {
                    "P-01": {"sku": "SKU-1001", "title": "无线鼠标 M2", "price": "129.9000",
                             "listed_at": "2026/03/08 10:20:00", "stock": "12",
                             "seller_name": "京东自营", "warehouse": "WH-SH-01"},
                    "P-02": {"sku_alt": "SKU-1001", "title_alt": "无线鼠标 M2 白色",
                             "internal_hash": "h-9f2c41"},
                    "P-03": {"title_legacy": "无线鼠标M2旧版"},
                    "P-04": {"price_raw": "￥1,299.00"},
                    "P-05": {"rating": "4.567"},
                    "P-06": {"upc": "06901234567890"},
                    "P-07": {"listed_at_alt": "2026-03-08"},
                    "P-08": {"internal_hash": "h-9f2c41"},
                },
            },
            {
                "record_id": "R-002",
                "paths": {
                    "P-01": {"sku": "SKU-1002", "title": None, "price": "89.5",
                             "listed_at": "2026-04-01T08:00:00Z", "stock": "0",
                             "seller_name": None, "warehouse": None},
                    "P-02": {"sku_alt": "SKU-1002", "title_alt": None,
                             "internal_hash": "h-11ab22"},
                    "P-03": {"title_legacy": "折叠键盘 K1"},
                    "P-04": {"price_raw": "￥89.50"},
                    "P-05": {"rating": "3.9"},
                    "P-06": {"upc": "6901234567891"},
                    "P-07": {"listed_at_alt": None},
                    "P-08": {"internal_hash": "h-11ab22"},
                },
            },
        ],
    },
    "BATCH-EMPTY": {
        "batch_id": "BATCH-EMPTY",
        "label": "空数据（0 条）",
        "kind": "empty",
        "description": "解析完成但没有任何记录，用于核对空态。",
        "transient_failures": 0,
        "records": [],
    },
    "BATCH-BOUNDARY": {
        "batch_id": "BATCH-BOUNDARY",
        "label": "边界输入（1 条）",
        "kind": "boundary",
        "description": "空白标题、0.005 舍入、最大日期、全零条码、零库存等边界值。",
        "transient_failures": 0,
        "records": [
            {
                "record_id": "R-EDGE-001",
                "paths": {
                    "P-01": {"sku": "SKU-9999", "title": "   ", "price": "0.005",
                             "listed_at": "2026-12-31T23:59:59+08:00", "stock": "0",
                             "seller_name": "", "warehouse": "WH-EDGE"},
                    "P-02": {"sku_alt": "SKU-9999", "title_alt": "  边界 标题  ",
                             "internal_hash": "h-edge"},
                    "P-03": {"title_legacy": "边界标题-旧版"},
                    "P-04": {"price_raw": "￥1,234,567.891"},
                    "P-05": {"rating": "5.000"},
                    "P-06": {"upc": "00000000000000"},
                    "P-07": {"listed_at_alt": "20261231"},
                    "P-08": {"internal_hash": "h-edge"},
                },
            }
        ],
    },
    "BATCH-INVALID": {
        "batch_id": "BATCH-INVALID",
        "label": "类型异常（1 条）",
        "kind": "invalid",
        "description": "价格、库存、日期无法按声明类型转换，预览应给出阻断原因且不允许确认。",
        "transient_failures": 0,
        "records": [
            {
                "record_id": "R-BAD-001",
                "paths": {
                    "P-01": {"sku": "SKU-5001", "title": "异常商品", "price": "N/A",
                             "listed_at": "not-a-date", "stock": "十二",
                             "seller_name": "测试卖家", "warehouse": "WH-BAD"},
                    "P-02": {"sku_alt": "SKU-5001", "title_alt": "异常商品", "internal_hash": "h-bad"},
                    "P-03": {"title_legacy": None},
                    "P-04": {"price_raw": "价格面议"},
                    "P-05": {"rating": "NaN"},
                    "P-06": {"upc": "6900000000000"},
                    "P-07": {"listed_at_alt": "2026-13-40"},
                    "P-08": {"internal_hash": "h-bad"},
                },
            }
        ],
    },
    "BATCH-TRANSIENT": {
        "batch_id": "BATCH-TRANSIENT",
        "label": "瞬时失败（可重试）",
        "kind": "transient",
        "description": "首次解析模拟超时，重试后使用同一批次数据正常完成。",
        "transient_failures": 1,
        "records": [
            {
                "record_id": "R-RETRY-001",
                "paths": {
                    "P-01": {"sku": "SKU-2001", "title": "重试商品", "price": "19.90",
                             "listed_at": "2026-05-01", "stock": "3",
                             "seller_name": "重试卖家", "warehouse": "WH-RETRY"},
                    "P-02": {"sku_alt": "SKU-2001", "title_alt": "重试商品", "internal_hash": "h-retry"},
                    "P-03": {"title_legacy": None},
                    "P-04": {"price_raw": "￥19.90"},
                    "P-05": {"rating": "4.8"},
                    "P-06": {"upc": "6901234500001"},
                    "P-07": {"listed_at_alt": "2026-05-01"},
                    "P-08": {"internal_hash": "h-retry"},
                },
            }
        ],
    },
}

# FORM-0003 的历史输出版本：按生成时的输出格式固化，不随当前转换器配置变化。
HISTORICAL_RESULTS: list[dict[str, Any]] = [
    {
        "result_no": "CONV-0001",
        "converter_id": 3,
        "converter_no": "FORM-0003",
        "parser_no": "PARS-0002",
        "mapping_table_id": "MAP-0003",
        "batch_id": "BATCH-NORMAL",
        "output_version": 1,
        "output_format": "NDJSON",
        "record_count": 1,
        "created_at": "2026-09-20 10:12:00",
        "created_by": "历史归档",
        "output_content": '{"product_id":"SKU-0901","title":"历史商品","price":"199.00","listed_at":"2026/09/18"}\n',
        "spec_snapshot": {
            "output_format": "NDJSON",
            "date_template": "YYYY/MM/DD",
            "precision": 3,
            "null_policy": "保留空值",
        },
        "note": "历史版本 v1：当时输出 NDJSON，日期模板 YYYY/MM/DD。",
    },
    {
        "result_no": "CONV-0002",
        "converter_id": 3,
        "converter_no": "FORM-0003",
        "parser_no": "PARS-0002",
        "mapping_table_id": "MAP-0003",
        "batch_id": "BATCH-NORMAL",
        "output_version": 2,
        "output_format": "CSV",
        "record_count": 1,
        "created_at": "2026-10-05 09:30:00",
        "created_by": "历史归档",
        "output_content": "product_id,title,price,listed_at\nSKU-0902,历史商品二,299.00,2026/10/01\n",
        "spec_snapshot": {
            "output_format": "CSV",
            "date_template": "YYYY/MM/DD",
            "precision": 2,
            "null_policy": "保留空值",
        },
        "note": "历史版本 v2：切换为 CSV，日期模板仍为 YYYY/MM/DD。",
    },
]
