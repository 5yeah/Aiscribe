"""Markdown → Word(.docx) 转换器（仅标准库）。

.docx 本质是 zip 包内的 WordprocessingML，这里手写最小合法结构：
[Content_Types].xml + _rels/.rels + word/document.xml（+ 可选 word/styles.xml）
支持的 Markdown 子集（报告生成器实际用到的）：
  # / ## / ### 标题、- 无序列表、--- 分隔线、普通段落
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import List, Tuple
from xml.sax.saxutils import escape

# 内嵌样式名（保证标题/列表在 Word 中正确显示）
_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>
<w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:pPr><w:outlineLvl w:val="0"/><w:spacing w:before="240" w:after="120"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:pPr><w:outlineLvl w:val="1"/><w:spacing w:before="200" w:after="100"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
<w:pPr><w:outlineLvl w:val="2"/><w:spacing w:before="160" w:after="80"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph">
<w:pPr><w:ind w:left="425"/><w:spacing w:after="60"/></w:pPr></w:style>
</w:styles>
"""

# 报告内容只含中文/英文/常见符号，统一转义后即可安全嵌入 XML
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_LIST_RE = re.compile(r"^[-*]\s+(.*)$")
_HR = re.compile(r"^-{3,}$")


def _para(text: str, style: str = "", bold_prefix: str = "") -> str:
    """生成一个段落；style 为样式 ID，bold_prefix 用于行首加粗片段。"""
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    runs = ""
    if bold_prefix and text.startswith(bold_prefix):
        runs += f'<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">' \
                f"{escape(bold_prefix)}</w:t></w:r>"
        text = text[len(bold_prefix):]
    runs += f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>' if text else ""
    return f"<w:p><w:pPr>{style_xml}</w:pPr>{runs}</w:p>"


def _empty_para() -> str:
    return "<w:p/>"


def markdown_to_body_xml(md: str) -> str:
    """把 Markdown 文本转成 document.xml 的 body 片段。"""
    paras: List[str] = []
    for raw_line in (md or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            paras.append(_empty_para())
            continue
        if _HR.match(line.strip()):
            paras.append(
                '<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6"'
                ' w:space="1" w:color="999999"/></w:pBdr></w:pPr></w:p>'
            )
            continue
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            # 首个 # 用 Title 样式，其余按级别
            style = "Title" if level == 1 else f"Heading{level - 1}"
            paras.append(_para(text, style))
            continue
        m = _LIST_RE.match(line)
        if m:
            text = m.group(1)
            # 报告条目形如 "[已完成] 标题（来源，时间）"，行首状态加粗
            bold = ""
            bm = re.match(r"^\[[^\]]+\]\s", text)
            if bm:
                bold = bm.group(0)
            paras.append(_para("• " + text, "ListParagraph", bold_prefix="• " + bold if bold else "• "))
            continue
        paras.append(_para(line))
    return "".join(paras)


def write_docx(md: str, out_path: Path) -> Path:
    """把 Markdown 报告写成 .docx 文件，返回路径。"""
    body = markdown_to_body_xml(md)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        "</w:sectPr></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type='
        '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
        ' Target="word/document.xml"/></Relationships>'
    )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type='
        '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"'
        ' Target="styles.xml"/></Relationships>'
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/styles.xml", _STYLES_XML)
    return out_path


def read_docx_text(path: Path) -> str:
    """读回 docx 的纯文本（测试与校验用）。"""
    import xml.etree.ElementTree as ET

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path, "r") as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    lines = []
    for p in root.iter(f"{{{ns['w']}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{ns['w']}}}t"))
        lines.append(text)
    return "\n".join(lines)
