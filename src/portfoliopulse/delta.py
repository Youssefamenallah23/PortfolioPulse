"""Delta mode: pulse events vs the company's last approved run.

The name of the product is a promise: re-run research on the same company and
you get the CHANGE, not another dossier. Comparison happens across runs with
zero new tables: claims of the new run are matched by normalized claim text
against the accepted claims (`approved`/`edited`) of the most recent prior
`approved` run of the same company.

- identical normalized claim text -> "unchanged" (same fact, seen again)
- unmatched claim that deterministically CONTRADICTS an accepted prior claim
  (same classifier as review-time conflicts, `validate.are_contradictory`)
  -> "contradicts" (a pulse event: figures or dates moved)
- everything else -> "new"

Bootstrap problem solved in order: the FIRST approved run is the baseline and
reports no delta. Comparison is read-only; it recomputes on demand at the
edges (run detail API / briefing export) instead of being persisted stale.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from portfoliopulse import models as m
from portfoliopulse.validate import are_contradictory


def _norm(text: str) -> str:
    return " ".join(text.strip().lower().split())


def last_approved_run(session: Session, run: m.ResearchRun) -> m.ResearchRun | None:
    return (
        session.query(m.ResearchRun)
        .filter(
            m.ResearchRun.company_id == run.company_id,
            m.ResearchRun.id != run.id,
            m.ResearchRun.status == "approved",
            m.ResearchRun.finished_at.isnot(None),
        )
        .order_by(m.ResearchRun.finished_at.desc(), m.ResearchRun.id.desc())
        .first()
    )


def delta_for_run(session: Session, run: m.ResearchRun) -> dict | None:
    """None when this run has no earlier approved sibling (it IS the baseline)."""
    prior = last_approved_run(session, run)
    if prior is None:
        return None

    prior_claims = [
        c
        for c in session.query(m.Claim)
        .filter_by(run_id=prior.id)
        .order_by(m.Claim.created_at, m.Claim.id)
        .all()
        if c.review_status in ("approved", "edited")
    ]
    prior_by_key = {_norm(c.claim_text): c for c in prior_claims}

    unchanged: list[str] = []
    new: list[str] = []
    contradicts: list[dict[str, str]] = []
    current_claims = [
        c
        for c in session.query(m.Claim)
        .filter_by(run_id=run.id)
        .order_by(m.Claim.created_at, m.Claim.id)
        .all()
        if c.review_status != "rejected"
    ]
    for claim in current_claims:
        if _norm(claim.claim_text) in prior_by_key:
            unchanged.append(claim.claim_text)
            continue
        clash = next(
            (old for old in prior_claims if are_contradictory(claim, old) is not None), None
        )
        if clash is not None:
            contradicts.append({"claim": claim.claim_text, "prior": clash.claim_text})
        else:
            new.append(claim.claim_text)

    return {
        "prior_run_id": prior.id,
        "prior_finished_at": prior.finished_at.isoformat() + "Z",
        "unchanged_count": len(unchanged),
        "unchanged": unchanged,
        "new": new,
        "contradicts": contradicts,
    }
