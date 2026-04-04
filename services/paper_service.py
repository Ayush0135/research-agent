import os
import re
import datetime
import httpx
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle, Image, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT
from reportlab.lib import colors

def markdown_to_html_tags(text: str) -> str:
    """Enhanced converter for inline markdown to ReportLab HTML-like tags."""
    # Bold
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # Italic
    text = re.sub(r'\*(.*?)\*', r'<i>\1</i>', text)
    # Inline Code
    text = re.sub(r'`(.*?)`', r'<font name="Courier" backColor="#f1f1f1">\1</font>', text)
    # Clean up links
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<link href="\2" color="blue"><u>\1</u></link>', text)
    return text

def parse_markdown_table(lines: list[str]) -> list[list[str]]:
    """Simple parser for markdown pipe tables."""
    table_data = []
    for line in lines:
        if '|' in line:
            # Split and clean cells, filtering empty outer splits
            cells = [c.strip() for c in line.split('|')]
            if len(cells) > 2:
                # Remove empty first/last if they exist (leading/trailing pipes)
                if not cells[0]: cells = cells[1:]
                if not cells[-1]: cells = cells[:-1]
                # Skip separator lines (---)
                if all(re.match(r'^[ :\-\|]+$', c) for c in cells):
                    continue
                table_data.append(cells)
    return table_data

def generate_academic_pdf(markdown_content: str, title_query: str, author_name: str = "Surefact AI Agent", output_path: str = None, reference_id: str = None) -> str:
    """
    Advanced Markdown to Academic PDF Generator.
    Features:
    - Table rendering
    - Image embedding (remote URLs)
    - Code block styling
    - Official certification branding
    """
    if not output_path:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"frontend/papers/research_{timestamp}.pdf"

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=54, leftMargin=54,
        topMargin=54, bottomMargin=54
    )

    styles = getSampleStyleSheet()
    
    # Custom Styles
    styles.add(ParagraphStyle(name='AcademicTitle', fontName='Times-Bold', fontSize=24, leading=28, alignment=TA_CENTER, spaceAfter=20, textColor=colors.HexColor("#1e293b")))
    styles.add(ParagraphStyle(name='AcademicAuthor', fontName='Times-Roman', fontSize=12, alignment=TA_CENTER, spaceAfter=5))
    styles.add(ParagraphStyle(name='AcademicVersion', fontName='Times-Italic', fontSize=10, alignment=TA_CENTER, textColor=colors.grey, spaceAfter=20))
    
    styles.add(ParagraphStyle(name='AcademicH1', fontName='Times-Bold', fontSize=18, leading=22, spaceBefore=25, spaceAfter=12, textColor=colors.HexColor("#0f172a"), borderPadding=0))
    styles.add(ParagraphStyle(name='AcademicH2', fontName='Times-Bold', fontSize=15, leading=19, spaceBefore=18, spaceAfter=10, textColor=colors.HexColor("#1e293b")))
    styles.add(ParagraphStyle(name='AcademicH3', fontName='Times-BoldItalic', fontSize=13, leading=17, spaceBefore=15, spaceAfter=8))
    
    styles.add(ParagraphStyle(name='AcademicBody', fontName='Times-Roman', fontSize=11, leading=15, alignment=TA_JUSTIFY, spaceAfter=12))
    styles.add(ParagraphStyle(name='AcademicBullet', fontName='Times-Roman', fontSize=11, leading=15, leftIndent=25, spaceAfter=6, firstLineIndent=-15))
    styles.add(ParagraphStyle(name='CodeBlock', fontName='Courier', fontSize=9, leading=12, leftIndent=20, rightIndent=20, spaceBefore=10, spaceAfter=10, backColor=colors.HexColor("#f8fafc"), borderPadding=10, borderColor=colors.grey, borderStyle='solid', borderWidth=0.5))

    Story = []

    # ── HEADER SECTIONS ──
    Story.append(Paragraph(title_query.upper(), styles['AcademicTitle']))
    Story.append(Paragraph(f"Synthesized by {author_name}", styles['AcademicAuthor']))
    if reference_id:
        Story.append(Paragraph(f"CERTIFIED DOCUMENT ID: {reference_id}", styles['AcademicVersion']))
    
    Story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e2e8f0"), spaceAfter=30))

    # ── PARSING LOGIC ──
    lines = markdown_content.split('\n')
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        
        if not line:
            idx += 1
            continue

        # 1. Handle Tables
        if line.startswith('|'):
            table_lines = []
            while idx < len(lines) and (lines[idx].strip().startswith('|') or not lines[idx].strip()):
                if lines[idx].strip():
                    table_lines.append(lines[idx].strip())
                idx += 1
            
            table_data = parse_markdown_table(table_lines)
            if table_data:
                # Wrap cell text in Paragraphs for wrapping
                formatted_data = [[Paragraph(markdown_to_html_tags(cell), styles['AcademicBody']) for cell in row] for row in table_data]
                t = Table(formatted_data, hAlign='LEFT', colWidths=[(doc.width/len(table_data[0]))]*len(table_data[0]))
                t.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Times-Bold'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                    ('VALIGN', (0,0), (-1,-1), 'TOP'),
                    ('LEFTPADDING', (0,0), (-1,-1), 8),
                    ('RIGHTPADDING', (0,0), (-1,-1), 8),
                ]))
                Story.append(t)
                Story.append(Spacer(1, 15))
            continue

        # 2. Handle Code Blocks
        if line.startswith('```'):
            idx += 1
            code_lines = []
            while idx < len(lines) and not lines[idx].strip().startswith('```'):
                code_lines.append(lines[idx])
                idx += 1
            idx += 1 # skip ending ```
            code_text = "\n".join(code_lines)
            Story.append(Paragraph(f"<pre>{code_text}</pre>", styles['CodeBlock']))
            continue

        # 3. Handle Images
        img_match = re.search(r'!\[.*?\]\((http.*?)\)', line)
        if img_match:
            img_url = img_match.group(1)
            try:
                # We try to download the image for embedding
                with httpx.Client() as client:
                    img_resp = client.get(img_url, timeout=5.0)
                    if img_resp.status_code == 200:
                        img_data = BytesIO(img_resp.content)
                        report_img = Image(img_data)
                        # Scale image to fit page width
                        aspect = report_img.imageHeight / report_img.imageWidth
                        report_img.drawWidth = doc.width * 0.8
                        report_img.drawHeight = (doc.width * 0.8) * aspect
                        Story.append(report_img)
                        Story.append(Spacer(1, 10))
            except Exception as e:
                print(f"Failed to embed image {img_url}: {e}")
            idx += 1
            continue

        # 4. Standard Markdown Elements
        tagged_content = markdown_to_html_tags(line)
        
        if line.startswith('# '):
            Story.append(Paragraph(tagged_content[2:], styles['AcademicH1']))
        elif line.startswith('## '):
            Story.append(Paragraph(tagged_content[3:], styles['AcademicH2']))
        elif line.startswith('### '):
            Story.append(Paragraph(tagged_content[4:], styles['AcademicH3']))
        elif line.startswith('- ') or line.startswith('* '):
            Story.append(Paragraph(f"• {tagged_content[2:]}", styles['AcademicBullet']))
        elif re.match(r'^\d+\.\s', line):
            # Numbered list
            Story.append(Paragraph(tagged_content, styles['AcademicBullet']))
        else:
            Story.append(Paragraph(tagged_content, styles['AcademicBody']))
        
        idx += 1

    # ── FOOTER SEAL ──
    Story.append(Spacer(1, 40))
    Story.append(HRFlowable(width="30%", thickness=0.5, color=colors.grey, hAlign='CENTER'))
    Story.append(Paragraph("END OF CERTIFIED RESEARCH RECORD", ParagraphStyle('End', alignment=TA_CENTER, fontSize=8, textColor=colors.grey)))

    doc.build(Story)
    return output_url(output_path)

def output_url(path: str) -> str:
    """Helper to convert local path to static URL."""
    return path.replace("frontend/", "/static/")
