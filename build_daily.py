"""Build a dated shortlist; try to recover missing abstracts from publisher pages/OpenAlex.

The Crossref date is a *candidate* publication date. Only explicit publisher
online-date metadata, associated with a matching DOI, counts as First View evidence.
No email services, language models or paid APIs are used.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
EARLIER_PUBLICATIONS = json.loads((ROOT / "data" / "known-earlier-publications.json").read_text(encoding="utf-8"))
JST = dt.timezone(dt.timedelta(hours=9))


def load(name):
    return json.loads((STATE / name).read_text(encoding="utf-8"))


def save(name, value):
    (STATE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def kind_guess(title, subtype=""):
    """Classify book reviews separately; preserve independent scholarly articles."""
    s = " ".join(html.unescape(title or "").split())
    st = (subtype or "").lower().strip()
    if re.search(r"\b20\d{2},\s*\d+\s*(?:pp\.?|pages)\b", s, re.I):
        return "book_review_candidate"
    if st in {"review", "book-review"} or re.search(r"^(?:book review|review of)\b", s, re.I):
        return "book_review_candidate"
    if re.search(r"^(?:publisher correction|correction|corrigendum|retraction|erratum|editorial|editor['’]?s note|announcement|ask a political scientist)\b", s, re.I):
        return "likely_nonresearch"
    if re.search(r"\bwritten by\b|\beds?\.\s|\bTo Our Guest Reviewers\b|\.\s*By\s+[A-Z][A-Za-z-]+", s, re.I):
        return "book_review_candidate" if (re.search(r"\bwritten by\b", s, re.I) or re.search(r"\.\s*By\s+[A-Z]", s)) else "likely_nonresearch"
    return "research_candidate_unverified"


def text_clean(value):
    return " ".join(html.unescape(value or "").split()).strip()


def date_parse(value):
    s = text_clean(value)
    for form in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%d %B %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(s[:40], form).date().isoformat()
        except ValueError:
            continue
    return None


def meta_content(soup, names):
    names = {n.lower() for n in names}
    for tag in soup.find_all("meta"):
        key = (tag.get("name") or tag.get("property") or "").lower()
        if key in names and tag.get("content"):
            return text_clean(tag["content"])
    return ""


def publisher_urls(item_url, doi, date):
    """Use a small set of official alternative paths before declaring HTML inaccessible."""
    urls = [item_url]
    if doi.lower().startswith("10.1111/"):
        urls += [f"https://onlinelibrary.wiley.com/doi/full/{doi}",
                 f"https://onlinelibrary.wiley.com/doi/abs/{doi}"]
    elif doi.lower().startswith("10.1086/"):
        urls += [f"https://www.journals.uchicago.edu/doi/full/{doi}",
                 f"https://www.journals.uchicago.edu/doi/abs/{doi}"]
    elif doi.lower().startswith("10.1136/") and date:
        try:
            published = dt.date.fromisoformat(date)
            slug = doi.rsplit("/", 1)[-1]
            for shift in (-1, 0):
                day = published + dt.timedelta(days=shift)
                urls.append(f"https://jme.bmj.com/content/early/{day:%Y/%m/%d}/{slug}.info")
        except ValueError:
            pass
    return list(dict.fromkeys(urls))


def abstract_review(value):
    """Warning flag, not automatic exclusion: suspicious text needs human review."""
    s = text_clean(value)
    if not s:
        return "missing"
    if re.match(r"^https?://", s, re.I) or re.search(r"\.(?:png|jpe?g|gif|svg)(?:\?|$)", s, re.I):
        return "suspect_media_link"
    if re.match(r"^(?:footnotes|author affiliations?|correspondence|department of|faculty of|university of)\b", s, re.I):
        return "suspect_nonabstract"
    return "present_needs_content_review"


def extract_publisher(html_text, doi):
    """Return only DOI-matched metadata; never infer an abstract from a title."""
    soup = BeautifulSoup(html_text, "html.parser")
    stated = meta_content(soup, ["citation_doi", "dc.identifier.doi", "doi"])
    stated = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", stated, flags=re.I).lower().strip()
    if not stated or stated != doi.lower():
        return {"doi_matched": False, "abstract": "", "online_date": None}
    abstract = meta_content(soup, ["citation_abstract", "dc.abstract", "dcterms.abstract"])
    if len(abstract) < 100:
        for selector in ("#Abs1-content", "section#abstract", "section.Abstract", "section.abstract", "#abstract", ".abstractSection", "div.c-article-section__content"):
            node = soup.select_one(selector)
            if node:
                candidate = text_clean(node.get_text(" ", strip=True))
                if len(candidate) >= 120 and len(candidate) < 15000:
                    abstract = candidate
                    break
    if len(abstract) < 100:
        description = meta_content(soup, ["dc.description", "description"])
        if len(description) >= 350 and not re.search(r"(browse|subscribe|sign up|read more)", description[:120], re.I):
            abstract = description
    direct_date = date_parse(meta_content(soup, ["citation_online_date", "citation_date_online"]))
    if not direct_date:
        # Wiley and other publishers sometimes print the first date as visible text.
        # Only consider it after citation_doi on this very page matches the candidate.
        first_text = soup.get_text(" ", strip=True)[:20000]
        matched = re.search(r"\b(?:First published(?: online)?|Published online)\s*:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4}|\d{4}-\d{2}-\d{2})", first_text, re.I)
        if matched:
            direct_date = date_parse(matched.group(1))
    return {"doi_matched": True,
            "abstract": abstract[:9000] if len(abstract) >= 100 else "",
            "online_date": direct_date}


def openalex_abstract(inverted):
    if not inverted or not isinstance(inverted, dict):
        return ""
    positions = [(pos, word) for word, positions in inverted.items() for pos in positions]
    positions.sort()
    return " ".join(word for _, word in positions)


def europepmc_abstract(session, doi):
    """Free biomedical abstract fallback; require a matching DOI, never infer from a title."""
    try:
        response = session.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params={"query": 'DOI:"' + doi + '"', "format": "json", "pageSize": 10},
            timeout=12,
        )
        response.raise_for_status()
        records = response.json().get("resultList", {}).get("result", [])
        for record in records:
            found = str(record.get("doi") or "").lower().strip()
            if found != doi.lower():
                continue
            pmcid = record.get("pmcid")
            if pmcid:
                full = session.get(
                    "https://www.ebi.ac.uk/europepmc/webservices/rest/" + pmcid + "/fullTextXML",
                    timeout=12,
                )
                if full.ok:
                    soup = BeautifulSoup(full.text, "html.parser")
                    abstracts = soup.find_all("abstract")
                    text = text_clean(" ".join(a.get_text(" ", strip=True) for a in abstracts))
                    if len(text) >= 100 and abstract_review(text) == "present_needs_content_review":
                        return text[:9000], "europepmc_fulltext_doi_matched"
            # Europe PMC search result supplies an abstract in some records.
            candidate = text_clean(record.get("abstractText") or "")
            if len(candidate) >= 100 and abstract_review(candidate) == "present_needs_content_review":
                return candidate[:9000], "europepmc_doi_matched"
    except (requests.RequestException, ValueError, TypeError):
        pass
    return "", ""


def enrich(item, session, start, end):
    work = dict(item)
    work["publication_date_is_api_candidate"] = True
    work["article_type_guess"] = kind_guess(work.get("title", ""), work.get("crossref_subtype", ""))
    previous = EARLIER_PUBLICATIONS.get((work.get("doi") or "").lower())
    work["previously_published_by_publisher"] = bool(
        previous and previous["first_publication_prior_to"] <= start)
    if work["previously_published_by_publisher"]:
        work["article_type_guess"] = "prior_publication_not_new"
        work["publisher_verification_evidence"] = previous["publisher_source"]
    # A URL/footnote in the upstream abstract field is not an abstract.
    upstream_status = abstract_review(work.get("abstract", ""))
    if upstream_status.startswith("suspect"):
        work["rejected_upstream_abstract"] = upstream_status
        work["abstract"] = ""
    work["abstract_source"] = "crossref_or_openalex_metadata" if text_clean(work.get("abstract", "")) else "missing"
    work["abstract_attempts"] = []
    work["publisher_online_date"] = None
    work["publisher_doi_matched"] = False
    work["publisher_date_verified_in_window"] = False
    work["publisher_date_conflict"] = False
    work["publisher_check_status"] = "not_checked"
    if work["article_type_guess"] in {"likely_nonresearch", "prior_publication_not_new"}:
        work["publisher_check_status"] = ("skipped_prior_publication" if work["previously_published_by_publisher"] else "skipped_likely_nonresearch")
        return work
    doi = work.get("doi", "")
    work["publisher_pages_attempted"] = []
    work["crossref_online_date"] = None  # Publisher-deposited metadata, NOT first-publication verification.
    for page_url in publisher_urls(work["url"], doi, work.get("date")):
        work["publisher_pages_attempted"].append(page_url)
        work["abstract_attempts"].append("publisher:" + page_url)
        try:
            response = session.get(page_url, timeout=8, headers={"Accept": "text/html,application/xhtml+xml"})
            response.raise_for_status()
            if "html" not in response.headers.get("Content-Type", "").lower():
                work["publisher_check_status"] = "publisher_non_html"
                continue
            result = extract_publisher(response.text[:1300000], doi)
            if not result["doi_matched"]:
                work["publisher_check_status"] = "publisher_doi_not_confirmed"
                continue
            work["publisher_doi_matched"] = True
            work["publisher_check_status"] = "doi_matched"
            work["publisher_evidence_url"] = page_url
            work["publisher_online_date"] = result["online_date"]
            if result["online_date"]:
                work["publisher_date_verified_in_window"] = start <= result["online_date"] <= end
                work["publisher_date_conflict"] = not work["publisher_date_verified_in_window"]
            if result["abstract"] and not text_clean(work.get("abstract", "")):
                work["abstract"] = result["abstract"]
                work["abstract_source"] = "publisher_doi_matched_page"
            if result["online_date"] and text_clean(work.get("abstract", "")):
                break
        except requests.RequestException as exc:
            work["publisher_check_status"] = "publisher_request_failed_" + type(exc).__name__
    if not work["publisher_online_date"]:
        # Crossref published-online is useful as a fallback lead but not proof of FIRST view.
        try:
            api = session.get("https://api.crossref.org/works/" + doi, timeout=8)
            api.raise_for_status()
            meta = api.json().get("message", {})
            # Crossref sometimes exposes an abstract only on the per-DOI record.
            if not text_clean(work.get("abstract", "")):
                candidate = text_clean(BeautifulSoup(meta.get("abstract") or "", "html.parser").get_text(" ", strip=True))
                if len(candidate) >= 100 and abstract_review(candidate) == "present_needs_content_review":
                    work["abstract"] = candidate[:9000]
                    work["abstract_source"] = "crossref_doi_record"
            parts = (meta.get("published-online") or {}).get("date-parts", [[]])[0]
            if len(parts) >= 3:
                work["crossref_online_date"] = dt.date(*parts[:3]).isoformat()
        except (requests.RequestException, ValueError, TypeError, KeyError, OverflowError):
            pass
    if not text_clean(work.get("abstract", "")):
        work["abstract_attempts"].append("openalex_doi")
        try:
            params = {}
            if os.getenv("OPENALEX_API_KEY"):
                params["api_key"] = os.getenv("OPENALEX_API_KEY")
            r = session.get("https://api.openalex.org/works/https://doi.org/" + doi, timeout=12, params=params)
            r.raise_for_status()
            recovered = openalex_abstract(r.json().get("abstract_inverted_index"))
            if len(recovered) >= 100:
                work["abstract"] = recovered[:9000]
                work["abstract_source"] = "openalex"
        except (requests.RequestException, ValueError, TypeError):
            pass
    if not text_clean(work.get("abstract", "")) and (
        doi.lower().startswith("10.1136/") or "med" in work.get("journal", "").lower()
    ):
        work["abstract_attempts"].append("europepmc_doi")
        recovered, evidence = europepmc_abstract(session, doi)
        if recovered:
            work["abstract"] = recovered
            work["abstract_source"] = evidence
    work["abstract_review_status"] = abstract_review(work.get("abstract", ""))
    work["publication_date_evidence_level"] = (
        "publisher_doi_matched" if work["publisher_online_date"]
        else "crossref_published_online_only" if work["crossref_online_date"]
        else "unconfirmed"
    )
    work["requires_manual_verification"] = (
        work["abstract_review_status"] != "present_needs_content_review"
        or work["publication_date_evidence_level"] != "publisher_doi_matched"
        or work["article_type_guess"] == "book_review_candidate"
    )
    return work


def build():
    report = load("latest-report.json")
    source = load("latest-candidates.json")
    now = dt.datetime.now(JST).date()
    start, end = (now - dt.timedelta(days=1)).isoformat(), now.isoformat()
    fetched_on = dt.datetime.fromisoformat(report["checked_at_utc"].replace("Z", "+00:00")).astimezone(JST).date().isoformat()
    current = fetched_on == end and source.get("collected_on") == end
    data = [a for a in source.get("items", []) if start <= a.get("date", "") <= end]
    data.sort(key=lambda a: (a.get("date", ""), a.get("doi", "")))
    session = requests.Session()
    session.headers.update({"User-Agent": "JournalDigest-Metadata/1.1 (academic monitoring; contact: " + os.getenv("CROSSREF_MAILTO", "journal-monitor@example.org") + ")"})
    enriched = [enrich(x, session, start, end) for x in data]
    # The introduced ledger records completed ChatGPT introductions, not mere discovery.
    # Annotate rather than delete records so candidate counts remain auditable.
    ledger_path = STATE / "introduced-dois.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    introduced = {d.lower().removeprefix("https://doi.org/") for d in ledger.get("introduced_dois", {})}
    for item in enriched:
        item["already_introduced"] = (item.get("doi", "").lower().removeprefix("https://doi.org/") in introduced)
    likely = [e for e in enriched if e["article_type_guess"] == "research_candidate_unverified"]
    reviews = [e for e in enriched if e["article_type_guess"] == "book_review_candidate"]
    excluded_nonresearch = sum(e["article_type_guess"] == "likely_nonresearch" for e in enriched)
    excluded_older = sum(e["article_type_guess"] == "prior_publication_not_new" for e in enriched)
    full = {
        "collected_on": source.get("collected_on"),
        "checked_at_utc": report.get("checked_at_utc"),
        "fresh_today_jst": current,
        "window_start_jst": start,
        "window_end_jst": end,
        "journal_total": report.get("journal_total", 0),
        "counts": report.get("counts", {}),
        "major_publishers": report.get("major_publishers", {}),
        "raw_candidate_count": len(data),
        "already_introduced_count": sum(x["already_introduced"] for x in enriched),
        "eligible_candidate_count": sum(not x["already_introduced"] for x in enriched),
        "research_candidate_unverified_count": len(likely),
        "book_review_candidate_count": len(reviews),
        "book_review_abstracts_missing": sum(not bool(text_clean(x.get("abstract", ""))) for x in reviews),
        "likely_nonresearch_count": excluded_nonresearch,
        "excluded_older_publications_count": excluded_older,
        "research_candidate_abstracts_available": sum(bool(text_clean(x.get("abstract", ""))) for x in likely),
        "research_candidate_abstracts_missing": sum(not bool(text_clean(x.get("abstract", ""))) for x in likely),
        "research_candidate_suspect_abstracts": sum(x.get("abstract_review_status", "").startswith("suspect") for x in likely),
        "publisher_first_online_in_window_verified_count": sum(x["publisher_date_verified_in_window"] for x in likely),
        "publisher_first_online_date_conflict_count": sum(x["publisher_date_conflict"] for x in likely),
        "warning": "Machine-discovered candidates, NOT verified First View research papers. Only explicit publisher online-date metadata tied to matching DOI counts as date verification.",
        "items": enriched,
    }
    # Persistent 45-day review queue; first-seen is not proof of introduction.
    # Carry political philosophy candidates forward independently of today's date.
    political_journals = {
        "J026", "J027", "J028", "J031", "J034", "J035", "J036", "J038",
        "J039", "J041", "J042", "J047", "J048", "J050", "J070", "J071",
    }
    backlog = []
    for candidate in source.get("items", []):
        doi = (candidate.get("doi") or "").lower().removeprefix("https://doi.org/")
        if candidate.get("journal_id") not in political_journals or not doi or doi in introduced:
            continue
        backlog.append({
            "doi": doi, "title": candidate.get("title", ""),
            "journal_id": candidate.get("journal_id", ""),
            "crossref_candidate_date": candidate.get("date"),
            "first_seen_jst": candidate.get("first_seen_jst"),
            "status": "unverified_not_introduced",
        })
    backlog.sort(key=lambda item: (item.get("crossref_candidate_date") or "", item["doi"]))
    save("latest-political-backlog.json", {
        "collected_on": source.get("collected_on"),
        "checked_at_utc": report.get("checked_at_utc"),
        "meaning": "45-day metadata candidates, not confirmed First View; verify before introduction.",
        "count": len(backlog), "items": backlog
    })
    full["political_backlog_unverified_count"] = len(backlog)
    save("latest-daily.json", full)
    report["daily_candidate_count"] = len(data)
    report["daily_unverified_research_candidate_count"] = len(likely)
    report["daily_abstracts_missing"] = full["research_candidate_abstracts_missing"]
    save("latest-report.json", report)
    print(json.dumps({k: full[k] for k in ("fresh_today_jst", "raw_candidate_count", "research_candidate_unverified_count", "research_candidate_abstracts_available", "research_candidate_abstracts_missing", "publisher_first_online_in_window_verified_count", "publisher_first_online_date_conflict_count")}, ensure_ascii=False), flush=True)
    return 0 if current else 2


if __name__ == "__main__":
    sys.exit(build())
