# sqlglot-mcp

基于 [sqlglot](https://github.com/tobymao/sqlglot) 的 **SQL 血缘分析 MCP 服务**,通过
[Model Context Protocol](https://modelcontextprotocol.io) 暴露给 Claude Desktop / Cursor / 自研客户端等使用。

支持:

- **表级血缘**: 识别写出语句(`SELECT` / `INSERT INTO ... SELECT` / `CREATE TABLE AS` / `MERGE` / `UPDATE`)的目标表、物理来源表,以及中间表(CTE)的展开。
- **列级血缘**: 每个输出列追溯到物理来源列(含表达式变换,如 `concat`、`sum`、`case when`)。
- **字段溯源**: 输出"输出列 → 中间步骤(CTE/子查询)→ 物理来源列"的完整链路。
- **多方言**: 工具参数 `dialect` 自由指定(sqlglot 支持的二十七种以上方言,默认 `presto`)。

## 快速开始

```bash
# 依赖 python >= 3.10
pip install -e .
# 启动(stdio 模式,MCP 客户端会自动拉起)
sqlglot-mcp
```

也可以直接运行 `python -m sqlglot_mcp`。

> 若遇到 PEP 668(externally-managed-environment)报错,可加 `--break-system-packages`,
> 或使用 venv / uv / conda 环境。

## MCP 客户端配置

### Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sqlglot": {
      "command": "/absolute/path/to/venv/bin/sqlglot-mcp",
      "args": []
    }
  }
}
```

### Cursor

Settings → MCP → Add server,command 填 `sqlglot-mcp`(若在 venv 中,填 venv 下的可执行文件绝对路径)。

### 通用(自研 MCP 客户端)

```json
{
  "mcpServers": {
    "sqlglot": {
      "command": "sqlglot-mcp",
      "args": []
    }
  }
}
```

## 工具列表

| 工具 | 说明 |
| --- | --- |
| `analyze_lineage(sql, dialect="presto", with_full_trace=true, schema="")` | 分析 SQL 血缘,返回 JSON(表级 `table_lineage` + 列级 `columns` + 溯源 `paths`);`schema` 为可选表结构 JSON,用于解析未限定/有歧义的列 |
| `validate_sql(sql, dialect="presto")` | 校验 SQL 可解析性,返回语句类型/目标表/来源表 |
| `list_dialects()` | 列出当前环境支持的方言 |

`schema` 参数示例(可传对象或 JSON 字符串):

```json
{"dim.user": ["id", "name", "org_id"], "dim.org": ["id", "name"]}
```

当查询里出现未加表前缀的列(如 CTE 内部 `SELECT id, org_name FROM ...`),只要某列只存在于一张表,提供 schema 后即可精确溯源到物理表;同时存在于多张表的列会如实标记为 `unknown_sources`(不猜测)。

## 返回值示例

```jsonc
{
  "ok": true,
  "dialect": "presto",
  "statement": "insert",
  "target_table": "dws.user_daily",
  "table_lineage": {
    "target": "dws.user_daily",
    "operation": "insert",
    "sources": ["dim.user", "dim.region", "ods.orders"],
    "intermediate_tables": [
      {"name": "user_region", "type": "cte", "sources": ["dim.user", "dim.region"]},
      {"name": "orders_agg", "type": "cte", "sources": ["ods.orders"]}
    ],
    "edges": [
      {"from": "dim.user", "to": "dws.user_daily"},
      {"from": "dim.region", "to": "dws.user_daily"},
      {"from": "ods.orders", "to": "dws.user_daily"}
    ]
  },
  "columns": [
    {
      "output_column": "total_amount",
      "source_columns": [
        {"table": "ods.orders", "column": "amount", "via": "o.amount", "qualified": "ods.orders.amount"}
      ],
      "unknown_sources": 0,
      "paths": [
        [
          {"node": "total_amount", "expr": "...", "is_source": false, "reference": null},
          {"node": "oa.total_amount", "expr": "...", "is_source": false, "reference": "orders_agg"},
          {"node": "o.amount", "expr": "ods.orders AS o", "is_source": true,
           "table": "ods.orders", "source_column": "o.amount"}
        ]
      ]
    }
  ],
  "errors": []
}
```

字段说明:

- `table_lineage.sources`: 物理来源表(CTE 别名已被展开);`intermediate_tables` 给出每个 CTE 的物理来源。
- `columns[].source_columns`: 输出列的直接来源列(`table` 物理表,`column` 裸列名,`via` 是 SQL 中的写法,`qualified` 为全限定名)。
- `columns[].paths`: 字段溯源链路,`reference` 指向中间表/CTE 别名,`is_source=true` 的节点为最终物理来源;
  `unknown_sources` 表示无法追溯到具体列的来源(如 `count(*)`、未提供 schema 的外部表)。
- `errors`: 某些列解析失败时的提示(单列失败不影响整体结果)。

## 开发

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v   # 运行测试(核心逻辑 + MCP 协议集成)
```

目录结构:

```
src/sqlglot_mcp/
├── lineage.py    # 血缘分析核心(纯函数,可独立复用)
├── server.py     # MCP 服务层(MCPServer, stdio)
└── __main__.py   # python -m sqlglot_mcp 入口
tests/
├── test_lineage.py   # 血缘逻辑单元测试
└── test_server.py    # MCP stdio JSON-RPC 集成测试
```