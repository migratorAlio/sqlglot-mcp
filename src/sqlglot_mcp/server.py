"""MCP 服务层: 基于 mcp 2.x 的 MCPServer,stdio 传输。

提供三个工具:
- analyze_lineage  分析 SQL 血缘(表级 + 列级 + 字段溯源)
- validate_sql     校验 SQL 与方言
- list_dialects    列出 sqlglot 支持的方言
"""

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer

from sqlglot_mcp import __version__
from sqlglot_mcp.lineage import (
    analyze,
    list_supported_dialects,
    normalize_dialect,
)

server = MCPServer(
    "sqlglot-mcp",
    version=__version__,
    instructions=(
        "SQL 血缘分析服务(基于 sqlglot)。"
        "可用工具 analyze_lineage / validate_sql / list_dialects。"
        "analyze_lineage 返回 JSON: table_lineage(表级依赖)、"
        "columns(列级血缘与字段溯源 paths)。"
    ),
)


@server.tool()
def analyze_lineage(
    sql: str,
    dialect: str = "presto",
    with_full_trace: bool = True,
    schema: str = "",
) -> str:
    """分析 SQL 血缘。返回 JSON 字符串,包含表级依赖(table_lineage)与列级血缘(columns,含 source_columns 和 paths 溯源链路)。

    Args:
        sql: 待分析的 SQL 语句(支持 SELECT / INSERT INTO ... SELECT / CREATE TABLE AS / MERGE 等)。
        dialect: SQL 方言,默认 presto;可传 trino/spark/hive/mysql/postgres/clickhouse 等,详见 list_dialects。
        with_full_trace: 是否在结果中包含每个输出列到来源列的完整溯源路径(full paths)。
        schema: 可选的表结构 JSON,用于解析未限定/有歧义的列,如 {"dim.user": ["id", "name"], "dim.org": ["id"]}。
    """
    return json.dumps(
        analyze(
            sql,
            dialect=dialect,
            with_full_trace=with_full_trace,
            schema=schema or None,
        ),
        ensure_ascii=False,
        default=str,
    )


@server.tool()
def validate_sql(sql: str, dialect: str = "presto") -> str:
    """校验 SQL 可解析性并返回语句类型/目标表。返回 JSON。

    Args:
        sql: 待校验的 SQL。
        dialect: SQL 方言,默认 presto。
    """
    try:
        normalize_dialect(dialect)
        result = analyze(sql, dialect=dialect, with_full_trace=False)
        if not result.get("ok"):
            return json.dumps(result, ensure_ascii=False, default=str)
        return json.dumps(
            {
                "ok": True,
                "dialect": dialect,
                "statement": result["statement"],
                "target_table": result["target_table"],
                "sources": result["table_lineage"]["sources"],
                "column_count": len(result["columns"]),
            },
            ensure_ascii=False,
            default=str,
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@server.tool()
def list_dialects() -> str:
    """列出当前环境 sqlglot 支持的全部 SQL 方言。返回 JSON 数组。"""
    return json.dumps(list_supported_dialects(), ensure_ascii=False)


def main() -> None:
    """stdio 入口,供 `sqlglot-mcp` 命令或 `python -m sqlglot_mcp` 调用。"""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()