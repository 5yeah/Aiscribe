"""允许 `python -m ai_report` 直接调用 CLI。"""

from ai_report.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
