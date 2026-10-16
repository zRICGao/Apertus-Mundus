"""Render the English report with vector diagrams. Requires reportlab.

Run from any directory: python track_2b/docs/build_technical_report.py
The Markdown source is authoritative; explicit breaks preserve six body pages.
"""
from pathlib import Path
import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, PageBreak,
    Table, TableStyle, Flowable, KeepTogether,
)

ROOT = Path(__file__).resolve().parents[1]
INK = colors.HexColor('#172F3A')
TEAL = colors.HexColor('#147D83')
MUTED = colors.HexColor('#536774')
PALE = colors.HexColor('#ECF4F3')
RULE = colors.HexColor('#CBD9DD')
AMBER = colors.HexColor('#B07527')
W, H = A4
MARGIN = 43
WIDTH = W - MARGIN * 2


def fonts():
    windows = Path('C:/Windows/Fonts')
    linux = Path('/usr/share/fonts/truetype/dejavu')
    specs = {
        'Body': ('arial.ttf', 'DejaVuSans.ttf'),
        'Bold': ('arialbd.ttf', 'DejaVuSans-Bold.ttf'),
        'Italic': ('ariali.ttf', 'DejaVuSans-Oblique.ttf'),
        'Mono': ('consola.ttf', 'DejaVuSansMono.ttf'),
    }
    for name, choices in specs.items():
        candidate = next((p for p in (windows / choices[0], linux / choices[1]) if p.exists()), None)
        if candidate is None:
            raise RuntimeError('Install Arial/Consolas or DejaVu fonts to render this report.')
        pdfmetrics.registerFont(TTFont(name, str(candidate)))
    pdfmetrics.registerFontFamily('Body', normal='Body', bold='Bold', italic='Italic', boldItalic='Bold')


fonts()
STYLES = {
    'body': ParagraphStyle('body', fontName='Body', fontSize=9.25, leading=12.55,
                           textColor=INK, spaceAfter=7.2, splitLongWords=True),
    'title': ParagraphStyle('title', fontName='Bold', fontSize=30, leading=34,
                            textColor=INK, spaceAfter=7),
    'h2': ParagraphStyle('h2', fontName='Bold', fontSize=18, leading=21.5,
                         textColor=INK, spaceAfter=11, keepWithNext=True),
    'h3': ParagraphStyle('h3', fontName='Bold', fontSize=11.1, leading=14.2,
                         textColor=TEAL, spaceBefore=7, spaceAfter=6, keepWithNext=True),
    'meta': ParagraphStyle('meta', fontName='Body', fontSize=8.1, leading=11,
                           textColor=MUTED, spaceAfter=12),
    'caption': ParagraphStyle('caption', fontName='Body', fontSize=8, leading=10.6,
                              textColor=MUTED, spaceAfter=8),
    'cell': ParagraphStyle('cell', fontName='Body', fontSize=8.05, leading=10.5,
                           textColor=INK, splitLongWords=True),
    'thead': ParagraphStyle('thead', fontName='Bold', fontSize=8, leading=10.4,
                            textColor=colors.white),
    'ref': ParagraphStyle('ref', fontName='Body', fontSize=8.05, leading=10.9,
                          textColor=INK, spaceAfter=7, splitLongWords=True),
}


def markup(text):
    text = escape(text)
    text = re.sub(r'`([^`]+)`', lambda m: '<font name="Mono" size="7.7">' + m.group(1) + '</font>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'(https?://[^\s<;]+)', lambda m: '<link href="' + m.group(1).rstrip('.') + '" color="#147D83">' + m.group(1) + '</link>', text)
    return text


class Diagram(Flowable):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.width = WIDTH
        self.height = {'architecture': 154, 'wiki': 207, 'memory': 144}[kind]

    def draw(self):
        c = self.canv

        def box(x, y, w, h, title, sub='', accent=False):
            c.setFillColor(TEAL if accent else PALE)
            c.setStrokeColor(TEAL if accent else RULE)
            c.roundRect(x, y, w, h, 5, fill=1, stroke=1)
            st = ParagraphStyle('node', fontName='Bold', fontSize=9, leading=11,
                                textColor=colors.white if accent else INK, alignment=TA_CENTER)
            content = escape(title)
            if sub:
                content += '<br/><font name="Body" size="7.7">' + escape(sub) + '</font>'
            p = Paragraph(content, st)
            _, ph = p.wrap(w-12, h)
            p.drawOn(c, x+6, y+(h-ph)/2)

        def arrow(x1, y1, x2, y2, label=''):
            c.setStrokeColor(MUTED)
            c.setFillColor(MUTED)
            c.setLineWidth(0.8)
            c.line(x1, y1, x2, y2)
            import math
            a = math.atan2(y2-y1, x2-x1)
            path = c.beginPath()
            path.moveTo(x2, y2)
            path.lineTo(x2-5*math.cos(a-.45), y2-5*math.sin(a-.45))
            path.lineTo(x2-5*math.cos(a+.45), y2-5*math.sin(a+.45))
            path.close()
            c.drawPath(path, fill=1, stroke=0)
            if label:
                c.setFont('Body', 7)
                c.drawCentredString((x1+x2)/2, (y1+y2)/2+5, label)

        if self.kind == 'architecture':
            bw = 112
            xs = [0, 132, 264, 396]
            box(xs[0], 96, bw, 47, 'Player action', 'Browser / FastAPI')
            box(xs[1], 96, bw, 47, 'Context builder', 'State + memory + evidence')
            box(xs[2], 96, bw, 47, 'Apertus', '2-4 short alternatives', True)
            box(xs[3], 96, bw, 47, 'Select + expand', 'Review, then SSE')
            for a, b in zip(xs, xs[1:]):
                arrow(a+bw, 119, b-3, 119)
            box(xs[3], 18, bw, 47, 'Validate + commit', 'Versioned, idempotent', True)
            box(xs[2], 18, bw, 47, 'SQLite world', 'State + confirmed events')
            box(xs[1], 18, bw, 47, 'Memory + Wiki', 'Maintain derived views')
            arrow(452, 96, 452, 68)
            arrow(396, 41, 379, 41)
            arrow(264, 41, 247, 41)
            arrow(188, 65, 188, 93)
            c.setFillColor(MUTED)
            c.setFont('Italic', 8)
            c.drawString(2, 43, 'Drafts are not facts.')
        elif self.kind == 'wiki':
            bw = 151
            xs = [0, 178, 356]
            box(xs[0], 147, bw, 47, 'Confirmed inputs', 'Documents / entities / events')
            box(xs[1], 147, bw, 47, 'Versioned sources', 'Revisions + evidence IDs')
            box(xs[2], 147, bw, 47, 'Wiki views', 'Projection + optional compiler')
            arrow(151, 170, 175, 170)
            arrow(329, 170, 353, 170)
            box(xs[2], 75, bw, 47, 'Scope + validity filter', 'Team / visibility / dependencies')
            box(xs[1], 75, bw, 47, 'Candidate retrieval', 'Lexical + valid dense evidence')
            box(xs[0], 75, bw, 47, 'BM25 + reranking', 'Up to 80 allowed fragments')
            arrow(432, 147, 432, 125)
            arrow(356, 98, 332, 98)
            arrow(178, 98, 154, 98)
            box(xs[0], 3, bw, 47, 'Evidence pack', 'Top 6 / 10,000 characters')
            box(xs[1], 3, bw, 47, 'Prompt assembly', 'Templates + current state', True)
            box(xs[2], 3, bw, 47, 'Apertus request', 'Propose / expand / decide', True)
            arrow(75, 75, 75, 53)
            arrow(151, 26, 175, 26)
            arrow(329, 26, 353, 26)
        elif self.kind == 'memory':
            box(0, 93, 340, 43, 'L1  |  Working memory', '10 recent records; 4,000-character context budget')
            box(0, 49, 340, 36, 'L2  |  Entity records', 'Stable identity, current typed state, scoped memories')
            box(0, 5, 340, 36, 'L3  |  Long-term evidence', 'Summaries + embeddings; original text retained')
            box(385, 38, 123, 71, 'Context builder', 'Select for this action', True)
            arrow(340, 115, 382, 94)
            arrow(340, 67, 382, 73)
            arrow(340, 23, 382, 50)


def table(lines):
    rows = [[v.strip() for v in line.strip().strip('|').split('|')] for line in lines]
    rows = [row for row in rows if not all(re.fullmatch(r':?-+:?', c) for c in row)]
    n = len(rows[0])
    first = rows[0][0]
    if n == 5:
        widths = [24, 117, 122, 120, WIDTH-383]
    elif n == 4:
        widths = ([230, 92, 92, WIDTH-414] if 'Retained' in first else
                  [226, 73, 112, WIDTH-411] if 'timing' in first else [215, 48, 65, WIDTH-328])
    else:
        widths = [90, 213, WIDTH-303] if first == 'Component' else [223, 155, WIDTH-378]
    formatted = [[Paragraph(markup(cell), STYLES['thead' if i == 0 else 'cell']) for cell in row] for i, row in enumerate(rows)]
    t = Table(formatted, colWidths=widths, repeatRows=1, hAlign='LEFT')
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), INK),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#F0F5F6'), colors.white]),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 7), ('RIGHTPADDING', (0,0), (-1,-1), 7),
        ('TOPPADDING', (0,0), (-1,-1), 7), ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LINEBELOW', (0,-1), (-1,-1), .6, RULE),
    ]))
    return t


def decorate(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(TEAL)
    canvas.rect(MARGIN, H-31, 25, 3, stroke=0, fill=1)
    canvas.setFont('Bold', 7.4)
    canvas.setFillColor(INK)
    canvas.drawString(MARGIN+33, H-31, 'APERTUS MUNDUS')
    canvas.setFont('Body', 7.1)
    canvas.setFillColor(MUTED)
    canvas.drawRightString(W-MARGIN, H-31, 'SYSTEM DESIGN & EXPERIMENTAL EVALUATION')
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(.5)
    canvas.line(MARGIN, 35, W-MARGIN, 35)
    canvas.setFont('Body', 7.2)
    canvas.drawString(MARGIN, 22, 'Track 2b  /  16 October 2026')
    canvas.drawRightString(W-MARGIN, 22, f'{doc.page:02d}  /  '+('REFERENCES' if doc.page > 6 else 'TECHNICAL REPORT'))
    canvas.restoreState()


def build():
    source = ROOT / 'technical_report.md'
    output = ROOT / 'Apertus_Mundus_Report.pdf'
    doc = BaseDocTemplate(str(output), pagesize=A4, title='Apertus Mundus - Technical Report',
                          author='Team Apertus Mundus', subject='Evidence-grounded persistent world simulation',
                          leftMargin=MARGIN, rightMargin=MARGIN, topMargin=49, bottomMargin=45)
    doc.addPageTemplates(PageTemplate(id='report', frames=[Frame(MARGIN, 45, WIDTH, H-94, leftPadding=0,
                           rightPadding=0, topPadding=0, bottomPadding=0)], onPage=decorate))
    story, paragraph, table_lines = [], [], []
    page = 1

    def flush():
        if paragraph:
            text = ' '.join(paragraph)
            style = ('ref' if page == 7 else 'meta' if text.startswith('**Technical report') else
                     'caption' if text.startswith(('**Figure', '**Table')) else 'body')
            story.append(Paragraph(markup(text), STYLES[style]))
            paragraph.clear()
        if table_lines:
            story.append(table(table_lines))
            story.append(Spacer(1, 9))
            table_lines.clear()

    for line in source.read_text(encoding='utf-8-sig').splitlines():
        if line.startswith('|'):
            if paragraph:
                flush()
            table_lines.append(line)
        elif not line.strip():
            flush()
        elif line == '<!-- pagebreak -->':
            flush()
            story.append(PageBreak())
            page += 1
        elif line.startswith('<!-- figure:'):
            flush()
            story.append(Diagram(line.split(':',1)[1].split()[0]))
            story.append(Spacer(1, 4))
        elif line.startswith('#'):
            flush()
            level = len(line)-len(line.lstrip('#'))
            style = 'title' if level == 1 else 'h2' if level == 2 else 'h3'
            story.append(Paragraph(markup(line[level:].strip()), STYLES[style]))
        else:
            paragraph.append(line)
    flush()
    doc.build(story)
    print(output)


if __name__ == '__main__':
    build()
