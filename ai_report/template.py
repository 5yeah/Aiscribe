"""格式克隆：从你过去在用的报告样例里抽取「排版结构」，固化成可复用的格式规范。

边界（重要）：
- 只抽结构：标题行 / 头部信息行 / 项目行 / 事项行 / 待办段 / 收尾注 / 是否双版本，
  **不抽取也不保留样例里的具体工作内容**
- 产物是 template.json + 一段给 AI 读的规范文本；报告正文仍由 AI 归纳，只是照着你的格式写
- 图片样例本机无法解析（不引入 OCR 依赖）：只存档，并把「请 AI 读图后回填」的指令交给宿主 AI，
  由它通过 MCP 的 save_template 回填；文本与 docx 样例可直接本机推断

存储位置：环境变量 AIREREPORT_TEMPLATES_DIR > <数据目录>/templates
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ai_report import config
from ai_report.utils import log, now_bj

# ---- 存储 ----

TEMPLATES_ENV = "AIREREPORT_TEMPLATES_DIR"
SAMPLES_DIR_NAME = "samples"

#: 可本机直接解析文本的样例后缀；图片只能存档后交给 AI 读
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

# ---- 识别用的正则 ----

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_DATE_PATTERNS = (
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}\s*[~～–—-]\s*\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"),
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{4}/\d{1,2}/\d{1,2}"),
)
_TITLE_RE = re.compile(r"^#{1,4}\s+(.+?)\s*$")
_HR_RE = re.compile(r"^\s*-{3,}\s*$")
_BOLD_NUM_RE = re.compile(
    r"^\s*\*\*\s*(\d+)\s*([、.．])\s*([^*：:（(]+?)\s*"
    r"(?:[（(][^）)]*[）)])?\s*([：:]?)\s*\*\*\s*$")
_PLAIN_NUM_RE = re.compile(
    r"^\s*(\d+)\s*([、.．])\s*([^：:（(]+?)\s*"
    r"(?:[（(][^）)]*[）)])?\s*([：:]?)\s*$")
_HASH_NUM_RE = re.compile(r"^\s*#{1,4}\s*(\d+|[一二三四五六七八九十]+)\s*([、.．])\s*(.+?)\s*$")
_CIRCLED_RE = re.compile(r"^\s*([①-⑳])\s*[、.．]?\s*(.+?)\s*$")
_DASH_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_NUM_ITEM_RE = re.compile(r"^\s*(\d+)\s*[、.．]\s+(.+?)\s*$")
_HASH3_ITEM_RE = re.compile(r"^\s*#{3,4}\s*(?:\d+[、.．]\s*)?(.+?)\s*$")
_TODO_RE = re.compile(r"(待办|下一步|后续|计划)")
_FOOTER_RE = re.compile(r"^\s*([*_])(?!\1)(.+?)\1\s*$")
_HEAD_KEYS = (("汇报人", "汇报人"), ("姓名", "汇报人"), ("作者", "汇报人"),
              ("部门", "部门"), ("日期", "日期"), ("时间", "日期"))


class TemplateError(ValueError):
    """样例无法解析或模板文件损坏时抛出（调用方转成用户可读提示）。"""


def templates_dir() -> Path:
    """模板存储目录（环境变量可覆盖），不存在则创建。"""
    env = os.environ.get(TEMPLATES_ENV)
    base = Path(env) if env else (config.data_dir() / "templates")
    base.mkdir(parents=True, exist_ok=True)
    return base


def samples_dir() -> Path:
    """图片等原始样例的存档目录。"""
    d = templates_dir() / SAMPLES_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def slug(name: str) -> str:
    """模板文件名：中英文都保留，只清掉路径与不安全字符。"""
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "-", (name or "").strip()).strip("-.")
    return cleaned or "template"


# ---- 单份样例的特征抽取 ----


def _norm(text: str) -> str:
    """把具体日期换成占位符，便于跨样例比对同一套格式。"""
    out = text
    for pat in _DATE_PATTERNS:
        out = pat.sub("{date}", out)
    return out


def _detect_title(lines: List[str]) -> Optional[str]:
    """第一处标题行（# ~ ####），作为报告标题样式。"""
    for ln in lines:
        m = _TITLE_RE.match(ln)
        if m:
            return _norm(m.group(1))
    return None


def _detect_meta(lines: List[str]) -> Optional[str]:
    """头部信息行：含汇报人/部门/日期等字段的那一行，值替换为占位符。"""
    for ln in lines:
        if "｜" not in ln and "|" not in ln:
            continue
        if not any(k in ln for k in ("汇报人", "姓名", "作者", "部门", "日期")):
            continue
        out = _norm(ln.strip())
        for key, placeholder in _HEAD_KEYS:
            out = re.sub(rf"({key})(\s*[：:]\s*\*\*|\*\*\s*[：:]\s*)([^｜|*]+)",
                         rf"\1\2{{{placeholder}}}", out)
        return out
    return None


def _detect_project_heading(lines: List[str]) -> Optional[Dict[str, Any]]:
    """项目行样式：按出现次数取多数，保留一个归一化示例。"""
    votes: Counter = Counter()
    for ln in lines:
        s = ln.strip()
        m = _BOLD_NUM_RE.match(s)
        if m:
            votes[("bold-arabic", m.group(2))] += 1
            continue
        m = _HASH_NUM_RE.match(s)
        if m:
            votes[("hash", m.group(2))] += 1
            continue
        # 有空格分隔的「1. 事项」是列表项，不是项目行，不参与项目行投票
        if _PLAIN_NUM_RE.match(s) and not _NUM_ITEM_RE.match(s) and not _HR_RE.match(s):
            votes[("plain-arabic", _PLAIN_NUM_RE.match(s).group(2))] += 1
            continue
        # 只有项目名加粗、没有编号的写法
        m = re.match(r"^\s*\*\*\s*([^*]{2,40}?)\s*([：:]?)\s*\*\*\s*$", s)
        if m:
            votes[("bold-name-only", "：")] += 1
    if not votes:
        return None
    (kind, punct), count = votes.most_common(1)[0]
    return {"kind": kind, "punct": punct, "count": count}


def _detect_item_style(blocks: List[List[str]]) -> Optional[Dict[str, Any]]:
    """事项行样式：在项目段内统计行首标记，并按「胜出样式」判断冒号后是否带说明。"""
    votes: Counter = Counter()
    picked: List[Tuple[str, str]] = []
    for block in blocks:
        for ln in block:
            s = ln.strip()
            if not s:
                continue
            m = _CIRCLED_RE.match(s)
            if m:
                votes["circled"] += 1
                picked.append(("circled", m.group(2)))
                continue
            m = _DASH_RE.match(s)
            if m and not _HR_RE.match(s):
                votes["dash"] += 1
                picked.append(("dash", m.group(1)))
                continue
            m = _NUM_ITEM_RE.match(s)
            if m:
                votes["numeric"] += 1
                picked.append(("numeric", m.group(2)))
                continue
            m = _HASH3_ITEM_RE.match(s)
            if m:
                votes["hash3"] += 1
                picked.append(("hash3", m.group(1)))
    if not votes:
        return None
    style, count = votes.most_common(1)[0]
    # 只统计胜出样式自己的行：说明句占比 ≥30% 才算「有说明」
    texts = [t for st, t in picked if st == style]
    with_detail = sum(1 for t in texts if re.search(r"[：:]", t))
    has_detail = bool(texts) and with_detail * 10 >= len(texts) * 3
    return {"style": style, "count": count, "has_detail": has_detail}


def _project_blocks(lines: List[str]) -> List[List[str]]:
    """把行按项目行切块，供事项样式与段落结构统计。"""
    blocks: List[List[str]] = []
    current: Optional[List[str]] = None
    for ln in lines:
        s = ln.strip()
        is_project = bool(
            _BOLD_NUM_RE.match(s)
            or _HASH_NUM_RE.match(s)
            or (_PLAIN_NUM_RE.match(s) and not _NUM_ITEM_RE.match(s)
                and not _HR_RE.match(s))
        )
        if is_project:
            current = []
            blocks.append(current)
        elif current is not None:
            current.append(ln)
    return blocks


def _detect_short_version(lines: List[str], title: Optional[str]) -> bool:
    """是否「详细版 + 简洁版」双版本：分隔线之后又出现标题或项目行。"""
    seen_hr = False
    for ln in lines:
        s = ln.strip()
        if _HR_RE.match(s):
            seen_hr = True
            continue
        if seen_hr and (_TITLE_RE.match(s) or _BOLD_NUM_RE.match(s)):
            return True
    return False


def _detect_todo(lines: List[str]) -> Optional[str]:
    """待办段标题：捕获含「待办/下一步/计划」的标题行，编号与内容换成占位符。"""
    for ln in lines:
        s = ln.strip()
        if not _TODO_RE.search(s):
            continue
        stripped = re.sub(r"[*#]+", "", s).strip()
        stripped = re.sub(r"^\d+\s*[、.．]\s*", "{n}、", stripped)
        stripped = re.sub(r"\s*[（(].*?[)）]\s*$", "", stripped).strip()
        if 2 <= len(stripped) <= 20:
            return stripped
    return None


def _detect_footer(lines: List[str]) -> Optional[str]:
    """收尾注：单独成行的斜体说明（`*……*`，区别于整行加粗的项目行）。"""
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith(("**", "__")):
            continue
        m = _FOOTER_RE.match(s)
        if m and 4 <= len(m.group(2)) <= 90:
            return "*{note}*"
    return None


def extract_features(text: str) -> Dict[str, Any]:
    """从一份样例文本抽出结构特征（单份样例的独立判断）。"""
    lines = (text or "").splitlines()
    blocks = _project_blocks(lines)
    return {
        "title": _detect_title(lines),
        "meta": _detect_meta(lines),
        "project": _detect_project_heading(lines),
        "item": _detect_item_style(blocks),
        "short_version": _detect_short_version(lines, _detect_title(lines)),
        "todo": _detect_todo(lines),
        "footer": _detect_footer(lines),
        "chars": len(text or ""),
    }


# ---- 多份样例合并（投票） ----


def _vote(values: Iterable[Any]) -> Tuple[Optional[Any], int, int]:
    """对可哈希特征投票，返回（多数值, 多数票数, 有效样本数）。"""
    counter: Counter = Counter()
    for v in values:
        if v is None:
            continue
        counter[json.dumps(v, ensure_ascii=False, sort_keys=True)] += 1
    total = sum(counter.values())
    if not total:
        return None, 0, 0
    key, count = counter.most_common(1)[0]
    return json.loads(key), count, total


def merge_features(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把多份样例的特征按多数票合并成一份模板特征。"""
    merged: Dict[str, Any] = {}
    confidence: Dict[str, Any] = {}
    for field in ("title", "meta", "project", "item", "todo", "footer"):
        value, count, total = _vote([f.get(field) for f in features])
        if value is not None:
            merged[field] = value
            confidence[field] = {"agree": count, "total": total}
    # 布尔特征：多数为真即采纳
    for field in ("short_version",):
        hits = sum(1 for f in features if f.get(field))
        merged[field] = hits * 2 >= max(1, len(features))
        confidence[field] = {"agree": hits, "total": len(features)}
    merged["title_has_hash"] = bool(merged.get("title"))
    return merged


# ---- 规范文本（给 AI 读） ----


def _project_line_spec(project: Optional[Dict[str, Any]]) -> Optional[str]:
    if not project:
        return None
    kind, punct = project.get("kind"), project.get("punct", "、")
    name = "{项目名}"
    if kind == "bold-arabic":
        return f"`**{{n}}{punct}{name}：**`（整行加粗）"
    if kind == "bold-name-only":
        return f"`**{name}：**`（项目名加粗、不带序号）"
    if kind == "hash":
        return f"`## {{n}}{punct}{name}`"
    return f"`{{n}}{punct}{name}：`"


def _item_line_spec(item: Optional[Dict[str, Any]]) -> Optional[str]:
    if not item:
        return None
    style = item.get("style")
    if style == "circled":
        base = "`{①}、{事项名}`"
    elif style == "numeric":
        base = "`{i}. {事项名}`"
    elif style == "dash":
        base = "`- {事项名}`"
    elif style == "hash3":
        base = "`### {i}. {事项名}`"
    else:
        base = "`{事项名}`"
    if item.get("has_detail"):
        base = base[:-1] + "：{说明}`" if base.endswith("`") else base
    return base


def spec_block(tpl: Dict[str, Any]) -> str:
    """把模板特征渲染成一段可直接塞进 AI 提示的格式规范。"""
    feat = tpl.get("features") or {}
    lines = [f"【输出格式规范（学自样例模板「{tpl.get('name', '未命名')}」"
             f"，共 {len(tpl.get('samples') or [])} 份样例）】"]
    n = 0
    if feat.get("title"):
        n += 1
        lines.append(f"{n}) 首行标题：`{feat['title']}`")
    if feat.get("meta"):
        n += 1
        lines.append(f"{n}) 头部信息行：`{feat['meta']}`（{'{name}'} 等占位符由你按实际情况填写）")
    proj = _project_line_spec(feat.get("project"))
    if proj:
        n += 1
        sep = feat.get("item", {}).get("has_detail") if isinstance(feat.get("item"), dict) else False
        note = "；`{项目名}` 后可跟括号补充（如时间段），简洁版一律去掉括号" if sep else ""
        lines.append(f"{n}) 项目行：{proj}{note}")
    item = _item_line_spec(feat.get("item"))
    if item:
        n += 1
        if isinstance(feat.get("item"), dict) and feat["item"].get("has_detail"):
            lines.append(f"{n}) 事项行：{item} —— 简洁版只保留标记与事项名，"
                         "删掉分隔符及之后的说明")
        else:
            lines.append(f"{n}) 事项行：{item} —— 一行一件事项，不追加说明句")
    if feat.get("todo"):
        n += 1
        lines.append(f"{n}) 待办事项独立成段，标题形如 `{feat['todo']}`")
    if feat.get("short_version"):
        n += 1
        lines.append(f"{n}) 报告分「详细版 + 简洁版」两版，两版之间用 `---` 分隔，"
                     "简洁版标题在原标题上加「（简洁版）」")
    if feat.get("footer"):
        n += 1
        lines.append(f"{n}) 收尾一行斜体说明，形如 `{feat['footer']}`")
    n += 1
    lines.append(f"{n}) 每条事项下**不要**写「时间：」「来源：」「状态：」之类的元信息行"
                 "（用户 2026-10-18 明确要求）：报告要简洁，来源只在头部统一标注一行")
    lines.append("（未列出的细节按样例的行文习惯处理；**不要把样例里的具体工作内容"
                 "照搬进来**，只复现它的结构与语气。）")
    return "\n".join(lines)


# ---- 学习、存取 ----


def _read_sample(path: Path) -> Tuple[str, str]:
    """读一份样例，返回 (kind, text)；图片返回空文本（交给宿主 AI 读）。"""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        from ai_report.docx import read_docx_text
        try:
            return "docx", read_docx_text(path)
        except Exception as exc:  # 损坏的 docx 不该中断学习
            log(f"[模板] 读取 docx 失败，跳过：{path}（{exc}）")
            return "docx", ""
    if suffix in TEXT_SUFFIXES:
        try:
            return "text", path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log(f"[模板] 读取样例失败，跳过：{path}（{exc}）")
            return "text", ""
    if suffix in IMAGE_SUFFIXES:
        return "image", ""
    raise TemplateError(f"不支持的样例格式：{path.name}"
                        f"（支持 md / txt / docx / 图片）")


def learn_from_paths(paths: Iterable[Path], name: str) -> Dict[str, Any]:
    """从样例文件学习格式，返回模板数据（不落盘）。"""
    features: List[Dict[str, Any]] = []
    samples: List[Dict[str, Any]] = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            raise TemplateError(f"样例文件不存在：{path}")
        kind, text = _read_sample(path)
        samples.append({"file": path.name, "kind": kind, "chars": len(text)})
        if text.strip():
            features.append(extract_features(text))
    if not features:
        raise TemplateError(
            "这些样例里没有可直接解析的文本：图片样例需要交给 AI 读图后回填"
            "（详见 /api/learn 返回的 image_hint）")
    tpl = {
        "name": name or "我的模板",
        "created": now_bj().strftime("%Y-%m-%d %H:%M"),
        "samples": samples,
        "features": merge_features(features),
    }
    tpl["spec"] = spec_block(tpl)
    return tpl


def save_template(tpl: Dict[str, Any]) -> Path:
    """把模板写到 templates/<slug>.json，返回路径。"""
    path = templates_dir() / f"{slug(tpl.get('name', ''))}.json"
    tpl = dict(tpl)
    tpl.setdefault("name", path.stem)
    if "spec" not in tpl or not tpl["spec"]:
        tpl["spec"] = spec_block(tpl)
    path.write_text(json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[模板] 已保存：{path}")
    return path


def load_template(name: str) -> Dict[str, Any]:
    """按名字读取模板；文件缺失或损坏时抛 TemplateError。"""
    path = templates_dir() / f"{slug(name)}.json"
    if not path.is_file():
        raise TemplateError(f"模板不存在：{name}")
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateError(f"模板文件损坏：{path.name}（{exc}）") from exc
    if not isinstance(data, dict):
        raise TemplateError(f"模板文件格式不对：{path.name}")
    data.setdefault("spec", spec_block(data))
    return data


def list_templates() -> List[Dict[str, Any]]:
    """列出已学模板（按最近修改排序，最新在前）。"""
    out: List[Dict[str, Any]] = []
    for path in sorted(templates_dir().glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        samples = data.get("samples") or []
        out.append({
            "name": data.get("name") or path.stem,
            "created": data.get("created", ""),
            "sample_count": len(samples),
            "has_image": any(s.get("kind") == "image" for s in samples),
            "pending_vision": not data.get("features"),
            "path": str(path),
        })
    return out


def resolve_template(name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """取生效模板：显式名字 > 环境变量 AIREREPORT_TEMPLATE > 最新一份；都没有则 None。"""
    if not name:
        name = os.environ.get("AIREREPORT_TEMPLATE")
    if name:
        try:
            return load_template(name)
        except TemplateError as exc:
            log(f"[模板] {exc}")
            return None
    items = list_templates()
    for item in items:
        try:
            return load_template(item["name"])
        except TemplateError:
            continue
    return None


def style_for_generator(tpl: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """把模板特征压成 generator 需要的最小样式（无模板时返回 None，保持原格式）。"""
    if not tpl:
        return None
    feat = tpl.get("features") or {}
    project = feat.get("project") or {}
    item = feat.get("item") or {}
    if not project and not item:
        return None
    return {
        "project_kind": project.get("kind"),
        "project_punct": project.get("punct", "、"),
        "item_style": item.get("style"),
        "item_has_detail": bool(item.get("has_detail")),
        #: 模板要求「详细版 + 简洁版」两版时，generator 会追加简洁版
        "short_version": bool(feat.get("short_version")),
    }


def stub_for_vision(name: str, sample: str) -> Dict[str, Any]:
    """图片样例的待回填模板骨架（AI 读图后由 save_template 补齐 features）。"""
    return {
        "name": name,
        "created": now_bj().strftime("%Y-%m-%d %H:%M"),
        "samples": [{"file": sample, "kind": "image", "chars": 0}],
        "features": {},
        "spec": "",
        "pending_vision": True,
    }


def image_hint(tpl: Dict[str, Any]) -> Optional[str]:
    """图片样例的读图提示：本机不引 OCR（保持零依赖），改由宿主 AI 读图后回填。"""
    imgs = [s for s in (tpl.get("samples") or []) if s.get("kind") == "image"]
    if not imgs:
        return None
    paths = []
    for s in imgs:
        p = s.get("path")
        if not p:
            candidate = samples_dir() / (s.get("file") or "")
            p = str(candidate) if candidate.is_file() else (s.get("file") or "")
        paths.append(p)
    text = [f"样例里含 {len(imgs)} 张图片——本机不做识图（保持零依赖）。"
            "请先用读图能力看清这些图片的排版，再据此校准格式规范："]
    text += [f"  - {p}" for p in paths]
    return "\n".join(text)
