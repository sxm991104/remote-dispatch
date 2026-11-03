"""把论文 markdown 转成 Word + PDF。

## 为什么自己写转换器，不用 pandoc

    这台机器上没有 pandoc / LibreOffice / Word。
    只有 WPS 和 Python 的 reportlab / python-docx。

    论文结构简单（标题 + 段落 + 表格 + 代码块 + 引用块），
    自己按行解析就够了，比装一整套工具链快。

## 输出

    docs/paper-agent-security.docx     Word，别人直接能打开
    docs/paper-agent-security.pdf      PDF，最通用的分发格式

## ⚠️ 中文

    docx 依赖 Word/WPS 打开时的字体，所以指定"微软雅黑"。
    PDF 要自己嵌字体，用系统自带的 msyh.ttc。
"""

from __future__ import annotations

import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"D:\Project\dsh-bridge-oss"
SRC = os.path.join(ROOT, "docs", "paper-agent-security.md")
OUT_DOCX = os.path.join(ROOT, "docs", "paper-agent-security.docx")
OUT_PDF = os.path.join(ROOT, "docs", "paper-agent-security.pdf")


# ══════════════════════════════════════════════════════
# 一、解析 markdown
# ══════════════════════════════════════════════════════

def parse(md: str) -> list[dict]:
    """把 markdown 拆成块。

    块类型：
        h1 / h2 / h3     标题
        p                段落
        li               列表项（无序）
        oli              列表项（有序）
        code             代码块
        quote            引用块
        table            表格（rows 是二维列表）
        hr               分隔线
    """
    lines = md.splitlines()
    blocks: list[dict] = []
    i = 0

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()

        # 空行
        if not s:
            i += 1
            continue

        # 代码块
        if s.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append({"t": "code", "text": "\n".join(buf)})
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            level = len(m.group(1))
            blocks.append({"t": f"h{min(level, 3)}", "text": m.group(2).strip()})
            i += 1
            continue

        # 分隔线
        if re.match(r"^-{3,}$", s) or re.match(r"^\*{3,}$", s):
            blocks.append({"t": "hr"})
            i += 1
            continue

        # 表格
        if s.startswith("|") and i + 1 < len(lines) and re.match(
            r"^\|[\s\-:|]+\|$", lines[i + 1].strip()
        ):
            rows = []
            header = [c.strip() for c in s.strip("|").split("|")]
            rows.append(header)
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(row)
                i += 1
            blocks.append({"t": "table", "rows": rows})
            continue

        # 引用
        if s.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"t": "quote", "text": " ".join(buf)})
            continue

        # 列表
        m = re.match(r"^[-*]\s+(.*)$", s)
        if m:
            blocks.append({"t": "li", "text": m.group(1).strip()})
            i += 1
            continue

        m = re.match(r"^(\d+)\.\s+(.*)$", s)
        if m:
            blocks.append({"t": "oli", "text": m.group(2).strip(),
                           "n": int(m.group(1))})
            i += 1
            continue

        # 段落（合并连续行）
        buf = []
        while i < len(lines):
            cur = lines[i]
            cs = cur.strip()
            if not cs:
                break
            if (cs.startswith(("#", ">", "|", "```"))
                    or re.match(r"^[-*]\s+", cs)
                    or re.match(r"^\d+\.\s+", cs)
                    or re.match(r"^-{3,}$", cs)):
                break
            buf.append(cs)
            i += 1
        if buf:
            blocks.append({"t": "p", "text": "".join(buf)})

    return blocks


def strip_md(text: str) -> str:
    """去掉 markdown 行内标记，留纯文字。"""
    t = text
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", t)
    t = re.sub(r"`(.+?)`", r"\1", t)
    t = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", t)
    return t.strip()


def segs(text: str) -> list[tuple[str, bool]]:
    """把一行拆成 [(文字, 是否加粗)]，用来保留粗体。"""
    out = []
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*", text):
        if m.start() > pos:
            out.append((strip_md(text[pos:m.start()]), False))
        out.append((strip_md(m.group(1)), True))
        pos = m.end()
    if pos < len(text):
        out.append((strip_md(text[pos:]), False))
    return [x for x in out if x[0]]


# ══════════════════════════════════════════════════════
# 二、生成 docx
# ══════════════════════════════════════════════════════

def make_docx(blocks: list[dict], out: str) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    doc = Document()

    # 正文默认字体
    style = doc.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    def set_font(run, size=10.5, bold=False, color=None):
        run.font.name = "微软雅黑"
        run.font.size = Pt(size)
        run.bold = bold
        if color:
            run.font.color.rgb = color
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    for b in blocks:
        t = b["t"]

        if t == "h1":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            set_font(p.add_run(strip_md(b["text"])), size=18, bold=True)
            p.paragraph_format.space_after = Pt(10)

        elif t == "h2":
            p = doc.add_paragraph()
            set_font(p.add_run(strip_md(b["text"])), size=14, bold=True)
            p.paragraph_format.space_before = Pt(12)
            p.paragraph_format.space_after = Pt(6)

        elif t == "h3":
            p = doc.add_paragraph()
            set_font(p.add_run(strip_md(b["text"])), size=12, bold=True)
            p.paragraph_format.space_before = Pt(8)
            p.paragraph_format.space_after = Pt(4)

        elif t == "p":
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(6)
            for txt, bold in segs(b["text"]):
                set_font(p.add_run(txt), bold=bold)

        elif t == "li":
            p = doc.add_paragraph(style="List Bullet")
            for txt, bold in segs(b["text"]):
                set_font(p.add_run(txt), bold=bold)

        elif t == "oli":
            p = doc.add_paragraph(style="List Number")
            for txt, bold in segs(b["text"]):
                set_font(p.add_run(txt), bold=bold)

        elif t == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(20)
            for txt, bold in segs(b["text"]):
                set_font(p.add_run(txt), size=10, bold=bold,
                         color=RGBColor(0x55, 0x55, 0x55))

        elif t == "code":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(14)
            r = p.add_run(b["text"])
            r.font.name = "Consolas"
            r.font.size = Pt(9)
            r._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

        elif t == "table":
            rows = b["rows"]
            if not rows:
                continue
            ncol = max(len(r) for r in rows)
            tb = doc.add_table(rows=len(rows), cols=ncol)
            tb.style = "Table Grid"
            for ri, row in enumerate(rows):
                for ci in range(ncol):
                    cell = tb.cell(ri, ci)
                    cell.text = ""
                    p = cell.paragraphs[0]
                    txt = strip_md(row[ci]) if ci < len(row) else ""
                    set_font(p.add_run(txt), size=9.5, bold=(ri == 0))

        elif t == "hr":
            doc.add_paragraph()

    doc.save(out)
    print(f"  ✅ {out}")


# ══════════════════════════════════════════════════════
# 三、生成 PDF（reportlab）
# ══════════════════════════════════════════════════════

def make_pdf(blocks: list[dict], out: str) -> None:
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        KeepTogether,
        PageBreak,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    # 中文字体：微软雅黑
    font_path = None
    for cand in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ):
        if os.path.exists(cand):
            font_path = cand
            break

    if font_path is None:
        raise RuntimeError("找不到中文字体")

    if font_path.endswith(".ttc"):
        # reportlab 读 ttc 要指定 face index
        pdfmetrics.registerFont(TTFont("CJK", font_path, subfontIndex=0))
    else:
        pdfmetrics.registerFont(TTFont("CJK", font_path))

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;"))

    def rich(text: str) -> str:
        """把 **粗体** 转成 reportlab 的 <b>。"""
        out = []
        pos = 0
        for m in re.finditer(r"\*\*(.+?)\*\*", text):
            if m.start() > pos:
                out.append(esc(strip_md(text[pos:m.start()])))
            out.append(f"<b>{esc(strip_md(m.group(1)))}</b>")
            pos = m.end()
        if pos < len(text):
            out.append(esc(strip_md(text[pos:])))
        return "".join(out)

    S = {
        "h1": ParagraphStyle("h1", fontName="CJK", fontSize=17, leading=24,
                             alignment=TA_CENTER, spaceAfter=12),
        "h2": ParagraphStyle("h2", fontName="CJK", fontSize=13, leading=20,
                             spaceBefore=14, spaceAfter=7),
        "h3": ParagraphStyle("h3", fontName="CJK", fontSize=11, leading=17,
                             spaceBefore=9, spaceAfter=5),
        "p": ParagraphStyle("p", fontName="CJK", fontSize=10, leading=17,
                            spaceAfter=6, firstLineIndent=0),
        "li": ParagraphStyle("li", fontName="CJK", fontSize=10, leading=17,
                             leftIndent=14, bulletIndent=4, spaceAfter=3),
        "quote": ParagraphStyle("quote", fontName="CJK", fontSize=9.5,
                                leading=15, leftIndent=16,
                                textColor="#555555", spaceAfter=6),
    }

    doc = SimpleDocTemplate(
        out, pagesize=A4,
        leftMargin=22 * mm, rightMargin=22 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title="当说服式攻击遇上执行层隔离",
        author="忆思",
    )

    story = []
    for b in blocks:
        t = b["t"]
        if t == "h1":
            story.append(Paragraph(esc(strip_md(b["text"])), S["h1"]))
        elif t == "h2":
            story.append(Paragraph(esc(strip_md(b["text"])), S["h2"]))
        elif t == "h3":
            story.append(Paragraph(esc(strip_md(b["text"])), S["h3"]))
        elif t == "p":
            story.append(Paragraph(rich(b["text"]), S["p"]))
        elif t == "li":
            story.append(Paragraph(rich(b["text"]), S["li"], bulletText="•"))
        elif t == "oli":
            story.append(Paragraph(rich(b["text"]), S["li"],
                                   bulletText=f"{b.get('n', 1)}."))
        elif t == "quote":
            story.append(Paragraph(rich(b["text"]), S["quote"]))
        elif t == "code":
            story.append(Preformatted(
                b["text"],
                ParagraphStyle("code", fontName="CJK", fontSize=8, leading=12,
                               leftIndent=10, backColor="#f4f4f4",
                               borderPadding=4),
            ))
        elif t == "table":
            rows = [[strip_md(c) for c in r] for r in b["rows"]]
            tb = Table(rows, repeatRows=1)
            tb.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), "CJK"),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.4, "#999999"),
                ("BACKGROUND", (0, 0), (-1, 0), "#eeeeee"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(tb)
            story.append(Spacer(1, 6))
        elif t == "hr":
            story.append(Spacer(1, 8))

    doc.build(story)
    print(f"  ✅ {out}")


def main() -> int:
    if not os.path.exists(SRC):
        print(f"❌ 找不到 {SRC}")
        return 1

    with open(SRC, encoding="utf-8") as f:
        md = f.read()

    blocks = parse(md)
    print(f"  解析出 {len(blocks)} 个块")

    # 统计
    from collections import Counter

    c = Counter(b["t"] for b in blocks)
    print(f"  块类型分布: {dict(c)}")
    print()

    ok = 0
    try:
        make_docx(blocks, OUT_DOCX)
        ok += 1
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ docx: {type(e).__name__}: {e}")

    try:
        make_pdf(blocks, OUT_PDF)
        ok += 1
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ pdf: {type(e).__name__}: {e}")

    print()
    for p in (OUT_DOCX, OUT_PDF):
        if os.path.exists(p):
            print(f"  {p}")
            print(f"    {os.path.getsize(p) / 1024:.1f} KB")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
