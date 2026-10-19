"""配置加载：数据源路径自动发现、环境变量覆盖、报告输出目录。

约定：
- 数据源路径按**当前用户**环境自动推导（%APPDATA% / %LOCALAPPDATA% / 用户主目录），
  换台电脑不用改代码；同一台机器有多个用户时，会兜底搜索其他用户目录下的同名相对路径
- 每个路径都可用环境变量显式覆盖（键名见 default_paths()）
- 敏感字段（API Key / AppSecret）只在内存中读取，展示时一律打码
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List

# ---- 包根目录（<repo>/ai_report 的上一级），报告默认写到 <根>/reports ----
ROOT_DIR = Path(__file__).resolve().parent.parent

#: 报告输出目录的环境变量名（覆盖 <根>/reports）
ENV_REPORTS_DIR = "AIREREPORT_REPORTS_DIR"
REPORTS_DIR = Path(os.environ.get(ENV_REPORTS_DIR) or (ROOT_DIR / "reports"))

# ---- 钉钉配置文件位置（钉钉提交当前默认停用，模块保留）----
DINGTALK_CONFIG_FILE = ROOT_DIR / "dingtalk_config.json"
DINGTALK_CONFIG_EXAMPLE = ROOT_DIR / "dingtalk_config.example.json"

# ---- 各数据源在“用户目录”下的相对位置（不含用户名，换机靠自动推导）----
CODEBUDDY_CN_REL = ("CodeBuddy CN", "codebuddy-sessions.vscdb")     # %APPDATA%（+logs/）
WORKBUDDY_REL = (".workbuddy", "workbuddy.db")                      # 用户主目录
CASS_REL = ("coding-agent-search", "coding-agent-search", "data", "agent_search.db")  # %APPDATA%
DOUBAO_REL = ("Doubao", "User Data", "Default", "IndexedDB")        # %LOCALAPPDATA%
KIMI_REL = ("kimi-desktop",)                                        # %APPDATA%
TRAE_MEMORY_REL = (".trae-cn", "memory", "projects")                # 用户主目录
#: TRAE 工作区根目录名（%APPDATA% 下），新版/旧版各一个
TRAE_ROOT_NAMES = ("Trae CN", "TRAE SOLO CN")


def _home() -> Path:
    """当前用户主目录（Windows 上即 C:/Users/<用户名>）。"""
    return Path(os.path.expanduser("~"))


def _other_homes() -> List[Path]:
    """同机其他用户的用户目录（多账号机器兜底）；排除 Public 等非用户目录。"""
    me = _home()
    root = me.parent
    if not root.is_dir():
        return []
    out: List[Path] = []
    try:
        for d in sorted(root.iterdir()):
            if d != me and d.is_dir() and (d / "AppData").is_dir():
                out.append(d)
    except OSError:
        return []
    return out


def _dedupe(paths: Iterable[Path]) -> List[Path]:
    """去重（Windows 路径大小写不敏感），保持顺序。"""
    seen, out = set(), []
    for p in paths:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _pick(candidates: List[Path]) -> str:
    """返回第一个存在的候选路径；都不存在时返回第一个（供展示与报错）。"""
    for c in candidates:
        if c.exists():
            return str(c)
    return str(candidates[0]) if candidates else ""


def _trae_roots(roaming: List[Path]) -> str:
    """TRAE 工作区根目录（分号分隔）：存在多少个就收多少个。"""
    found = [str(b / n) for b in roaming for n in TRAE_ROOT_NAMES if (b / n).is_dir()]
    if found:
        return ";".join(found)
    return ";".join(str(roaming[0] / n) for n in TRAE_ROOT_NAMES)


def default_paths() -> Dict[str, str]:
    """按当前机器自动推导各数据源路径（当前用户优先，其他用户目录兜底）。"""
    homes = _dedupe([_home()] + _other_homes())
    roaming = _dedupe([Path(os.environ["APPDATA"]) if os.environ.get("APPDATA")
                       else _home() / "AppData" / "Roaming"]
                      + [h / "AppData" / "Roaming" for h in homes])
    local = _dedupe([Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA")
                     else _home() / "AppData" / "Local"]
                    + [h / "AppData" / "Local" for h in homes])
    return {
        "CODEBUDDY_CN_DB": _pick([b.joinpath(*CODEBUDDY_CN_REL) for b in roaming]),
        "WORKBUDDY_DB": _pick([h.joinpath(*WORKBUDDY_REL) for h in homes]),
        "CASS_DB": _pick([b.joinpath(*CASS_REL) for b in roaming]),
        "DOUBAO_IDB_DIR": _pick([b.joinpath(*DOUBAO_REL) for b in local]),
        "KIMI_DIR": _pick([b.joinpath(*KIMI_REL) for b in roaming]),
        "TRAE_ROOTS": _trae_roots(roaming),
        "TRAE_MEMORY_DIR": _pick([h.joinpath(*TRAE_MEMORY_REL) for h in homes]),
    }


def get_db_path(env_key: str) -> str:
    """读取数据源路径：环境变量优先，否则用自动推导的默认路径。"""
    return os.environ.get(env_key) or default_paths().get(env_key, "")


def ensure_reports_dir() -> Path:
    """确保 reports 目录存在并返回。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return REPORTS_DIR


def _mask(value: str | None) -> str:
    """敏感值打码展示：abc123456 -> ab****56。"""
    if not value:
        return "(未设置)"
    if len(value) <= 6:
        return "***"
    return value[:2] + "****" + value[-2:]


def describe() -> Dict[str, str]:
    """生成当前配置概览（敏感字段已打码），供 `aireport config` 展示。"""
    paths = default_paths()
    info = [("包根目录", str(ROOT_DIR)), ("报告输出目录", str(REPORTS_DIR))]
    labels = {
        "CODEBUDDY_CN_DB": "CodeBuddy CN 数据库",
        "WORKBUDDY_DB": "WorkBuddy 数据库",
        "CASS_DB": "Cass 数据库",
        "DOUBAO_IDB_DIR": "豆包 IndexedDB",
        "KIMI_DIR": "Kimi 目录",
        "TRAE_ROOTS": "TRAE 工作区",
        "TRAE_MEMORY_DIR": "TRAE 记忆目录",
    }
    for key, label in labels.items():
        source = "环境变量" if os.environ.get(key) else "自动发现"
        info.append((label, f"{get_db_path(key)}（{source}）"))
    info.append(("LLM 提供商", os.environ.get("LLM_PROVIDER", "deepseek")))
    info.append(("DEEPSEEK_API_KEY", _mask(os.environ.get("DEEPSEEK_API_KEY"))))
    return dict(info)
