"""L4-LLM 合成层：把骨架里的条目文字改写成「内容级」工作陈述（可插拔、可降级）。

谁定排版：排版由 generator.py 决定。本模块**只改写文字**——给模型的是
`{"sections": [{"name": 项目名, "items": [{"title": 条目名, "body": 当日素材}]}]}`，
要求它原结构返回；拿回结果后由 generator.check_outline 校验（项目/条目的数量、顺序、
项目名一律不许动，文字里不许残留时间戳、[图片]、URL、日志、元信息行），
校验通过才由 generator 重新渲染成 Markdown，不过就如实退回纯模板草稿。

隐私约定：
- 只把骨架文本（已是本地汇总结果）发往 LLM，绝不传原始对话
- 无 API Key 或请求失败 → 原样退回模板草稿并如实记 last_error，绝不中断流程

Key 与模型从哪来（优先级从高到低）：
1. 函数参数（`api_key=` / `model=`）
2. 环境变量：`LLM_PROVIDER` / `LLM_MODEL` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`
3. 本地配置文件 `llm.json`（`AIREREPORT_LLM_FILE` 可覆盖；默认 `<仓库根>/llm.json`
   或 `~/.aireport/llm.json`，已 gitignore），形如
   `{"provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-..."}`
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ai_report import config, generator
from ai_report.utils import log

#: 各 provider 的端点与默认模型
PROVIDERS = {
    "deepseek": {
        "url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "key_env": "OPENAI_API_KEY",
    },
    "ollama": {
        "url": "http://127.0.0.1:11434/v1/chat/completions",
        "model": "qwen2.5",
        "key_env": "",  # 本地服务无需 key
    },
}

#: 本地配置文件位置的环境变量名
LLM_FILE_ENV = "AIREREPORT_LLM_FILE"

#: 模型覆盖的环境变量名（本机 ollama 拉的模型名各不相同，必须能改）
MODEL_ENV = "LLM_MODEL"

#: 请求超时（秒），`AIREREPORT_LLM_TIMEOUT` 可覆盖；归纳长草稿比取数慢得多
TIMEOUT_ENV = "AIREREPORT_LLM_TIMEOUT"
DEFAULT_TIMEOUT = 60

SYSTEM_PROMPT = (
    "你是中文工作汇报助理。用户给你一份 JSON：它是本机工具**已经定好结构**的工作记录骨架，"
    "形如 `{\"sections\": [{\"name\": \"项目名\", \"items\": [{\"title\": \"条目名\", "
    "\"body\": \"当日原始素材\"}]}]}`。其中 `title` 往往是原始对话标题，`body` 是当天真实发生的碎片——"
    "用户的原话提问、命令行片段、日志与报错、[图片] 占位、URL，常带时间戳，"
    "同一件事被拆成很多碎片，且完全没有整理。\n"
    "你的任务**只是把这两段文字改写成「人能直接读」的工作陈述**。"
    "**把原始素材原样复述出去 = 任务失败**。\n"
    "硬性要求：\n"
    "1. 只输出一个 JSON 对象（顶层键就是 `sections`），不要任何前言、解释或 ``` 代码块围栏。\n"
    "2. **结构一个字都不能动**：sections 的个数与顺序、每个 section 的 `name`、"
    "items 的**条数与顺序**，全部原样保留。不许增删项目、不许增删条目、不许调换顺序、不许改项目名。\n"
    "3. 每个 item 的 `title` 与 `body` 都要改写。`title` 是条目名，10~20 字，说清这件事是什么；"
    "`body` 用 1~3 句专业的工作陈述，说清「做了什么、做到什么程度 / 结论是什么」。"
    "严禁出现时间戳（如 09:14）、`[图片]` 占位、命令行原文、日志或堆栈、URL、"
    "「时间：」「来源：」「状态：」这类元信息行，严禁用「；」把原始片段串起来的堆砌写法，"
    "严禁照抄情绪化口语（如「你他妈的」「不行」「丑死了」），要把诉求提炼成中性描述。\n"
    "4. 同一条 `body` 里的多个碎片属于同一件事时合并成通顺的一句话，不要按时间戳逐条摊开；"
    "纯闲聊或毫无信息量的碎片直接丢弃。\n"
    "5. 不得编造素材里没有的事实；信息实在不足时写保守的中性陈述"
    "（如「就 X 进行调试与方案讨论」），不要硬编结论。\n"
    "6. **分清「事实」与「用户的判断」**：素材里用户常把现象或猜测当结论讲"
    "（如「RabbitMQ 崩溃了」「这些文件其实是能 ls 读取的」「这个绿色的结论是错的」）。"
    "这类话只能写成「排查 X（用户判断为 Y）」「核对 Y 的说法」，"
    "**严禁改写成「已定位到 Y」「确认 Y 导致…」「验证了 Y」这类结论句**——"
    "你没做过验证，就不要写成验证结论。\n"
    "7. 某个 item 的 `body` 在输入里是空字符串时，输出里也必须留空（`\"\"`），**绝不替它编内容**。\n"
)

#: 交给模型时给骨架加的引导语（强化「改写」而非「复述」）
DRAFT_LEAD = ("以下是由本机工具拼装的原始骨架（含未整理的碎片）。"
              "请只改写每个 item 的 title / body，不要照抄、不要改结构：\n\n")


def llm_file() -> Optional[Path]:
    """本地配置文件位置：环境变量 > 仓库根 llm.json > ~/.aireport/llm.json。"""
    env = os.environ.get(LLM_FILE_ENV)
    if env:
        return Path(env)
    for candidate in (config.ROOT_DIR / "llm.json",
                      Path.home() / ".aireport" / "llm.json"):
        if candidate.is_file():
            return candidate
    return None


def _file_conf() -> Dict[str, Any]:
    """读本地配置文件；缺失/损坏时返回空表（不中断主流程）。"""
    path = llm_file()
    if not path:
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[LLM] 读取 {path} 失败，按未配置处理：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _conf_text(conf: Dict[str, Any], key: str) -> str:
    return str(conf.get(key) or "").strip()


def _parse_json(text: str) -> Optional[Any]:
    """从模型回复里抠出 JSON：容忍 ``` 围栏与前后解释性文字。"""
    s = (text or "").strip()
    if not s:
        return None
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    head, tail = s.find("{"), s.rfind("}")
    if 0 <= head < tail:
        try:
            return json.loads(s[head:tail + 1])
        except json.JSONDecodeError:
            return None
    return None


def _slots_payload(outline: Dict[str, Any]) -> Dict[str, Any]:
    """只把槽位文字发给模型（不带标题/来源/页脚，减少可被改坏的东西）。"""
    return {
        "sections": [
            {
                "name": sec.get("name"),
                "items": [{"title": item.get("title") or "", "body": item.get("body") or ""}
                          for item in (sec.get("items") or [])],
            }
            for sec in (outline.get("sections") or [])
        ]
    }


class ReportSynthesizer:
    """条目改写器。provider 由 LLM_PROVIDER 选择（默认 deepseek），模型可用 LLM_MODEL 覆盖。"""

    def __init__(self, provider: Optional[str] = None, api_key: Optional[str] = None,
                 model: Optional[str] = None, timeout: Optional[int] = None) -> None:
        conf = _file_conf()
        self.provider = (provider or os.environ.get("LLM_PROVIDER")
                         or _conf_text(conf, "provider") or "deepseek").lower()
        if self.provider not in PROVIDERS:
            log(f"[LLM] 未知 provider {self.provider!r}，回退 deepseek")
            self.provider = "deepseek"
        self.conf = PROVIDERS[self.provider]
        self.model = (model or os.environ.get(MODEL_ENV)
                      or _conf_text(conf, "model") or self.conf["model"])
        key_env = self.conf["key_env"]
        if api_key is not None:
            self.api_key = api_key          # 显式传入（含空串 = 明确不配 key）
        else:
            self.api_key = ((os.environ.get(key_env, "") if key_env else "")
                            or _conf_text(conf, "api_key"))
        self.timeout = timeout or int(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT)
        #: 最近一次调用为什么没归纳成（空串 = 没失败）；引导页据此如实告知用户
        self.last_error = ""

    @property
    def missing(self) -> str:
        """不具备调用条件时的原因说明；可用时返回空串。"""
        if self.conf["key_env"] and not self.api_key:
            return f"未配置 {self.conf['key_env']}"
        return ""

    @property
    def available(self) -> bool:
        """是否具备调用条件（ollama 本地无需 key）。"""
        return not self.missing

    def rewrite(self, outline: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """把骨架里的文字改写成工作陈述；返回结构相同的骨架，失败返回 None。

        任何失败（无 key / 网络异常 / 非 JSON 回复）都只记 last_error，绝不抛异常。
        """
        self.last_error = ""
        if not (isinstance(outline, dict) and (outline.get("sections") or [])):
            return None                      # 空报告没有可改写的文字
        if not self.available:
            self.last_error = self.missing
            log(f"[LLM] {self.missing}，已退回模板模式")
            return None

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": DRAFT_LEAD + json.dumps(
                    _slots_payload(outline), ensure_ascii=False, indent=2)},
            ],
            "temperature": 0.3,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            req = urllib.request.Request(
                self.conf["url"],
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            if not content or not str(content).strip():
                raise ValueError("LLM 返回内容为空")
        except (urllib.error.URLError, ValueError, KeyError, IndexError,
                json.JSONDecodeError, OSError, TimeoutError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log(f"[LLM] 归纳失败（{exc}），已退回模板模式")
            return None

        data = _parse_json(str(content))
        if not isinstance(data, dict):
            self.last_error = "模型没有返回可解析的 JSON"
            log(f"[LLM] {self.last_error}，已退回模板模式")
            return None
        return data


def _merge_text(reference: Dict[str, Any], written: Dict[str, Any]) -> Dict[str, Any]:
    """把归纳后的文字填回本机骨架：标题/来源/项目名/条目位置一律用骨架的。

    模型只返回 `sections`（它看不到、也不该改报告标题与来源行），所以这里必须
    以骨架为底稿逐条贴文字——否则渲染出来会丢掉 `# 报告标题` 和来源行。
    """
    merged = json.loads(json.dumps(reference, ensure_ascii=False))   # 深拷贝，不污染骨架
    sections = merged.get("sections") or []
    new_sections = written.get("sections") or []
    for ref_sec, new_sec in zip(sections, new_sections):
        if not isinstance(new_sec, dict):
            continue
        for ref_item, new_item in zip(ref_sec.get("items") or [], new_sec.get("items") or []):
            if not isinstance(new_item, dict):
                continue
            title = str(new_item.get("title") or "").strip()
            if title:
                ref_item["title"] = title
            ref_item["body"] = str(new_item.get("body") or "").strip()
    return merged


def polish_outline(outline: Dict[str, Any], style: Optional[Dict] = None,
                   synth: Optional[ReportSynthesizer] = None) -> Tuple[str, str]:
    """程序强制排版：渲染草稿 →（可选）AI 只改写文字 → 校验 → 由程序重新排版。

    synth 为 None 时就是纯模板路径。返回 (markdown, 失败原因)；成功或未启用归纳时原因为空串。
    校验不过一律退回草稿并如实记原因，绝不把跑偏的排版交给用户。
    """
    draft = generator.render_outline(outline, style)
    if synth is None or not (outline.get("sections") or []):
        return draft, ""
    written = synth.rewrite(outline)
    if written is None:
        return draft, synth.last_error or "归纳器没有返回可用结果"
    problems = generator.check_outline(written, outline)
    if problems:
        synth.last_error = "归纳结果未通过格式校验，已退回模板排版：" + "；".join(problems)
        log(f"[LLM] {synth.last_error}")
        return draft, synth.last_error
    # 只把「文字」填回本机骨架，排版仍由 generator 决定
    return generator.render_outline(_merge_text(outline, written), style), ""


def status() -> Dict[str, Any]:
    """当前归纳能力概览（供引导页如实展示：用什么 provider / 模型 / 是否已配置 key）。"""
    synth = ReportSynthesizer()
    return {
        "provider": synth.provider,
        "model": synth.model,
        "ready": synth.available,
        "missing": synth.missing,
        "config_file": str(llm_file() or ""),
    }


__all__ = ["ReportSynthesizer", "polish_outline", "status", "PROVIDERS",
           "SYSTEM_PROMPT", "DRAFT_LEAD", "llm_file"]
