import io
import os
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.graphics.barcode import qr
from reportlab.graphics.shapes import Drawing
from datetime import datetime

def _add_qr_to_elements(elements, order_id):
    """ Helper to add a verification QR code linking to the public portal. """
    verify_url = f"https://surefact.io/verify/{order_id}" # Placeholder base URL
    qr_code = qr.QrCodeWidget(verify_url)
    bounds = qr_code.getBounds()
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    
    d = Drawing(60, 60, transform=[60./width,0,0,60./height,0,0])
    d.add(qr_code)
    
    elements.append(Spacer(1, 20))
    elements.append(Paragraph("<b>VERIFY AUTHENTICITY</b>", ParagraphStyle('QRLabel', fontSize=7, textColor=colors.grey, alignment=1)))
    elements.append(Spacer(1, 4))
    elements.append(Table([[d]], colWidths=[60]))
    elements.append(Spacer(1, 4))
    elements.append(Paragraph(f"Verification ID: {order_id}", ParagraphStyle('QRID', fontSize=6, textColor=colors.grey, alignment=1)))

def generate_refund_receipt(data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    
    # Custom Styles
    brand_color = colors.HexColor("#00ffa3")
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=22, spaceAfter=5, textColor=brand_color)
    subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'], fontSize=10, textColor=colors.grey, spaceAfter=20)
    normal_style = styles["Normal"]
    
    elements = []

    # ── HEADER ──
    logo_path = "frontend/surefact_logo.png"
    header_data = []
    if os.path.exists(logo_path):
        img = Image(logo_path, width=32, height=32)
        header_data.append([img, Paragraph("SUREFACT COMPLIANCE", title_style)])
    else:
        header_data.append(["", Paragraph("SUREFACT COMPLIANCE", title_style)])
        
    head_table = Table(header_data, colWidths=[40, 400])
    head_table.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'MIDDLE')]))
    elements.append(head_table)
    elements.append(Paragraph("Official Transaction Reversal Certificate", subtitle_style))
    elements.append(Spacer(1, 20))

    # ── CONTENT TABLE ──
    table_data = [
        [Paragraph("<b>Status</b>", normal_style), "REFUND COMPLETED"],
        [Paragraph("<b>Account</b>", normal_style), data.get('email', 'N/A')],
        [Paragraph("<b>Order ID</b>", normal_style), data.get('transaction_id', 'N/A')],
        [Paragraph("<b>Plan Detail</b>", normal_style), data.get('plan', 'N/A').upper()],
        [Paragraph("<b>Refund Amount</b>", normal_style), f"INR {data.get('refund_amount', 0.0)}"],
        [Paragraph("<b>Date</b>", normal_style), data.get('date', datetime.now().strftime('%Y-%m-%d'))]
    ]

    t = Table(table_data, colWidths=[150, 320])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor("#f8fafc")),
        ('TEXTCOLOR', (0,0), (-1,-1), colors.HexColor("#1e293b")),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 14),
        ('TOPPADDING', (0,0), (-1,-1), 14),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#e2e8f0"))
    ]))
    elements.append(t)
    elements.append(Spacer(1, 40))

    # ── VERIFICATION ──
    _add_qr_to_elements(elements, data.get('transaction_id', 'N/A'))

    # ── FOOTER SEAL ──
    elements.append(Spacer(1, 50))
    seal_style = ParagraphStyle('Seal', fontSize=8, textColor=colors.lightgrey, alignment=1, italic=True)
    elements.append(Paragraph("● Digitally Signed and Certified by Surefact Compliance Systems ●", seal_style))

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes

def generate_payment_receipt(data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    
    brand_color = colors.HexColor("#6366f1") # Blue for payment
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=22, spaceAfter=5, textColor=brand_color)
    subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'], fontSize=10, textColor=colors.grey, spaceAfter=20)
    normal_style = styles["Normal"]

    elements = []

    # ── HEADER ──
    logo_path = "frontend/surefact_logo.png"
    header_data = []
    if os.path.exists(logo_path):
        img = Image(logo_path, width=32, height=32)
        header_data.append([img, Paragraph("SUREFACT PREMIUM", title_style)])
    else:
        header_data.append(["", Paragraph("SUREFACT PREMIUM", title_style)])
        
    head_table = Table(header_data, colWidths=[40, 400])
    head_table.setStyle(TableStyle([('VALIGN', (0,0), (-1,-1), 'MIDDLE')]))
    elements.append(head_table)
    elements.append(Paragraph("Official Service Activation Receipt", subtitle_style))
    elements.append(Spacer(1, 20))

    # ── CONTENT TABLE ──
    table_data = [
        [Paragraph("<b>Status</b>", normal_style), "PAYMENT VERIFIED"],
        [Paragraph("<b>User</b>", normal_style), data.get('email', 'N/A')],
        [Paragraph("<b>Transaction ID</b>", normal_style), data.get('transaction_id', 'N/A')],
        [Paragraph("<b>Plan Tier</b>", normal_style), data.get('plan', 'N/A').upper()],
        [Paragraph("<b>Amount Paid</b>", normal_style), f"INR {data.get('amount', 0.0)}"],
        [Paragraph("<b>Credits Issued</b>", normal_style), str(data.get('credits', 0))]
    ]

    t = Table(table_data, colWidths=[150, 320])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor("#f8fafc")),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.HexColor("#e2e8f0")),
        ('BOTTOMPADDING', (0,0), (-1,-1), 14),
        ('TOPPADDING', (0,0), (-1,-1), 14),
        ('FONTSIZE', (0,0), (-1,-1), 10),
    ]))
    elements.append(t)
    
    # ── VERIFICATION ──
    _add_qr_to_elements(elements, data.get('transaction_id', 'N/A'))

    # ── FOOTER SEAL ──
    elements.append(Spacer(1, 60))
    elements.append(Paragraph("● Certified Official Transaction Record - Surefact Compliance ●", ParagraphStyle('Seal', fontSize=8, textColor=colors.lightgrey, alignment=1)))

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes

def generate_suspension_notice(data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=20, spaceAfter=10, textColor=colors.HexColor("#ef4444"))
    normal_style = styles["Normal"]
    muted_style = ParagraphStyle('Muted', parent=normal_style, fontSize=9, textColor=colors.grey)

    elements = []

    # ── HEADER ──
    elements.append(Paragraph("SUREFACT COMPLIANCE ENFORCEMENT", ParagraphStyle('Brand', fontSize=10, textColor=colors.grey, spaceAfter=15)))
    elements.append(Paragraph("NOTICE OF ACCOUNT SUSPENSION", title_style))
    elements.append(Paragraph(f"Generated: {data.get('suspended_at', 'N/A')}", muted_style))
    elements.append(Spacer(1, 30))

    # ── DETAILS ──
    info_table = [
        [Paragraph("<b>Affected Email</b>", normal_style), data.get('email', 'N/A')],
        [Paragraph("<b>Reason Code</b>", normal_style), data.get('reason_label', 'N/A')],
        [Paragraph("<b>Duration</b>", normal_style), f"{data.get('duration_days', 3)} Days"],
        [Paragraph("<b>Scheduled Reinstatement</b>", normal_style), data.get('expires_at', 'N/A')],
    ]
    t = Table(info_table, colWidths=[150, 320])
    t.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#fee2e2")),
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor("#fff1f2")),
        ('BOTTOMPADDING', (0,0), (-1,-1), 12),
        ('FONTSIZE', (0,0), (-1,-1), 10),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 30))
    
    elements.append(Paragraph("<b>Violation Context:</b>", normal_style))
    elements.append(Spacer(1, 5))
    elements.append(Paragraph(data.get('reason_detail', 'Multiple policy violations detected.'), ParagraphStyle('Note', fontSize=10, leading=14)))
    
    elements.append(Spacer(1, 40))
    elements.append(Paragraph("<i>This document serves as an official record of platform administrative action.</i>", ParagraphStyle('Foot', fontSize=8, textColor=colors.grey, alignment=1)))

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes

def generate_appeal_decision_notice(data: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    
    title_text = "APPEAL APPROVED" if data['decision'] == "approved" else "APPEAL REJECTED"
    title_color = colors.HexColor("#10b981") if data['decision'] == "approved" else colors.HexColor("#ef4444")
    
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=20, spaceAfter=20, textColor=title_color)
    elements = []

    elements.append(Paragraph("SUREFACT COMPLIANCE REVIEW", ParagraphStyle('Brand', fontSize=10, textColor=colors.grey, spaceAfter=15)))
    elements.append(Paragraph(title_text, title_style))
    
    table_data = [
        ["Account Email:", data["email"]],
        ["Review Date:", data["decision_date"]],
        ["Final Decision:", data["decision"].upper()],
    ]
    t = Table(table_data, colWidths=[120, 320])
    t.setStyle(TableStyle([
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('LINEBELOW', (0,0), (-1,-1), 0.5, colors.lightgrey),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 30))
    
    elements.append(Paragraph("<b>Compliance Team Response:</b>", styles['Normal']))
    elements.append(Spacer(1, 10))
    elements.append(Paragraph(f"<i>\"{data['admin_response']}\"</i>", ParagraphStyle('Review', fontSize=11, leading=16, backColor=colors.HexColor("#f8fafc"), borderPadding=10)))

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
