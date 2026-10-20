"""Exports: Markdown analyst briefing + Excel research pack (+ manifest).

Rules enforced here
- ONLY `approved` runs export; review state is authoritative: rejected claims are
  hard-excluded via review.visible_claims, reviewer edits appear as final wording.
- Generated purely from persisted records — no LLM regeneration, no invented text.
- Disputed (conflicting-verification) claims are included but loudly labelled.
- File paths are slugged and run-scoped: exports never collide with other runs.
All timestamps naive-UTC internally, ISO+Z at the manifest/report edge.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from portfoliopulse import models as m
from portfoliopulse.db import utcnow
from portfoliopulse.review import visible_claims


class ExportBlocked(RuntimeError):
    """Raised for runs that have not been approved for export."""


class ExportWriteError(RuntimeError):
    """An export file failed to generate; the partial manifest records the failure."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "unnamed-company"


def _footnote_key(source_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", source_id.lower())[:12]


def _reviewed_claims(session: Session, run: m.ResearchRun) -> list[m.Claim]:
    """Approved/edited material in final wording; rejected material hard-excluded."""
    return [
        claim
        for claim in visible_claims(session, run)
        if claim.review_status in ("approved", "edited")
    ]


def _add_sources_block(session: Session, run: m.ResearchRun, lines: list[str]) -> str:
    """Section-9 bullet list + footnote definitions. Returns the footnote defs."""
    claims = _reviewed_claims(session, run)
    seen: set[str] = set()
    footnotes: list[str] = []
    for claim in claims:
        for source in claim.sources:
            if source.id in seen:
                continue
            seen.add(source.id)
            retrieved = source.retrieved_at.isoformat() if source.retrieved_at else "date unknown"
            label = source.publisher or source.title or source.canonical_url[:60]
            lines.append(f"- [{label}]({source.canonical_url}) — retrieved {retrieved}")
            footnotes.append(
                f"[^{_footnote_key(source.id)}]: [{label}]({source.canonical_url}) — retrieved {retrieved}"
            )
    return "\n".join(footnotes)


def _cite(claim: m.Claim) -> str:
    refs = " ".join(f"[^{_footnote_key(source.id)}]" for source in claim.sources)
    return refs or "[^nosource]"


def _hypotheses(session: Session, run: m.ResearchRun) -> list[m.Hypothesis]:
    return (
        session.query(m.Hypothesis)
        .filter_by(run_id=run.id)
        .filter(m.Hypothesis.status.in_(("approved", "edited")))
        .order_by(m.Hypothesis.created_at)
        .all()
    )


def _approved_text(hyp: m.Hypothesis) -> str:
    text = hyp.hypothesis_text
    if text.startswith("DRAFT —"):
        text = text[len("DRAFT — ") :].strip()  # approved wording sheds the draft label
    return text


# ---------------------------------------------------------------------------
# Markdown analyst briefing
# ---------------------------------------------------------------------------


def build_markdown_briefing(session: Session, run: m.ResearchRun) -> str:
    """The 10-section briefing. Every factual statement carries source citations."""
    company = session.get(m.Company, run.company_id)
    claims = _reviewed_claims(session, run)
    hypotheses = _hypotheses(session, run)

    established = [c for c in claims if c.verification_status == "supported"]
    disputed = [c for c in claims if c.verification_status == "conflicting"]
    profile_claims = [c for c in claims if c.category in ("company_profile", "ownership")]
    events = [c for c in established if c.category in m.EVENT_CATEGORIES]
    observations = [c for c in established if c.category not in m.EVENT_CATEGORIES]
    review_counts = {
        decision: session.query(m.Review).filter_by(decision=decision).count()
        for decision in ("approved", "edited", "rejected", "flagged")
    }

    lines: list[str] = []
    lines.append(f"# Analyst briefing — {company.name}")
    lines.append("")
    lines.append(f"Generated: {utcnow().isoformat()}Z · Run `{run.id}` (approved exports)")
    lines.append("")
    lines.append("## 1. Company and research scope")
    lines.append(
        f"- Company: **{company.name}**"
        + (f" ({company.sector})" if company.sector else "")
        + (f" · {company.country}" if company.country else "")
    )
    if company.website:
        lines.append(f"- Official site: {company.website}")
    lines.append(f"- Research focus: {run.research_focus}")
    lines.append("- Every statement below cites a listed public source; unknown stays unknown.")
    lines.append("")
    lines.append("## 2. Executive summary")
    if disputed:
        lines.append(
            f"> ⚠️ {len(disputed)} disputed statement(s) exist (conflicting public sources); "
            "labelled DISPUTED below and never usable as headline facts."
        )
    for claim in established[:5]:
        lines.append(f"- {claim.claim_text} {_cite(claim)}")
    if not established:
        lines.append("- No undisputed approved findings for this run yet.")
    lines.append("")
    lines.append("## 3. Company and ownership context")
    if profile_claims:
        lines.extend(f"- {c.claim_text} {_cite(c)}" for c in profile_claims)
    else:
        lines.append("(no approved profile claims in this run)")
    lines.append("")
    lines.append("## 4. Material public developments")
    if events:
        for claim in events:
            date_part = f" (dated {claim.event_date.isoformat()})" if claim.event_date else ""
            lines.append(f"- {claim.claim_text}{date_part} {_cite(claim)}")
    else:
        lines.append("(no dated events approved in this run)")
    lines.append("")
    lines.append("## 5. Evidence-backed observations")
    if observations:
        lines.extend(f"- {c.claim_text} (category: {c.category}) {_cite(c)}" for c in observations)
    for claim in disputed:
        lines.append(f"- **DISPUTED** — {claim.claim_text} {_cite(claim)}")
    lines.append("")
    lines.append("## 6. Preliminary hypotheses to investigate")
    if hypotheses:
        for hyp in hypotheses:
            prefix = (
                "APPROVED hypothesis" if hyp.status == "approved" else "reviewer-edited wording"
            )
            lines.append(f"### {hyp.title} · {prefix}")
            lines.append("")
            lines.append(_approved_text(hyp))
            lines.append("")
            lines.append(f"- Supporting claims: {', '.join(c.id for c in hyp.claims)}")
            lines.append("")
    else:
        lines.append("(no approved hypotheses in this run)")
        lines.append("")
    lines.append("## 7. Alternative explanations and limitations")
    if hypotheses:
        for hyp in hypotheses:
            lines.extend(f"- {alt}" for alt in hyp.alternative_explanations)
            lines.extend(f"- Missing evidence: {item}" for item in hyp.missing_evidence)
    lines.append("")
    lines.append("## 8. Open questions / recommended next research steps")
    if hypotheses:
        for hyp in hypotheses:
            lines.extend(f"- {question}" for question in hyp.questions_to_validate)
    lines.append("")
    lines.append("## 9. Sources and retrieval date")
    footnote_defs = _add_sources_block(session, run, lines)
    if not footnote_defs:
        lines.append("(no sources)")
    lines.append("")
    lines.append("## 10. Review and approval status")
    lines.append(f"- Run status: `{run.status}` (exports approved; see manifest)")
    lines.append(
        f"- Claim reviews recorded: {review_counts['approved']} approved, "
        f"{review_counts['edited']} edited, {review_counts['rejected']} rejected, "
        f"{review_counts['flagged']} flagged"
    )
    lines.append(
        "- Rejected/flagged material is excluded from sections 2–5 and remains available in the audit trail."
    )
    lines.append("")
    if footnote_defs:
        lines.append(footnote_defs)  # renders Markdown footnotes exactly once
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Excel research pack
# ---------------------------------------------------------------------------

SHEET_HEADERS: dict[str, list[str]] = {
    "Executive Summary": ["Key finding / state", "Category", "Citations"],
    "Company Profile": [
        "Company name",
        "Official site",
        "Sector",
        "Country",
        "Profile claims (approved only)",
    ],
    "Events": ["Event date", "Category", "Statement", "Source URLs"],
    "Evidence Register": [
        "Claim ID",
        "Category",
        "Statement",
        "Supporting excerpt",
        "Event date",
        "Verification",
        "Review status",
        "Source URLs",
    ],
    "Hypotheses": [
        "Hypothesis ID",
        "Title",
        "Status",
        "Statement",
        "Supporting claim IDs",
        "Alternative explanations",
        "Missing evidence",
        "Questions to validate",
    ],
    "Open Questions": ["Type", "Item", "Detail"],
}


def build_workbook(session: Session, run: m.ResearchRun):
    """Openpyxl workbook built from the reviewed data snapshot (spec's 6 sheets)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    company = session.get(m.Company, run.company_id)
    claims = _reviewed_claims(session, run)
    hypotheses = _hypotheses(session, run)
    established = [c for c in claims if c.verification_status == "supported"]
    disputed = [c for c in claims if c.verification_status == "conflicting"]

    workbook = Workbook()
    workbook.remove(workbook.active)
    wrap = Alignment(wrap_text=True, vertical="top")

    def _new_sheet(name: str):
        sheet = workbook.create_sheet(name)
        sheet.append(SHEET_HEADERS[name])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for col, header in enumerate(SHEET_HEADERS[name], start=1):
            sheet.column_dimensions[get_column_letter(col)].width = min(
                max(14, len(header) + 6), 60
            )
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        return sheet

    def _wrap_rows(sheet):
        for row in sheet.iter_rows(min_row=1):
            for cell in row:
                cell.alignment = wrap

    # 1 Executive Summary
    sheet = _new_sheet("Executive Summary")
    sheet.append(
        ["Research scope", f"{company.name} — {run.research_focus}", f"Run {run.id} · {run.status}"]
    )
    sheet.append(
        [
            "Review state",
            "Approved exports generated after completed human review",
            f"Generated {utcnow().isoformat()}Z",
        ]
    )
    for claim in established[:8]:
        sheet.append(
            [claim.claim_text, claim.category, " ; ".join(s.canonical_url for s in claim.sources)]
        )
    for claim in disputed:
        sheet.append(
            [
                "⚠️ DISPUTED: " + claim.claim_text,
                claim.category,
                "conflicting sources — see Open Questions",
            ]
        )
    _wrap_rows(sheet)

    # 2 Company Profile
    sheet = _new_sheet("Company Profile")
    profile = [c.claim_text for c in claims if c.category in ("company_profile", "ownership")]
    sheet.append(
        [
            company.name,
            company.website or "(unknown)",
            company.sector or "(unknown)",
            company.country or "(unknown)",
            " ; ".join(profile) or "(no approved profile claims)",
        ]
    )
    _wrap_rows(sheet)

    # 3 Events: dated claims only — unknown dates stay out (never invented)
    sheet = _new_sheet("Events")
    for claim in claims:
        if claim.category in m.EVENT_CATEGORIES and claim.event_date:
            sheet.append(
                [
                    claim.event_date.isoformat(),
                    claim.category,
                    claim.claim_text,
                    " ; ".join(s.canonical_url for s in claim.sources),
                ]
            )
    _wrap_rows(sheet)

    # 4 Evidence Register: every approved/edited claim with full provenance
    sheet = _new_sheet("Evidence Register")
    for claim in claims:
        sheet.append(
            [
                claim.id,
                claim.category,
                claim.claim_text,
                claim.supporting_excerpt,
                claim.event_date.isoformat() if claim.event_date else None,
                claim.verification_status,
                claim.review_status,
                " ; ".join(s.canonical_url for s in claim.sources),
            ]
        )
    _wrap_rows(sheet)

    # 5 Hypotheses
    sheet = _new_sheet("Hypotheses")
    for hyp in hypotheses:
        sheet.append(
            [
                hyp.id,
                hyp.title,
                hyp.status,
                _approved_text(hyp),
                ", ".join(c.id for c in hyp.claims),
                " ; ".join(hyp.alternative_explanations),
                " ; ".join(hyp.missing_evidence),
                " ; ".join(hyp.questions_to_validate),
            ]
        )
    _wrap_rows(sheet)

    # 6 Open Questions: disclosed conflicts + research questions + open issues
    sheet = _new_sheet("Open Questions")
    for claim in disputed:
        sheet.append(
            ["Disclosed conflict", claim.claim_text, "conflicting public sources; review recorded"]
        )
    for hyp in hypotheses:
        for question in hyp.questions_to_validate:
            sheet.append(["Hypothesis question", hyp.title, question])
    for issue in (
        session.query(m.ValidationIssue).filter_by(run_id=run.id, review_status="pending").all()
    ):
        sheet.append(["Validation issue", issue.issue_type, issue.message or "(no detail)"])
    _wrap_rows(sheet)

    return workbook


# ---------------------------------------------------------------------------
# manifest + orchestrator
# ---------------------------------------------------------------------------


def run_export_dir(run: m.ResearchRun, company: m.Company, out_root: str | Path) -> Path:
    """Run-scoped export directory: different runs (same company) never collide."""
    return Path(out_root) / f"{run.id[:20]}_{slugify(company.name)}"


def export_run(
    session: Session, run: m.ResearchRun, *, out_root: str | Path = "./artifacts"
) -> dict:
    """Generate Markdown + Excel exports for an APPROVED run; returns the manifest."""
    if run.status != "approved":
        raise ExportBlocked(
            "run is not approved for export; complete the claim and hypothesis review first"
        )
    company = session.get(m.Company, run.company_id)
    run_dir = run_export_dir(run, company, out_root)
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "run_id": run.id,
        "company_id": run.company_id,
        "generated_at": utcnow().isoformat() + "Z",
        "approval_status": run.status,
        "directory": str(run_dir),
        "files": [],
        "warnings": [],
    }

    def _write(name: str, writer) -> None:
        try:
            writer(run_dir / name)
            manifest["files"].append(name)
        except Exception as exc:  # noqa: BLE001 - failure boundary: record, then re-raise
            manifest["warnings"].append(f"{name} failed: {type(exc).__name__}: {exc}")

    _write(
        "analyst_brief.md",
        lambda path: path.write_text(build_markdown_briefing(session, run), encoding="utf-8"),
    )
    _write("research_pack.xlsx", lambda path: build_workbook(session, run).save(path))

    disputed_count = (
        session.query(m.Claim).filter_by(run_id=run.id, verification_status="conflicting").count()
    )
    if disputed_count:
        manifest["warnings"].append(
            f"{disputed_count} conflicting claim(s) disclosed in the briefing and Open Questions"
        )
    try:
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        raise ExportWriteError(f"could not write manifest.json: {exc}") from exc

    failures = [warning for warning in manifest["warnings"] if " failed: " in warning]
    if failures:
        raise ExportWriteError("; ".join(failures))
    return manifest
