"""基于 sqlglot 的 SQL 血缘分析核心逻辑。

与 MCP 层解耦,全部为纯函数,便于单独测试 / 复用:
- 表级血缘: 写出语句(SELECT / INSERT / CTAS / MERGE / UPDATE)的目标表
  与物理来源表,以及中间 CTE 的展开信息。
- 列级血缘: 对每个输出列用 sqlglot.lineage 追溯来源列。
- 字段溯源: 输出"输出列 -> 中间步骤 -> 物理来源列"的完整链路(paths)。

支持方言: 以工具参数形式让调用方自由选择 sqlglot 支持的任意方言。
"""

from __future__ import annotations

import json
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage as build_lineage
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.dialects import DIALECTS

# 常见方言,仅供提示用;实际上是开放的(supported = sqlglot 全体方言)
COMMON_DIALECTS = [
    "presto",
    "trino",
    "spark",
    "hive",
    "databricks",
    "snowflake",
    "bigquery",
    "clickhouse",
    "duckdb",
    "mysql",
    "postgres",
    "sqlite",
]

# 规范化方言名: "trino" -> "Trino" 等,方便校验与提示
_DIALECT_LOOKUP: dict[str, str] = {name.lower(): name for name in DIALECTS}


def normalize_dialect(dialect: str) -> str:
    """校验并规范化方言名,不存在时抛出 ValueError。"""
    key = (dialect or "").strip().lower()
    if key in _DIALECT_LOOKUP:
        return _DIALECT_LOOKUP[key]
    supported = ", ".join(sorted(_DIALECT_LOOKUP))
    raise ValueError(f"未知方言 {dialect!r},支持: {supported}")


def list_supported_dialects() -> list[str]:
    """返回 sqlglot 支持的全部方言名(规范化形式)。"""
    return sorted(_DIALECT_LOOKUP)


def parse_schema(schema: str | dict | None) -> dict | None:
    """解析可选的表结构参数(dict 或 JSON 字符串),并转换为 sqlglot 需要的嵌套格式。

    用户友好输入(扁平)支持两种:
      {"dim.user": ["id", "name"]}          # 仅列名
      {"dim.user": {"id": "int", "name": "string"}}   # 列名 -> 类型
    内部会转换成 sqlglot 的嵌套形式: {"dim": {"user": {"id": "unknown", ...}}}。
    """
    if schema is None:
        return None
    if isinstance(schema, str):
        schema = schema.strip()
        if not schema:
            return None
        try:
            schema = json.loads(schema)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"schema 需为合法 JSON 对象,如 {{\"dim.user\": [\"id\", \"name\"]}},错误: {exc}"
            ) from exc
    if not isinstance(schema, dict):
        raise ValueError("schema 需为 JSON 对象")

    nested: dict = {}
    for name, columns in schema.items():
        parts = [p for p in name.split(".") if p] if isinstance(name, str) else []
        if not parts:
            raise ValueError(f"schema 表名非法: {name!r}")
        if isinstance(columns, list):
            cols = {c: "unknown" for c in columns if isinstance(c, str)}
        elif isinstance(columns, dict):
            cols = columns
        else:
            raise ValueError(f"schema 表 {name!r} 的列需为数组或对象")
        node = nested
        for part in parts[:-1]:
            node = node.setdefault(part, {})  # type: ignore[assignment]
        node[parts[-1]] = cols
    return nested


def table_full_name(table: exp.Table) -> str:
    """返回表的完整名称,如 catalog.db.table / db.table / t。"""
    parts = [
        p
        for p in (
            getattr(table, "catalog", None),
            getattr(table, "db", None),
            table.this,
        )
        if p not in (None, "")
    ]
    return ".".join(str(p) for p in parts)


def statement_info(expression: exp.Expression) -> dict[str, Any]:
    """识别语句类型与目标(写出)表。"""
    if isinstance(expression, exp.Insert):
        return {"operation": "insert", "target": table_full_name(expression.this)}
    if isinstance(expression, exp.Merge):
        return {"operation": "merge", "target": table_full_name(expression.this)}
    if isinstance(expression, exp.Update):
        return {"operation": "update", "target": table_full_name(expression.this)}
    if isinstance(expression, exp.Delete):
        return {"operation": "delete", "target": table_full_name(expression.this)}
    if isinstance(expression, exp.Create):
        return {"operation": "create", "target": table_full_name(expression.this)}
    return {"operation": "select", "target": None}


def main_query(expression: exp.Expression) -> exp.Query | None:
    """取出可用于列级血缘分析的主查询(SELECT)。

    INSERT / CREATE 返回其内层 SELECT;MERGE 返回 USING 查询;
    纯 SELECT 直接返回自身。
    """
    if isinstance(expression, exp.Query):
        return expression
    q = expression.args.get("expression")
    if isinstance(q, exp.Query):
        return q
    using = expression.args.get("using")
    if isinstance(using, exp.Query):
        return using
    return None


def _tables_in_query(query: exp.Query, exclude: str | None = None) -> list[str]:
    """通过 scope 收集查询中直接引用的物理表(自动排除 CTE 别名)。"""
    seen: set[str] = set()
    out: list[str] = []
    for scope in traverse_scope(query):
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                name = table_full_name(source)
                if name != exclude and name not in seen:
                    seen.add(name)
                    out.append(name)
    return out


def physical_sources(
    expression: exp.Expression, exclude: str | None = None
) -> list[str]:
    """语句的物理来源表列表(不含目标表与 CTE 别名)。"""
    query = (
        expression
        if isinstance(expression, exp.Query)
        else main_query(expression)
    )
    if isinstance(query, exp.Query):
        return _tables_in_query(query, exclude=exclude)
    # 兜底(如 UPDATE / DELETE 等): 直接扫所有 Table 节点并剔除目标表
    seen: set[str] = set()
    out: list[str] = []
    for table in expression.find_all(exp.Table):
        name = table_full_name(table)
        if name != exclude and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def cte_summaries(expression: exp.Expression) -> list[dict[str, Any]]:
    """中间表(CTE)摘要: 每个 CTE 的别名及其引用的物理来源表。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cte in expression.find_all(exp.CTE):
        alias = cte.alias
        if alias in seen:
            continue
        seen.add(alias)
        inner = cte.this
        out.append(
            {
                "name": alias,
                "type": "cte",
                "sources": _tables_in_query(inner) if isinstance(inner, exp.Query) else [],
            }
        )
    return out


def output_columns(query: exp.Query) -> list[str]:
    """主查询的输出列名(对集合操作取第一个 SELECT)。"""
    if isinstance(query, exp.SetOperation):
        query = query.this  # 第一个 SELECT
    if isinstance(query, exp.Select):
        cols = [s.alias_or_name for s in query.expressions]
        return [c for c in cols if c and c not in ("*",)]
    return []


def node_entry(node: Any, dialect: str) -> dict[str, Any]:
    """把 lineage 图中的一个节点转成可读 dict。"""
    is_source = isinstance(node.expression, exp.Table)
    entry: dict[str, Any] = {
        "node": node.name,
        "expr": node.expression.sql(dialect=dialect),
        "is_source": is_source,
        "reference": node.reference_node_name or None,
    }
    if is_source:
        entry["table"] = table_full_name(node.expression)
        entry["source_column"] = node.name
    return entry


def source_column_ref(node: Any) -> dict[str, Any]:
    """来源列节点 -> 结构化引用(物理表 + 裸列名 + SQL 中的别名写法)。"""
    table = table_full_name(node.expression)
    name = node.name
    bare = name.rsplit(".", 1)[-1] if "." in name else name
    return {
        "table": table,
        "column": bare,
        "via": name,
        "qualified": f"{table}.{bare}" if table else name,
    }


def lineage_paths(node: Any, dialect: str) -> list[list[dict[str, Any]]]:
    """从输出列到每个来源列的完整路径(字段溯源/中间表展开)。"""
    paths: list[list[dict[str, Any]]] = []

    def dfs(n: Any, acc: list[dict[str, Any]]) -> None:
        cur = acc + [node_entry(n, dialect)]
        if not n.downstream:
            paths.append(cur)
            return
        for d in n.downstream:
            dfs(d, cur)

    dfs(node, [])
    return paths


def analyze(
    sql: str,
    dialect: str = "presto",
    with_full_trace: bool = True,
    schema: str | dict | None = None,
) -> dict[str, Any]:
    """分析一段 SQL 的血缘,返回结构化结果。

    schema 可选,用于解析查询中未加表限定符 / 存在歧义的列,
    形如 {"dim.user": ["id", "name"], "dim.org": ["id"]},可以是 dict 或 JSON 字符串。
    """
    try:
        normalize_dialect(dialect)
        expression = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001 - 解析失败信息直接回传给调用方
        return {
            "ok": False,
            "dialect": dialect,
            "error": str(exc),
            "sql": sql,
        }

    if isinstance(expression, exp.Command):
        # 解析器无法识别时会把语句回退为 Command(静默),需显式报告
        return {
            "ok": False,
            "dialect": dialect,
            "error": (
                "无法解析为受支持的语句类型(解析器回退为 Command;"
                "注意 CTAS 若带 CTE,需写成 CREATE TABLE t AS WITH ... SELECT)"
            ),
            "sql": sql,
        }

    try:
        schema_map = parse_schema(schema)
    except ValueError as exc:
        return {"ok": False, "dialect": dialect, "error": str(exc), "sql": sql}

    info = statement_info(expression)
    target = info["target"]
    sources = physical_sources(expression, exclude=target)
    intermediates = cte_summaries(expression)

    result: dict[str, Any] = {
        "ok": True,
        "dialect": dialect,
        "statement": info["operation"],
        "target_table": target,
        "table_lineage": {
            "target": target,
            "operation": info["operation"],
            "sources": sources,
            "intermediate_tables": intermediates,
            "edges": [{"from": s, "to": target} for s in sources] if target else [],
        },
        "columns": [],
        "errors": [],
    }

    query = main_query(expression)
    if isinstance(query, exp.Query):
        for col in output_columns(query):
            try:
                node = build_lineage(
                    col, query, dialect=dialect, schema=schema_map
                )
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"列 {col!r} 血缘解析失败: {exc}")
                continue

            all_nodes = list(node.walk())
            leaves = [n for n in all_nodes if not n.downstream]
            table_leaves = [
                n for n in leaves if isinstance(n.expression, exp.Table)
            ]
            entry: dict[str, Any] = {
                "output_column": col,
                "source_columns": [source_column_ref(n) for n in table_leaves],
                "unknown_sources": len(leaves) - len(table_leaves),
                "hops": [node_entry(n, dialect) for n in all_nodes],
            }
            if with_full_trace:
                entry["paths"] = lineage_paths(node, dialect)
            result["columns"].append(entry)

    return result