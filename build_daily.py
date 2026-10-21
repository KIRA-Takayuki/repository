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
JST = dt.timezone(dt.timedelta(hours=9))


def load(name):
    return json.loads((STATE / name).read_text(encoding="utf-8"))


def save(name, value):
    (STATE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def kind_guess(title):
    s = " ".join(html.unescape(title or "").split())
    if re.search(r"^(?:book review|publisher correction|correction|retraction|erratum|editorial|announcement)\b", s, re.I):
        return "likely_nonresearch"
    if re.search(r"\bwritten by\b|\beds?\.\s|\bTo Our Guest Reviewers\b|\.\s*By\s+[A-Z][A-Za-z-]+", s, re.I):
        return "likely_nonresearch"
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
    return {"doi_matched": True,
            "abstract": abstract[:9000] if len(abstract) >= 100 else "",
            "online_date": direct_date}


def openalex_abstract(inverted):
    if not inverted or not isinstance(inverted, dict):
        return ""
    positions = [(pos, word) for word, positions in inverted.items() for pos in positions]
    positions.sort()
    return " ".join(word for _, word in positions)


def enrich(item, session, start, end):
    work = dict(item)
    work["publication_date_is_api_candidate"] = True
    work["article_type_guess"] = kind_guess(work.get("title", ""))
    work["abstract_source"] = "crossref_or_openalex_metadata" if text_clean(work.get("abstract", "")) else "missing"
    work["publisher_online_date"] = None
    work["publisher_doi_matched"] = False
    work["publisher_date_verified_in_window"] = False
    work["publisher_date_conflict"] = False
    work["publisher_check_status"] = "not_checked"
    if work["article_type_guess"] == "likely_nonresearch":
        work["publisher_check_status"] = "skipped_likely_nonresearch"
        return work
    doi = work.get("doi", "")
    try:
        response = session.get(work["url"], timeout=12, headers={"Accept": "text/html,application/xhtml+xml"})
        response.raise_for_status()
        if "html" in response.headers.get("Content-Type", "").lower():
            result = extract_publisher(response.text[:1300000], doi)
            work["publisher_doi_matched"] = result["doi_matched"]
            work["publisher_check_status"] = "doi_matched" if result["doi_matched"] else "publisher_doi_not_confirmed"
            if result["doi_matched"]:
                work["publisher_online_date"] = result["online_date"]
                if result["online_date"]:
                    work["publisher_date_verified_in_window"] = start <= result["online_date"] <= end
                    work["publisher_date_conflict"] = not work["publisher_date_verified_in_window"]
                if result["abstract"] and not text_clean(work.get("abstract", "")):
                    work["abstract"] = result["abstract"]
                    work["abstract_source"] = "publisher_doi_matched_page"
        else:
            work["publisher_check_status"] = "publisher_non_html"
    except requests.RequestException as exc:
        work["publisher_check_status"] = "publisher_request_failed_" + type(exc).__name__
    if not text_clean(work.get("abstract", "")):
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
    likely = [e for e in enriched if e["article_type_guess"] != "likely_nonresearch"]
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
        "research_candidate_unverified_count": len(likely),
        "likely_nonresearch_count": len(data) - len(likely),
        "research_candidate_abstracts_available": sum(bool(text_clean(x.get("abstract", ""))) for x in likely),
        "research_candidate_abstracts_missing": sum(not bool(text_clean(x.get("abstract", ""))) for x in likely),
        "publisher_first_online_in_window_verified_count": sum(x["publisher_date_verified_in_window"] for x in likely),
        "publisher_first_online_date_conflict_count": sum(x["publisher_date_conflict"] for x in likely),
        "warning": "Machine-discovered candidates, NOT verified First View research papers. Only explicit publisher online-date metadata tied to matching DOI counts as date verification.",
        "items": enriched,
    }
    save("latest-daily.json", full)
    report["daily_candidate_count"] = len(data)
    report["daily_unverified_research_candidate_count"] = len(likely)
    report["daily_abstracts_missing"] = full["research_candidate_abstracts_missing"]
    save("latest-report.json", report)
    print(json.dumps({k: full[k] for k in ("fresh_today_jst", "raw_candidate_count", "research_candidate_unverified_count", "research_candidate_abstracts_available", "research_candidate_abstracts_missing", "publisher_first_online_in_window_verified_count", "publisher_first_online_date_conflict_count")}, ensure_ascii=False), flush=True)
    return 0 if current else 2


if __name__ == "__main__":
    sys.exit(build())
