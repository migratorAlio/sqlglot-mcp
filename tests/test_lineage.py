"""血缘分析核心逻辑单元测试(不依赖 MCP)。"""

from __future__ import annotations

import json
import unittest

from sqlglot_mcp.lineage import analyze, list_supported_dialects, normalize_dialect

SAMPLE_SQL = """
INSERT INTO dws.user_daily (user_id, name, region, total_amount, order_cnt)
WITH user_region AS (
    SELECT u.user_id, u.name, r.region_name AS region
    FROM dim.user u
    JOIN dim.region r ON r.region_id = u.region_id
),
orders_agg AS (
    SELECT o.user_id, SUM(o.amount) AS total_amount, COUNT(*) AS order_cnt
    FROM ods.orders o
    WHERE o.dt = '2026-09-23'
    GROUP BY o.user_id
)
SELECT ur.user_id,
       ur.name,
       ur.region,
       oa.total_amount,
       oa.order_cnt
FROM user_region ur
JOIN orders_agg oa ON oa.user_id = ur.user_id
"""


class TestLineageTableLevel(unittest.TestCase):
    def test_dialect_normalize(self):
        self.assertEqual(normalize_dialect("presto"), "Presto")
        self.assertEqual(normalize_dialect("TRINO"), "Trino")
        with self.assertRaises(ValueError):
            normalize_dialect("no_such_dialect")

    def test_list_dialects(self):
        dialects = list_supported_dialects()
        self.assertIn("presto", dialects)
        self.assertIn("spark", dialects)
        self.assertGreater(len(dialects), 20)

    def test_target_and_sources(self):
        result = analyze(SAMPLE_SQL, dialect="presto")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["statement"], "insert")
        self.assertEqual(result["target_table"], "dws.user_daily")
        self.assertEqual(
            set(result["table_lineage"]["sources"]),
            {"dim.user", "dim.region", "ods.orders"},
        )

    def test_intermediate_ctes(self):
        result = analyze(SAMPLE_SQL, dialect="presto")
        intermediates = {
            cte["name"]: cte["sources"] for cte in result["table_lineage"]["intermediate_tables"]
        }
        self.assertEqual(set(intermediates), {"user_region", "orders_agg"})
        self.assertEqual(set(intermediates["user_region"]), {"dim.user", "dim.region"})
        self.assertEqual(set(intermediates["orders_agg"]), {"ods.orders"})

    def test_ctas(self):
        result = analyze(
            "CREATE TABLE analytics.total AS SELECT a, b FROM raw.src WHERE b > 0",
            dialect="spark",
        )
        self.assertEqual(result["statement"], "create")
        self.assertEqual(result["target_table"], "analytics.total")
        self.assertEqual(result["table_lineage"]["sources"], ["raw.src"])

    def test_ctas_with_cte_as_with(self):
        result = analyze(
            "CREATE TABLE gold.t AS WITH c AS (SELECT id FROM silver.s) SELECT id FROM c",
            dialect="spark",
        )
        self.assertEqual(result["statement"], "create")
        self.assertEqual(result["target_table"], "gold.t")
        self.assertEqual(result["table_lineage"]["sources"], ["silver.s"])

    def test_unsupported_syntax_reports_error(self):
        # CREATE TABLE t WITH cte AS (...) SELECT ... 不被 sqlglot 支持,
        # 会被回退为 Command —— 应如实报错而不是假装成 select
        result = analyze(
            "CREATE TABLE t WITH c AS (SELECT id FROM s) SELECT id FROM c",
            dialect="spark",
        )
        self.assertFalse(result["ok"])
        self.assertIn("Command", result["error"])


class TestLineageColumnLevel(unittest.TestCase):
    def test_column_sources(self):
        result = analyze(SAMPLE_SQL, dialect="presto")
        cols = {c["output_column"]: c for c in result["columns"]}
        self.assertEqual(set(cols), {"user_id", "name", "region", "total_amount", "order_cnt"})

        def refs(column):
            return {(s["table"], s["column"]) for s in cols[column]["source_columns"]}

        # 关联列应能追溯到 CTE/物理来源
        self.assertIn(("dim.user", "user_id"), refs("user_id"))
        # SUM(o.amount) -> ods.orders.amount(经 orders_agg 中间表)
        self.assertIn(("ods.orders", "amount"), refs("total_amount"))
        # 全限定名便于直接使用
        qualified = {s["qualified"] for s in cols["total_amount"]["source_columns"]}
        self.assertIn("ods.orders.amount", qualified)
        # COUNT(*) 无明确来源列 -> 存在 unknown 来源
        self.assertEqual(cols["order_cnt"]["unknown_sources"], 1)

    def test_paths_include_intermediate_hops(self):
        result = analyze(SAMPLE_SQL, dialect="presto")
        col = next(c for c in result["columns"] if c["output_column"] == "total_amount")
        # 至少一条路径: total_amount -> orders_agg(total_amount) -> ods.orders.amount
        path = col["paths"][0]
        hops = [hop for hop in path]
        self.assertIn("total_amount", [h["node"] for h in hops])
        # 中间表跳: 该 hop 的 reference 指向 CTE 别名 orders_agg
        self.assertTrue(any(h["reference"] == "orders_agg" for h in hops), hops)
        # 最后一步是物理来源
        self.assertTrue(hops[-1]["is_source"])
        self.assertEqual((hops[-1]["table"], hops[-1]["source_column"]), ("ods.orders", "o.amount"))
        self.assertEqual(len(hops), 3)

    def test_simple_select(self):
        result = analyze(
            "SELECT id, concat(first_name, '.', last_name) AS full_name FROM app.users",
            dialect="presto",
        )
        self.assertEqual(result["statement"], "select")
        self.assertIsNone(result["target_table"])
        cols = {c["output_column"]: c for c in result["columns"]}
        self.assertEqual(set(cols), {"id", "full_name"})
        refs = {(s["table"], s["column"]) for s in cols["full_name"]["source_columns"]}
        self.assertIn(("app.users", "first_name"), refs)
        self.assertIn(("app.users", "last_name"), refs)

    def test_schema_resolves_ambiguous_columns(self):
        # CTE 内部列未限定: 无 schema 时 org_name 也无法确定物理来源
        sql = ("INSERT INTO dws.user_daily "
               "WITH cte AS (SELECT id, org_name FROM dim.user u JOIN dim.org o ON o.id = u.org_id) "
               "SELECT c.id AS user_id, c.org_name AS org FROM cte c")
        result = analyze(sql, dialect="presto")
        by_name = {c["output_column"]: c for c in result["columns"]}
        self.assertEqual(by_name["org"]["unknown_sources"], 1)

        # 提供 schema 后,仅属于 dim.org 的 org_name 被精确解析
        result = analyze(
            sql,
            dialect="presto",
            schema={"dim.user": ["id", "org_id"], "dim.org": ["id", "org_name"]},
        )
        self.assertEqual(result["errors"], [])
        by_name = {c["output_column"]: c for c in result["columns"]}
        refs = {(s["table"], s["column"]) for s in by_name["org"]["source_columns"]}
        self.assertIn(("dim.org", "org_name"), refs)
        # 两张表都有的 id 仍然被如实标记为未知来源(不瞎猜)
        self.assertEqual(by_name["user_id"]["unknown_sources"], 1)

    def test_schema_accepts_json_string(self):
        result = analyze(
            "SELECT a FROM t",
            dialect="presto",
            schema='{"t": ["a"]}',
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])

    def test_schema_invalid_json(self):
        result = analyze("SELECT a FROM t", dialect="presto", schema="not json{{")
        self.assertFalse(result["ok"])
        self.assertIn("schema", result["error"])

    def test_union(self):
        result = analyze(
            "SELECT a FROM t1 UNION SELECT b FROM t2", dialect="presto"
        )
        self.assertEqual(result["table_lineage"]["sources"], ["t1", "t2"])


class TestLineageErrors(unittest.TestCase):
    def test_parse_error(self):
        result = analyze("SELECT * FORM t", dialect="presto")
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_bad_dialect(self):
        result = analyze("SELECT 1", dialect="not_a_dialect")
        self.assertFalse(result["ok"])
        self.assertIn("未知方言", result.get("error", ""))

    def test_json_serializable(self):
        for sql in (SAMPLE_SQL, "SELECT a, b FROM t", "SELECT * FROM t"):
            result = analyze(sql, dialect="presto")
            json.dumps(result, ensure_ascii=False)  # 不应抛异常


if __name__ == "__main__":
    unittest.main()