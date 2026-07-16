from __future__ import annotations

import html
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image as ReportImage,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .config import PDF_OUTPUT_DIR, ROOT_DIR
from .models import FindingStatus, ReviewState
from .utils import atomic_json_write, relative_to_root, sha256_file


STATUS_COLORS = {
    FindingStatus.FOUND: "#22634f",
    FindingStatus.NOT_FOUND: "#a94735",
    FindingStatus.NEEDS_REVIEW: "#a66c12",
    FindingStatus.INTERNAL: "#5f6872",
}


def _rate_summary(state: ReviewState) -> str:
    requested = state.application.requested_price
    match = next((finding for finding in state.findings if finding.criterion_id == "fee_match"), None)
    if requested is None:
        return "The application did not provide a reliably parsed amount."
    if not match:
        return f"Application amount: ${requested:,.2f}. No direct website comparison applies to this checklist."
    return f"Application amount: ${requested:,.2f}. {match.note}"


def _artifact_href(report_dir: Path, root_relative: str) -> str:
    target = ROOT_DIR / root_relative
    return Path(os.path.relpath(target, report_dir)).as_posix()


def generate_html(state: ReviewState, review_dir: Path) -> Path:
    app = state.application
    rows = []
    evidence_by_id = {item.id: item for item in state.evidence}
    for finding in state.findings:
        links = []
        for evidence_id in finding.evidence_ids:
            record = evidence_by_id.get(evidence_id)
            if record:
                href = _artifact_href(review_dir, record.stamped_path)
                links.append(f'<a href="{html.escape(href)}">{html.escape(evidence_id)}</a>')
        url = f'<a href="{html.escape(finding.url)}">Source page</a>' if finding.url else "-"
        quote = f'<blockquote>{html.escape(finding.quote)}</blockquote>' if finding.quote else ""
        rows.append(
            "<tr>"
            f'<td><strong>{html.escape(finding.label)}</strong>{quote}</td>'
            f'<td><span class="status {finding.status.value.lower().replace(" ", "-")}">{finding.status.value}</span></td>'
            f'<td>{html.escape(finding.note)}</td>'
            f'<td>{url}<br>{" ".join(links) or "-"}</td>'
            "</tr>"
        )
    gallery = []
    for evidence in state.evidence:
        href = _artifact_href(review_dir, evidence.stamped_path)
        gallery.append(
            f'<figure><a href="{html.escape(href)}"><img src="{html.escape(href)}" alt="{html.escape(evidence.id)}"></a>'
            f'<figcaption>{html.escape(evidence.id)} - {html.escape(evidence.kind.replace("_", " "))}</figcaption></figure>'
        )
    notes = "".join(f"<li>{html.escape(note)}</li>" for note in state.reviewer_notes) or "<li>No reviewer notes.</li>"
    report = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pre-Approval Website Review - {html.escape(state.id[:8])}</title>
<style>
body{{font-family:Arial,sans-serif;color:#16241f;margin:0;background:#f5f2e9}}main{{max-width:1120px;margin:0 auto;padding:42px}}
header{{background:#173b31;color:white;padding:34px;border-radius:18px}}h1{{margin:0 0 8px;font-size:30px}}h2{{margin-top:34px}}
.notice{{background:#fff2d5;border-left:5px solid #c68819;padding:14px 18px;margin:22px 0}}.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.card{{background:white;border:1px solid #d9ddd8;border-radius:12px;padding:16px}}.label{{font-size:12px;color:#657069;text-transform:uppercase;letter-spacing:.08em}}
table{{border-collapse:collapse;width:100%;background:white;font-size:14px}}th,td{{border:1px solid #dce1dd;padding:12px;vertical-align:top;text-align:left}}th{{background:#e8eee9}}
.status{{white-space:nowrap;border-radius:99px;padding:5px 9px;color:white;font-weight:700;font-size:12px}}.found{{background:#22634f}}.not-found{{background:#a94735}}.needs-review{{background:#a66c12}}.internal{{background:#5f6872}}
blockquote{{margin:9px 0 0;padding-left:10px;border-left:3px solid #b8c7bf;color:#45524c}}.gallery{{display:grid;grid-template-columns:repeat(2,1fr);gap:18px}}
figure{{margin:0;background:white;padding:10px;border:1px solid #d9ddd8;border-radius:12px}}figure img{{display:block;width:auto;max-width:100%;max-height:520px;margin:0 auto;object-fit:contain;background:#eee}}
figcaption{{padding:8px 3px 2px;color:#53605a}}footer{{margin-top:40px;color:#657069;font-size:12px}}@media(max-width:760px){{main{{padding:18px}}.grid,.gallery{{grid-template-columns:1fr}}table{{display:block;overflow-x:auto}}}}
</style></head><body><main>
<header><div>Website verification record</div><h1>{html.escape(app.requested_item or "Pre-approval request")}</h1><div>Review {html.escape(state.id[:8])} - {html.escape(state.created_at[:10])}</div></header>
<div class="notice"><strong>Human decision required.</strong> This report gathers public evidence and does not approve or deny the request.</div>
<section class="grid">
<div class="card"><div class="label">Participant</div><strong>{html.escape(app.participant_name or "Not extracted")}</strong></div>
<div class="card"><div class="label">Provider</div><strong>{html.escape(app.provider_name or "Not extracted")}</strong></div>
<div class="card"><div class="label">Category</div><strong>{html.escape((app.category or "Unknown").replace("_", " ").title())}</strong></div>
<div class="card"><div class="label">Application price</div><strong>{html.escape(app.requested_price_text or "Not extracted")}</strong></div>
<div class="card"><div class="label">Website</div><a href="{html.escape(app.website_url or "#")}">{html.escape(app.website_url or "Not extracted")}</a></div>
<div class="card"><div class="label">Review date</div><strong>{html.escape(state.updated_at[:19].replace("T", " "))} UTC</strong></div>
</section>
<h2>Rate comparison</h2><div class="card">{html.escape(_rate_summary(state))}</div>
<h2>Checklist findings</h2><table><thead><tr><th>Criterion</th><th>Status</th><th>Reviewer note</th><th>Evidence</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Reviewer notes</h2><div class="card"><ul>{notes}</ul></div>
<h2>Evidence captures</h2><div class="gallery">{''.join(gallery) or '<p>No screenshots were captured. Review access warnings above.</p>'}</div>
<footer>Generated by the Pre-Approval Website Verification Tool. Website evidence is time-sensitive and must be reviewed by authorized staff.</footer>
</main></body></html>"""
    path = review_dir / "report.html"
    path.write_text(report, encoding="utf-8")
    state.report_html = relative_to_root(path, ROOT_DIR)
    return path


def _pdf_footer(canvas, document) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#63716b"))
    canvas.drawString(0.65 * inch, 0.4 * inch, "Evidence support only - final approval or denial remains with authorized staff.")
    canvas.drawRightString(7.85 * inch, 0.4 * inch, f"Page {document.page}")
    canvas.restoreState()


def generate_pdf(state: ReviewState, review_dir: Path) -> Path:
    path = review_dir / "report.pdf"
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=colors.HexColor("#173b31"), alignment=TA_LEFT, spaceAfter=12))
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=8.5, leading=11, textColor=colors.HexColor("#415149")))
    styles.add(ParagraphStyle(name="TableSmall", parent=styles["BodyText"], fontSize=7.2, leading=8.6, textColor=colors.HexColor("#415149")))
    styles.add(ParagraphStyle(name="Notice", parent=styles["BodyText"], backColor=colors.HexColor("#fff2d5"), borderColor=colors.HexColor("#c68819"), borderWidth=1, borderPadding=9, leading=14, spaceAfter=16))
    document = SimpleDocTemplate(str(path), pagesize=letter, rightMargin=0.55 * inch, leftMargin=0.55 * inch, topMargin=0.55 * inch, bottomMargin=0.65 * inch, title="Pre-Approval Website Review")
    story = [
        Paragraph("Pre-Approval Website Review", styles["ReportTitle"]),
        Paragraph("Human decision required. This report gathers public evidence and does not approve or deny the request.", styles["Notice"]),
    ]
    app = state.application
    summary_data = [
        ["Participant", html.escape(app.participant_name or "Not extracted"), "Category", html.escape((app.category or "Unknown").replace("_", " ").title())],
        ["Provider", html.escape(app.provider_name or "Not extracted"), "Request", html.escape(app.requested_item or "Not extracted")],
        ["Application price", html.escape(app.requested_price_text or "Not extracted"), "Reviewed", html.escape(state.updated_at[:19].replace("T", " ") + " UTC")],
        ["Website", Paragraph(html.escape(app.website_url or "Not extracted"), styles["Small"]), "Review ID", state.id[:8]],
    ]
    summary_table = Table(summary_data, colWidths=[0.95 * inch, 2.25 * inch, 1.0 * inch, 2.65 * inch])
    summary_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e8eee9")), ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#e8eee9")), ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"), ("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd4cf")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 7)]))
    story.extend([summary_table, Spacer(1, 16), Paragraph("Rate comparison", styles["Heading2"]), Paragraph(html.escape(_rate_summary(state)), styles["BodyText"]), Spacer(1, 14), Paragraph("Checklist findings", styles["Heading2"])])
    finding_rows = [["Criterion", "Status", "Finding", "Evidence"]]
    for finding in state.findings:
        evidence = ", ".join(finding.evidence_ids) or "-"
        body = html.escape(finding.note)
        if finding.quote:
            body += "<br/><i>" + html.escape(finding.quote[:260]) + "</i>"
        finding_rows.append([
            Paragraph(html.escape(finding.label), styles["TableSmall"]),
            Paragraph(f"<b>{html.escape(finding.status.value)}</b>", styles["TableSmall"]),
            Paragraph(body, styles["TableSmall"]),
            Paragraph(html.escape(evidence), styles["TableSmall"]),
        ])
    findings_table = Table(finding_rows, repeatRows=1, colWidths=[1.55 * inch, 0.8 * inch, 3.75 * inch, 0.72 * inch])
    table_commands = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#173b31")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 8), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd4cf")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 4)]
    for row_index, finding in enumerate(state.findings, 1):
        table_commands.append(("TEXTCOLOR", (1, row_index), (1, row_index), colors.HexColor(STATUS_COLORS[finding.status])))
    findings_table.setStyle(TableStyle(table_commands))
    story.append(findings_table)
    if state.reviewer_notes:
        story.extend([Spacer(1, 14), Paragraph("Reviewer notes", styles["Heading2"])])
        story.extend(Paragraph("- " + html.escape(note), styles["BodyText"]) for note in state.reviewer_notes)
    if state.evidence:
        story.extend([PageBreak(), Paragraph("Evidence index", styles["Heading2"])])
        for record in state.evidence:
            image_path = ROOT_DIR / record.stamped_path
            caption = f"{record.id} - {record.kind.replace('_', ' ')} - {record.url}"
            blocks = [Paragraph(html.escape(caption), styles["Small"]), Spacer(1, 5)]
            if image_path.exists():
                try:
                    image = ReportImage(str(image_path))
                    max_width, max_height = 6.85 * inch, 6.8 * inch
                    scale = min(max_width / image.imageWidth, max_height / image.imageHeight, 1)
                    image.drawWidth = image.imageWidth * scale
                    image.drawHeight = image.imageHeight * scale
                    blocks.append(image)
                except Exception:
                    blocks.append(Paragraph("Image available in the evidence folder.", styles["Small"]))
            story.extend([KeepTogether(blocks), Spacer(1, 14)])
    document.build(story, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)
    state.report_pdf = relative_to_root(path, ROOT_DIR)
    stable_copy = PDF_OUTPUT_DIR / f"{state.id}-report.pdf"
    stable_copy.write_bytes(path.read_bytes())
    return path


def generate_manifest(state: ReviewState, review_dir: Path) -> Path:
    manifest = {
        "review_id": state.id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "human_decision_required": True,
        "application": state.application.model_dump(mode="json"),
        "application_sha256": state.application_sha256,
        "findings": [item.model_dump(mode="json") for item in state.findings],
        "evidence": [item.model_dump(mode="json") for item in state.evidence],
        "reports": {
            "html": state.report_html,
            "pdf": state.report_pdf,
        },
    }
    path = review_dir / "manifest.json"
    atomic_json_write(path, manifest)
    return path


def package_review(state: ReviewState, review_dir: Path) -> Path:
    package = review_dir / f"review-{state.id[:8]}-package.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(review_dir.rglob("*")):
            if path.is_file() and path != package and not path.name.endswith(".tmp"):
                archive.write(path, path.relative_to(review_dir))
    state.package_zip = relative_to_root(package, ROOT_DIR)
    return package


def generate_report_package(state: ReviewState, review_dir: Path) -> None:
    generate_html(state, review_dir)
    generate_pdf(state, review_dir)
    generate_manifest(state, review_dir)
    package_review(state, review_dir)
