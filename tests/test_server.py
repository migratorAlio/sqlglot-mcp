"""MCP stdio 协议层集成测试(不依赖任何 MCP 客户端 SDK)。

通过子进程启动 server,直接按 MCP 的 newline-delimited JSON-RPC
协议做握手: initialize -> notifications/initialized -> tools/list -> tools/call。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))


class TestMcpStdio(unittest.TestCase):
    def _handshake(self, tool_name: str, arguments: dict) -> tuple[dict, dict]:
        proc = subprocess.Popen(
            [sys.executable, "-m", "sqlglot_mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=ENV,
        )
        assert proc.stdin and proc.stdout

        def send(msg: dict) -> None:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        def recv() -> dict:
            line = proc.stdout.readline()
            if not line:
                raise AssertionError(
                    f"server 提前退出: {proc.stderr.read()}"
                )
            return json.loads(line)

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "0.0.1"},
                },
            }
        )
        init_resp = recv()
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_resp = recv()
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments,
                },
            }
        )
        call_resp = recv()
        proc.stdin.close()
        try:
            proc.wait(timeout=30)
        finally:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        return init_resp, tools_resp, call_resp

    def test_tools_list(self):
        _, tools_resp, _ = self._handshake("analyze_lineage", {"sql": "SELECT 1"})
        names = {t["name"] for t in tools_resp["result"]["tools"]}
        self.assertIn("analyze_lineage", names)
        self.assertIn("validate_sql", names)
        self.assertIn("list_dialects", names)

    def test_analyze_lineage_tool(self):
        _, _, call_resp = self._handshake(
            "analyze_lineage",
            {
                "sql": "INSERT INTO t SELECT a, b FROM s",
                "dialect": "presto",
                "with_full_trace": True,
            },
        )
        payload = json.loads(call_resp["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target_table"], "t")
        self.assertEqual(payload["table_lineage"]["sources"], ["s"])
        self.assertEqual(payload["columns"][0]["output_column"], "a")

    def test_list_dialects_tool(self):
        _, _, call_resp = self._handshake("list_dialects", {})
        dialects = json.loads(call_resp["result"]["content"][0]["text"])
        self.assertIn("presto", dialects)

    def test_error_response(self):
        _, _, call_resp = self._handshake(
            "analyze_lineage", {"sql": "SELECT 1", "dialect": "bad"}
        )
        payload = json.loads(call_resp["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertIn("未知方言", payload["error"])


if __name__ == "__main__":
    unittest.main()