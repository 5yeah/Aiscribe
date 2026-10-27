"""Build the Aiscribe stdio sidecar with PyInstaller and place it in dsh-plugin/runtime.

Usage (from the repository root):
    python scripts/build_sidecar.py

Requires PyInstaller in the current interpreter:
    python -m pip install pyinstaller zstandard

Use a clean virtualenv if the system Python has an old `pathlib` backport
(PyInstaller refuses to run with it):
    python -m venv build/venv
    build/venv/Scripts/python -m pip install pyinstaller zstandard
    build/venv/Scripts/python scripts/build_sidecar.py

The output is a one-folder build (faster startup than --onefile):
    dsh-plugin/runtime/<platform>-<arch>/aireport[.exe]
    dsh-plugin/runtime/<platform>-<arch>/_internal/...

Setting AIREREPORT_BIN at runtime overrides discovery, which is useful for
testing a build from another directory.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "dsh-plugin"
ENTRY = ROOT / "packaging" / "sidecar_entry.py"
BUILD = ROOT / "build" / "sidecar"


def platform_tag() -> str:
    import platform
    key = f"{platform.system().lower()}-{platform.machine().lower()}"
    aliases = {
        "windows-amd64": "win32-x64",
        "windows-x86_64": "win32-x64",
        "windows-arm64": "win32-arm64",
        "darwin-arm64": "darwin-arm64",
        "darwin-x86_64": "darwin-x64",
        "linux-x86_64": "linux-x64",
        "linux-amd64": "linux-x64",
        "linux-aarch64": "linux-arm64",
    }
    return aliases.get(key, key)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Aiscribe MCP sidecar")
    parser.add_argument("--tag", default=platform_tag(),
                        help="runtime directory name (default: <platform>-<arch>)")
    args = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401
        import zstandard  # noqa: F401
    except ImportError as exc:
        print(f"缺少构建依赖：{exc.name}。"
              "请先运行：python -m pip install pyinstaller zstandard",
              file=sys.stderr)
        return 2

    dest = PLUGIN / "runtime" / args.tag
    dist = BUILD / "dist"
    work = BUILD / "work"
    spec = BUILD / "spec"
    for path in (dist, work, spec):
        path.mkdir(parents=True, exist_ok=True)

    sep = ";" if sys.platform == "win32" else ":"
    add_data = f"{ROOT / 'ai_report' / 'web'}{sep}ai_report/web"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--console",
        "--name", "aireport",
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(spec),
        "--paths", str(ROOT),
        "--add-data", add_data,
        "--collect-submodules", "ai_report",
        "--collect-all", "zstandard",
        "--exclude-module", "tkinter",
        "--exclude-module", "tests",
        str(ENTRY),
    ]
    print("+ " + " ".join(cmd))
    # 全局 bincache 常被安全软件锁住；把 PyInstaller 缓存也放进 build/
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(BUILD / "pyinstaller-cache")
    subprocess.run(cmd, check=True, cwd=ROOT, env=env)

    built = dist / "aireport"
    if not built.is_dir():
        print(f"PyInstaller 未生成预期目录：{built}", file=sys.stderr)
        return 1

    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(built, dest)
    exe = dest / ("aireport.exe" if sys.platform == "win32" else "aireport")
    size_mb = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file()) / 1024 / 1024
    print(f"\nsidecar 已生成：{exe}")
    print(f"目录大小：{size_mb:.1f} MB")
    print("下一步：cd dsh-plugin && npm pack")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
