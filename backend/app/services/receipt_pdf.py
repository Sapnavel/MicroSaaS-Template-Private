"""Receipt/invoice PDF rendering. HMS Project Completion Prompt gap
("receipt generation") -- `billing_service.generate_receipt_pdf` is the only
caller; this module is pure layout, no DB access, no authorization (that
already happened in the caller).

Renders the SAME document regardless of `invoice.status`, with one cosmetic
difference: the heading reads "RECEIPT" for a `paid` invoice and "INVOICE"
otherwise (an unpaid invoice is a bill, not proof of payment -- do not
mislabel one as the other). This is a real generated PDF (reportlab), not a
placeholder -- but it is still just a formatted document, not an integration
with any payment processor; see `billing_engine.mark_invoice_paid`'s
docstring for why "paid" here means "a staff member confirmed collection
happened outside this system," not "a gateway confirmed a charge."
"""

from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

from app.models.billing import Invoice, InsuranceClaim, InvoiceItem
from app.schemas.billing import InvoiceResponse

_SOURCE_TYPE_LABEL = {
    "consultation": "Consultation",
    "lab_order": "Lab order",
    "prescription": "Prescription",
    "admission": "Admission",
}


def _money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def build_receipt_pdf(
    *,
    invoice: Invoice,
    patient_name: str,
    items: list[InvoiceItem],
    claim: InsuranceClaim | None,
) -> bytes:
    """Renders `invoice` (plus its line items and claim, if any) as a PDF
    and returns the raw bytes -- the router wraps this in a `Response` with
    `media_type="application/pdf"`, it never touches disk."""
    summary = InvoiceResponse.model_validate(invoice)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ReceiptTitle", parent=styles["Title"], fontSize=20)
    meta_style = ParagraphStyle("ReceiptMeta", parent=styles["Normal"], fontSize=10, textColor=colors.grey)

    heading = "RECEIPT" if invoice.status == "paid" else "INVOICE"
    story = [
        Paragraph("Hospital Management System", meta_style),
        Paragraph(heading, title_style),
        Spacer(1, 0.15 * inch),
        Paragraph(f"Invoice ID: {invoice.id}", meta_style),
        Paragraph(f"Patient: {patient_name}", meta_style),
        Paragraph(f"Status: {invoice.status.upper()}", meta_style),
        Paragraph(f"Created: {invoice.created_at.strftime('%Y-%m-%d %H:%M UTC')}", meta_style),
        Spacer(1, 0.3 * inch),
    ]

    if items:
        item_rows = [["Description", "Type", "Amount"]]
        for item in items:
            item_rows.append(
                [item.description, _SOURCE_TYPE_LABEL.get(item.source_type, item.source_type), _money(item.amount)]
            )
        item_table = Table(item_rows, colWidths=[3.2 * inch, 1.8 * inch, 1.3 * inch])
        item_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
                ]
            )
        )
        story.append(item_table)
    else:
        story.append(Paragraph("No line items on this invoice yet.", styles["Normal"]))

    story.append(Spacer(1, 0.25 * inch))

    totals_rows = [["Subtotal", _money(summary.total_amount)]]
    if summary.discount_amount > 0:
        totals_rows.append(["Discount", f"-{_money(summary.discount_amount)}"])
    if summary.tax_rate_percent is not None:
        totals_rows.append([f"Tax ({summary.tax_rate_percent}%)", _money(summary.tax_amount)])
    totals_rows.append(["Grand total", _money(summary.grand_total)])

    totals_table = Table(totals_rows, colWidths=[5 * inch, 1.3 * inch])
    totals_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, -1), (-1, -1), 1, colors.HexColor("#1f2937")),
                ("TOPPADDING", (0, -1), (-1, -1), 6),
            ]
        )
    )
    story.append(totals_table)

    if claim is not None:
        story.append(Spacer(1, 0.3 * inch))
        story.append(Paragraph("Insurance claim", styles["Heading3"]))
        claim_rows = [
            ["Payer", claim.payer_name],
            ["Claim amount", _money(claim.claim_amount)],
            ["Patient copay", _money(claim.patient_copay)],
            ["Claim status", claim.state.upper()],
        ]
        claim_table = Table(claim_rows, colWidths=[2 * inch, 4.3 * inch])
        claim_table.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")]))
        story.append(claim_table)

    story.append(Spacer(1, 0.4 * inch))
    story.append(
        Paragraph(
            "This is a system-generated document. Payment status reflects this system's own records "
            "only -- no external payment processor is integrated.",
            meta_style,
        )
    )

    doc.build(story)
    return buffer.getvalue()
