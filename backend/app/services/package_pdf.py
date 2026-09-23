"""Printable PDF of a dispute package: the letter, then an enclosure checklist."""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer


def render_package_pdf(package: dict) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch,
        title=package.get("subject", "Dispute"),
    )
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    story = []
    for line in package["body"].splitlines():
        story.append(Paragraph(escape(line), body) if line.strip() else Spacer(1, 8))

    story += [PageBreak(), Paragraph("Before you send", styles["Heading2"])]
    for item in package.get("enclosures", []):
        story.append(Paragraph(f"&#9744; Enclose: {escape(item)}", body))
    story.append(Spacer(1, 8))
    for warning in package.get("warnings", []):
        story.append(Paragraph(f"&#9744; {escape(warning)}", body))
    story.append(Paragraph(
        "&#9744; Keep a copy of everything you send. Certified mail with return receipt gives you proof of the "
        "date it was received, which starts the investigation clock.", body,
    ))
    doc.build(story)
    return buffer.getvalue()
