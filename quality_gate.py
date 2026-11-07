"""Fail-closed structural checks for daily journal metadata.

This checks machine-verifiable evidence only. It does NOT certify journal article
type, the semantic correctness of abstracts, or actual ChatGPT delivery.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JST = dt.timezone(dt.timedelta(hours=9))
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$", re.I)


def load(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def validate(daily, report, introduced, today=None):
    today = today or dt.datetime.now(JST).date().isoformat()
    errors, warnings = [], []
    try:
        observed = dt.datetime.fromisoformat(daily["checked_at_utc"].replace("Z", "+00:00"))
        checked_jst = observed.astimezone(JST).date().isoformat()
    except (KeyError, TypeError, ValueError) as exc:
        errors.append("invalid collection timestamp: " + str(exc))
        checked_jst = None
    if checked_jst != today or daily.get("collected_on") != today:
        errors.append(f"stale collection: checked_jst={checked_jst} collected_on={daily.get('collected_on')} today={today}")
    if report.get("checked_at_utc") != daily.get("checked_at_utc"):
        errors.append("daily/report collection timestamps do not match")
    if report.get("journal_total") != 145 or daily.get("journal_total") != 145:
        errors.append("source journal manifest must contain exactly 145 entries; do not label it as 135")
    counts = daily.get("counts", {})
    if sum(counts.get(k, 0) for k in ("checked", "failed", "unresolved", "truncated")) != 145:
        errors.append("journal status count does not reconcile to 145")
    if counts.get("checked") != 135:
        warnings.append(f"operational 135-journal target not met: checked={counts.get('checked')}")
    items = daily.get("items", [])
    if daily.get("raw_candidate_count") != len(items):
        errors.append("raw candidate count differs from items length")
    known = {k.lower().replace("https://doi.org/", "").strip() for k in introduced.get("introduced_dois", {})}
    duplicates = 0
    seen = set()
    for i, item in enumerate(items):
        doi = (item.get("doi") or "").lower().removeprefix("https://doi.org/").strip()
        if not DOI_PATTERN.fullmatch(doi):
            errors.append(f"item {i}: invalid DOI")
        if doi in seen:
            errors.append(f"item {i}: duplicate DOI in daily list")
        seen.add(doi)
        if not item.get("title") or not item.get("authors"):
            warnings.append(f"item {i}: missing title or authors")
        if item.get("publisher_date_verified_in_window") and (
            not item.get("publisher_doi_matched") or not item.get("publisher_online_date")
        ):
            errors.append(f"item {i}: unsupported publisher-date verification")
        if item.get("abstract_review_status", "") == "present_needs_content_review" and not item.get("abstract", "").strip():
            errors.append(f"item {i}: abstract flagged present but blank")
        if doi in known:
            duplicates += 1
            if not item.get("already_introduced"):
                errors.append(f"item {i}: introduced DOI not marked for exclusion")
        elif item.get("already_introduced"):
            errors.append(f"item {i}: nonintroduced DOI incorrectly excluded")
    if daily.get("already_introduced_count") != duplicates:
        errors.append(f"introduced count inconsistent: expected={duplicates} found={daily.get('already_introduced_count')}")
    if daily.get("eligible_candidate_count") != len(items) - duplicates:
        errors.append("eligible candidate count inconsistent")
    if daily.get("research_candidate_abstracts_missing", 0):
        warnings.append(f"{daily['research_candidate_abstracts_missing']} research candidates lack abstracts")
    evidence_counts = {"publisher_doi_matched": 0,
                       "crossref_published_online_only": 0, "unconfirmed": 0}
    manual_review = []
    for i, item in enumerate(items):
        if item.get("already_introduced"):
            continue
        kind = item.get("article_type_guess", "research_candidate_unverified")
        if kind in ("likely_nonresearch", "prior_publication_not_new"):
            continue
        level = item.get("publication_date_evidence_level") or (
            "publisher_doi_matched" if item.get("publisher_online_date") and item.get("publisher_doi_matched")
            else "crossref_published_online_only" if item.get("crossref_online_date")
            else "unconfirmed"
        )
        if level not in evidence_counts:
            errors.append(f"item {i}: unknown publication evidence level {level}")
            level = "unconfirmed"
        evidence_counts[level] += 1
        reasons = []
        if level != "publisher_doi_matched":
            reasons.append("publisher first-online date not independently verified")
        if item.get("abstract_review_status") != "present_needs_content_review":
            reasons.append("abstract absent or suspect")
        if kind == "book_review_candidate":
            reasons.append("book review requires bibliographic and content verification")
        if reasons:
            manual_review.append({"doi": item.get("doi"), "reasons": reasons})
    if manual_review:
        warnings.append(f"{len(manual_review)} eligible candidates require manual semantic verification")
    status = "failed" if errors else "structurally_valid_not_semantically_verified"
    return {"status": status, "checked_on_jst": today, "raw_candidates": len(items),
            "already_introduced": duplicates, "eligible_candidates": len(items) - duplicates,
            "publisher_date_evidence_counts": evidence_counts,
            "manual_review_required_count": len(manual_review),
            "manual_review_required": manual_review,
            "ready_for_automatic_formal_introduction": False,
            "errors": errors, "warnings": warnings,
            "meaning": "Structural gate only. No automatic confirmation of first publication, substantive abstract accuracy, article type or delivery."}


def main():
    try:
        result = validate(load("state/latest-daily.json"), load("state/latest-report.json"),
                          load("state/introduced-dois.json"))
    except (OSError, ValueError, TypeError) as exc:
        result = {"status": "failed", "errors": [str(exc)]}
    output = ROOT / "state" / "latest-quality-gate.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
