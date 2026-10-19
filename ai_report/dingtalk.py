"""L4-钉钉提交器：把报告提交到钉钉工作日志（带人工审核闸门）。

硬规则（人工审核闸门，用户强需求）：
- 仅在用户显式带 --confirm 时才真正发送，否则只做 dry-run 预览
- 凭证只从环境变量读取，缺失拒绝运行
- template_id / userid / 字段映射放在 dingtalk_config.json，不写死在代码
- 单字段 value ≤ 1000 字符，CONTENT_LIMIT=950 保险截断
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from ai_report.config import DINGTALK_CONFIG_FILE, DINGTALK_CONFIG_EXAMPLE
from ai_report.utils import log, truncate

GETTOKEN_URL = "https://oapi.dingtalk.com/gettoken"
CREATE_URL = "https://oapi.dingtalk.com/topapi/report/create"

# 钉钉单个 content.value 硬上限 1000 字符，留保险余量
CONTENT_LIMIT = 950


class DingTalkError(RuntimeError):
    """钉钉 API / 配置错误（清晰报错，不静默）。"""


def load_config(path: Optional[Path] = None) -> Dict:
    """读取 dingtalk_config.json（template_id / userid / 字段映射）。"""
    cfg_path = Path(path) if path else DINGTALK_CONFIG_FILE
    if not cfg_path.is_file():
        raise DingTalkError(
            f"钉钉配置文件不存在：{cfg_path}\n"
            f"请复制 {DINGTALK_CONFIG_EXAMPLE.name} 为 {cfg_path.name} 并填写模板ID/员工ID。"
        )
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise DingTalkError(f"钉钉配置文件读取失败：{exc}") from exc
    for key in ("template_id", "userid"):
        if not str(cfg.get(key, "")).strip():
            raise DingTalkError(f"钉钉配置缺少必填字段：{key}")
    return cfg


def require_credentials() -> tuple:
    """读取环境变量凭证，缺失则拒绝运行。"""
    appkey = os.environ.get("DINGTALK_APPKEY", "").strip()
    appsecret = os.environ.get("DINGTALK_APPSECRET", "").strip()
    if not appkey or not appsecret:
        raise DingTalkError(
            "缺少环境变量 DINGTALK_APPKEY / DINGTALK_APPSECRET，拒绝运行。\n"
            "请先在系统环境变量中配置钉钉应用凭证（应用需具备 qyapi_report_manage 权限）。"
        )
    return appkey, appsecret


def resolve_source(report_md: str, mapping: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
    """决定报告内容如何映射到日志字段。

    mapping 形如 {"今日完成工作": "ALL", "明日工作计划": "PROJECTS",
                  "需要协调/帮助": "section:未分类"}
    - ALL：整个报告放入该字段
    - PROJECTS：只放“按项目汇总”小节
    - section:标题：只放对应小节
    默认映射：整个报告进“今日完成工作”。
    """
    mapping = mapping or {"今日完成工作": "ALL"}
    contents: List[Dict[str, str]] = []
    for field_name, rule in mapping.items():
        value = _select(report_md, rule)
        contents.append({
            "key": field_name,
            "value": truncate(value, CONTENT_LIMIT),
        })
    return contents


def _select(report_md: str, rule: str) -> str:
    """按规则从 Markdown 中取片段。"""
    if rule == "ALL":
        return report_md
    if rule == "PROJECTS":
        return _section(report_md, "按项目汇总") or report_md
    if rule.startswith("section:"):
        return _section(report_md, rule.split(":", 1)[1]) or ""
    return report_md


def _section(md: str, heading: str) -> str:
    """提取某 `## heading` 小节内容（到下一个 ## 标题为止）。"""
    lines = md.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and heading in line:
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def get_token(appkey: str, appsecret: str, opener=None) -> str:
    """获取 access_token。opener 可注入（测试用）。"""
    url = f"{GETTOKEN_URL}?appkey={appkey}&appsecret={appsecret}"
    fetch = opener or _urlopen_json
    try:
        data = fetch(url, None, None)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise DingTalkError(f"获取钉钉 token 网络失败：{exc}") from exc
    if data.get("errcode", 0) != 0:
        raise DingTalkError(f"获取钉钉 token 失败：errcode={data.get('errcode')} {data.get('errmsg')}")
    token = data.get("access_token", "")
    if not token:
        raise DingTalkError("获取钉钉 token 失败：返回为空")
    return token


def create_report(token: str, cfg: Dict, contents: List[Dict[str, str]], opener=None) -> Dict:
    """创建工作日志。opener 可注入（测试用）。"""
    payload = {
        "template_id": cfg["template_id"],
        "userid": cfg["userid"],
        "contents": contents,
        "dd_from": "ai-report",
        "to_chat": cfg.get("to_chat", ""),
        "to_userids": cfg.get("to_userids", ""),
        "to_cids": cfg.get("to_cids", ""),
    }
    url = f"{CREATE_URL}?access_token={token}"
    fetch = opener or _urlopen_json
    try:
        data = fetch(url, payload, None)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise DingTalkError(f"提交钉钉日志网络失败：{exc}") from exc
    if data.get("errcode", 0) != 0:
        raise DingTalkError(
            f"提交钉钉日志失败：errcode={data.get('errcode')} {data.get('errmsg')}"
        )
    return data


def _urlopen_json(url: str, payload: Optional[Dict], _headers) -> Dict:
    """标准库 HTTP 请求（JSON in/out），独立成函数便于 mock。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_preview(report_md: str, cfg: Dict, contents: List[Dict[str, str]]) -> str:
    """生成 dry-run 预览文本（分段、截断后内容、目标 template_id/userid）。"""
    lines = [
        "========== 钉钉提交预览（dry-run，未发送）==========",
        f"目标 template_id : {cfg['template_id']}",
        f"目标 userid       : {cfg['userid']}",
        f"字段数            : {len(contents)}",
    ]
    for i, c in enumerate(contents, 1):
        lines.append("-" * 50)
        lines.append(f"[{i}] {c['key']}（{len(c['value'])}/{CONTENT_LIMIT} 字符"
                     f"{'，已截断' if len(c['value']) >= CONTENT_LIMIT else ''}）")
        lines.append(c["value"])
    lines.append("=" * 50)
    lines.append("确认无误后追加 --confirm 参数才会真正发送。")
    return "\n".join(lines)


def submit(report_md: str, config_path: Optional[Path] = None,
           confirm: bool = False, opener=None, input_fn=input) -> bool:
    """提交入口：默认只预览；confirm=True 且用户输入 YES 才发送。

    返回 True 表示已实际发送成功，False 表示仅预览/被取消。
    """
    appkey, appsecret = require_credentials()
    cfg = load_config(config_path)
    contents = resolve_source(report_md, cfg.get("field_mapping"))

    if not confirm:
        print(build_preview(report_md, cfg, contents))
        return False

    # 二次人工确认：即使带了 --confirm 也要再敲一次 YES（审核闸门双保险）
    answer = input_fn("确认发送到钉钉工作日志？输入 YES 确认：").strip()
    if answer != "YES":
        print("已取消，未发送。")
        return False

    token = get_token(appkey, appsecret, opener=opener)
    create_report(token, cfg, contents, opener=opener)
    log("[钉钉] 日志提交成功")
    print("已提交到钉钉工作日志。")
    return True
