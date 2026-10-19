"""MCP stdio 独立启动器（供 TRAE / 任意 MCP 客户端配置使用）。

python 运行本脚本时会自动把脚本所在目录加入 sys.path，
因此无论客户端从哪个工作目录、是否设置 PYTHONPATH，都能找到 ai_report 包。

TRAE 配置示例：
    {"command": "python", "args": ["f:/DSH/Aiscribe/run_mcp_stdio.py"]}
"""

from ai_report.mcp_server import main

if __name__ == "__main__":
    raise SystemExit(main(["--stdio"]))
