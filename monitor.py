"""144-journal incremental digest monitor; fail closed on uncertain journal identity."""
from __future__ import annotations
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter

# The six publishers prioritized by the user (plus University of Chicago's Ethics).
MAJOR_PUBLISHERS = frozenset({'Springer Nature', 'Cambridge University Press', 'Wiley',
                              'Oxford University Press', 'SAGE'})
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'journals.json'
REGISTRY = ROOT / 'state' / 'registry.json'
STATE = ROOT / 'state' / 'state.json'
REPORT = ROOT / 'state' / 'latest-report.json'
DIGEST = ROOT / 'state' / 'latest-digest.txt'
UA = 'JournalMonitor/1.0 (scholarly metadata; contact: ' + os.getenv('CROSSREF_MAILTO', 'journal-monitor@example.org') + ')'


def norm(value: str) -> str:
    t = unicodedata.normalize('NFKD', value or '').casefold()
    t = ''.join(c for c in t if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', t)


def json_read(path: Path, default: Any):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def json_write(path: Path, val: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(val, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def get(session: requests.Session, url: str, params: dict | None = None) -> dict:
    for n in range(4):
        try:
            response = session.get(url, params=params, timeout=25)
            if response.status_code == 429 or response.status_code >= 500:
                raise requests.HTTPError(f'HTTP {response.status_code}: {url}')
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if n == 3:
                raise
            time.sleep(2 ** n)
    raise RuntimeError('unreachable')


def journal_identifiers(session: requests.Session, journal: dict, old: dict | None) -> dict:
    """Identify using exact journal names; ambiguous matches are never accepted."""
    # Publisher-verified ISSN in the manifest overrides unreliable title matching.
    if journal.get('issn'):
        fixed = journal['issn']
        if old and old.get('issn') == fixed and old.get('status') == 'identified':
            return old
        return {'name': journal['journal'], 'issn': fixed, 'issns': [fixed],
                'openalex_id': (old or {}).get('openalex_id'), 'status': 'identified',
                'notes': ['Publisher-verified ISSN in data/journals.json']}
    if old and (old.get('issn') or old.get('openalex_id')):
        return old
    name = journal['journal']
    entry = {'name': name, 'issn': None, 'issns': [], 'openalex_id': None, 'status': 'unresolved', 'notes': []}
    # Some publisher URLs expose an eight-digit ISSN; validate its official label later.
    try:
        results = get(session, 'https://api.crossref.org/journals', {'query': name, 'rows': 15}).get('message', {}).get('items', [])
        matches = [a for a in results if norm(a.get('title', '')) == norm(name)]
        if len(matches) == 1:
            entry['issns'] = matches[0].get('ISSN', [])
            entry['issn'] = entry['issns'][0] if entry['issns'] else None
        elif len(matches) > 1:
            # This can happen when multiple ISSNs are listed under multiple metadata records.
            identifiers = {tuple(sorted(m.get('ISSN', []))) for m in matches}
            if len(identifiers) == 1:
                entry['issns'] = list(next(iter(identifiers)))
                entry['issn'] = entry['issns'][0] if entry['issns'] else None
            else:
                entry['notes'].append('Crossref: ambiguous exact title')
        else:
            entry['notes'].append('Crossref: no exact title match')
    except Exception as exc:
        entry['notes'].append('Crossref lookup error: ' + str(exc)[:160])
    try:
        params={'search': name, 'per_page': 15}
        if os.getenv('OPENALEX_API_KEY'): params['api_key']=os.getenv('OPENALEX_API_KEY')
        results = get(session, 'https://api.openalex.org/sources', params).get('results', [])
        matches = [a for a in results if norm(a.get('display_name', '')) == norm(name)]
        # If ISSN is already trusted, prefer that source over identical-titled unrelated publications.
        if entry['issns']:
            matches = [a for a in matches if set(a.get('issn') or []) & set(entry['issns'])] or matches
        if len(matches) == 1:
            entry['openalex_id'] = matches[0]['id'].split('/')[-1]
            if not entry['issn']:
                entry['issns'] = matches[0].get('issn') or []
                entry['issn'] = matches[0].get('issn_l') or (entry['issns'][0] if entry['issns'] else None)
        elif len(matches) > 1:
            entry['notes'].append('OpenAlex: ambiguous exact title')
        else:
            entry['notes'].append('OpenAlex: no exact title match')
    except Exception as exc:
        entry['notes'].append('OpenAlex lookup error: ' + str(exc)[:160])
    if entry['issn'] or entry['openalex_id']:
        entry['status'] = 'identified'
    return entry


def date_parts(obj: dict | None):
    if not obj or not obj.get('date-parts'):
        return None
    p = obj['date-parts'][0]
    if len(p) < 3:
        return None  # not safe to invent publication day
    try:
        return dt.date(*p[:3])
    except (TypeError, ValueError):
        return None


def first_publication_crossref(work: dict) -> dt.date | None:
    # Online First takes precedence over later print/issue publication.
    if work.get('published-online'):
        return date_parts(work['published-online'])
    # Some publishers provide only "published" or "issued"; never use creation/deposit date.
    if work.get('published-print'):
        return date_parts(work['published-print'])
    return date_parts(work.get('published')) or date_parts(work.get('issued'))


def crossref_candidates(session: requests.Session, issn: str, start: dt.date, end: dt.date):
    cursor = '*'
    out = []
    for _ in range(8):  # bounded; overflow is explicitly reported, not silently ignored
        msg = get(session, f'https://api.crossref.org/journals/{issn}/works', {
            'filter': f'from-pub-date:{start.isoformat()},until-pub-date:{end.isoformat()},type:journal-article',
            'rows': 100, 'cursor': cursor,
        }).get('message', {})
        items = msg.get('items', [])
        out.extend(items)
        if len(items) < 100:
            return out, False
        nxt = msg.get('next-cursor')
        if not nxt or nxt == cursor:
            return out, True
        cursor = nxt
    return out, True


def openalex_candidates(session: requests.Session, source_id: str, start: dt.date, end: dt.date):
    works = []
    page = 1
    for _ in range(8):
        params={'filter': f'primary_location.source.id:{source_id},from_publication_date:{start.isoformat()},to_publication_date:{end.isoformat()}',
            'per_page': 100, 'page': page}
        if os.getenv('OPENALEX_API_KEY'): params['api_key']=os.getenv('OPENALEX_API_KEY')
        js = get(session, 'https://api.openalex.org/works', params)
        chunk = js.get('results', [])
        works += chunk
        if len(chunk) < 100:
            return works, False
        page += 1
    return works, True


def crossref_to_item(record: dict, jid: str) -> dict | None:
    title = html.unescape(re.sub('<[^>]+>', '', ' '.join(record.get('title') or []))).strip()
    doi = (record.get('DOI') or '').lower().strip()
    when = first_publication_crossref(record)
    if not title or not doi or not when:
        return None
    if record.get('subtype') in ('review', 'book-review') or re.search(r'^(erratum|correction|retraction|editorial|book review)\b',title,re.I):
        return None
    authors = []
    for a in record.get('author', []):
        authors.append(' '.join(filter(None, [a.get('given'), a.get('family')])) or a.get('name') or '')
    abstract = BeautifulSoup(record.get('abstract', ''), 'html.parser').get_text(' ', strip=True)
    return {'doi':doi,'title':title,'authors':list(filter(None,authors)), 'date':when.isoformat(), 'journal_id':jid,
            'url':'https://doi.org/'+doi,'abstract':abstract,'origin':'crossref','journal':(record.get('container-title') or [''])[0]}


def recover_openalex_abstract(inverted):
    if not inverted:
        return ''
    words=[]
    for word, locs in inverted.items():
        for i in locs:
            words.append((i,word))
    return ' '.join(w for _,w in sorted(words))


def openalex_to_item(record: dict, jid: str) -> dict | None:
    doi=(record.get('doi') or '').removeprefix('https://doi.org/').lower()
    title=record.get('title') or ''
    when=record.get('publication_date') or ''
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',when) or not doi or not title:
        return None
    authors=[a.get('author',{}).get('display_name','') for a in record.get('authorships',[])]
    return {'doi':doi,'title':title,'authors':[a for a in authors if a],'date':when,
            'journal_id':jid,'url':'https://doi.org/'+doi,
            'abstract':recover_openalex_abstract(record.get('abstract_inverted_index')),
            'origin':'openalex','journal':(record.get('primary_location') or {}).get('source',{}).get('display_name','')}


def is_in_range(item: dict, start: dt.date, end: dt.date):
    try: return start <= dt.date.fromisoformat(item['date']) <= end
    except (TypeError, ValueError): return False


def short_bib(item: dict, journal: dict) -> str:
    names=item['authors']
    if not names:
        who='著者未収録'
    else:
        def fmt(n):
            parts=n.split()
            return (parts[-1]+', '+ ''.join(p[0]+'.' for p in parts[:-1])) if len(parts)>1 else n
        who=fmt(names[0]) + (' et al.' if len(names)>2 else (' & '+fmt(names[1]) if len(names)==2 else ''))
    abbrev=journal.get('abbrev') or journal['journal']
    return f'{who} ({item["date"][:4]}). {item["title"]}. {abbrev}.'


def digest_text(entries: list[dict], journals: dict, report: dict):
    today=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date().isoformat()
    lines=[f'学術誌・論文メタデータ候補｜{today}',f'要確認の候補：{len(entries)}本','',
           '※翻訳・要約・出版社による初回公開日の照合は、ChatGPT内で別途実施する。',
           '※このファイルはメール本文ではなく調査候補一覧である。','']
    for n,item in enumerate(entries,1):
        j=journals[item['journal_id']]
        lines += [f'{n}. {item["title"]}',short_bib(item,j),item['url'],
                  'Abstract (original): '+(item.get('abstract') or 'Not available'), '']
    counts=report['counts']
    major=report.get('major_publishers',{})
    lines += ['【調査状況】',
              f'大手6社80誌：API照会成功 {major.get("checked",0)}誌／未解決・取得失敗 {80-major.get("checked",0)}誌。',
              f'全144誌：API照会成功 {counts.get("checked",0)}誌／識別子未解決 {counts.get("unresolved",0)}誌／取得失敗 {counts.get("failed",0)}誌／件数上限 {counts.get("truncated",0)}誌。',
              'これはCrossref/OpenAlex等の機械照会状況であり、出版社新着ページの直接確認ではない。',
              '初回オンライン公開日と研究論文かどうかを検証した後にのみ新着として紹介する。']
    return '\n'.join(lines)+'\n'


def run(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--initialize-ids',action='store_true')
    parser.add_argument('--days',type=int,default=45)
    parser.add_argument('--limit-journals',type=int,default=0)
    opt=parser.parse_args(argv)
    source=json_read(DATA,{})
    assert source.get('total')==144 and len(source['journals'])==144, 'journal manifest must contain 144'
    seen_ids=[j['id'] for j in source['journals']]
    assert len(set(seen_ids))==144
    jmap={j['id']:j for j in source['journals']}
    reg=json_read(REGISTRY,{})
    state=json_read(STATE,{'sent_dois':{},'first_run':True})
    session=requests.Session(); session.headers.update({'User-Agent':UA, 'Accept':'application/json'})

    now=dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    # Initial run is strictly bounded; later runs look back to catch late metadata updates.
    start=now - dt.timedelta(days=7 if state.get('first_run',True) else max(1,opt.days))
    status={};candidates={};errors=[]
    # Stable partition: all 80 major-publisher titles are queried before the other 64.
    def is_priority(j):
        return j['platform'] in MAJOR_PUBLISHERS or j['journal'] == 'Ethics'
    ordered_journals = sorted(source['journals'], key=lambda j: 0 if is_priority(j) else 1)
    assert sum(is_priority(j) for j in ordered_journals) == 80
    journals=ordered_journals[:opt.limit_journals] if opt.limit_journals else ordered_journals
    for index,j in enumerate(journals,1):
        jid=j['id']
        ent=journal_identifiers(session,j,reg.get(jid))
        reg[jid]=ent
        if opt.initialize_ids:
            status[jid]={'status':ent['status'],'count':0}
            continue
        if ent['status']!='identified':
            status[jid]={'status':'unresolved','detail':'; '.join(ent['notes'][:3])};continue
        found=[]; success=False; truncated=False; failures=[]
        if ent.get('issn'):
            # An online ISSN and a print ISSN may be separately registered.
            # Try alternative identifiers if the primary one returns zero records.
            identifiers = list(dict.fromkeys([ent['issn']] + (ent.get('issns') or [])))
            for issn in identifiers:
                try:
                    rs,overflow=crossref_candidates(session,issn,start,now)
                    found += [x for r in rs if (x:=crossref_to_item(r,jid)) and is_in_range(x,start,now)]
                    success=True;truncated |= overflow
                    if rs:
                        break
                except Exception as exc:failures.append('Crossref '+str(exc)[:110])
        # OpenAlex is optional: without an API key its rate limits had caused
        # many HTTP 429 errors. Crossref remains available without an OpenAI API.
        if ent.get('openalex_id') and os.getenv('OPENALEX_API_KEY'):
            try:
                rs,overflow=openalex_candidates(session,ent['openalex_id'],start,now)
                found += [x for r in rs if (x:=openalex_to_item(r,jid)) and is_in_range(x,start,now)]
                success=True;truncated |= overflow
            except Exception as exc:failures.append('OpenAlex '+str(exc)[:110])
        status[jid]={'status':'truncated' if truncated else ('checked' if success else 'failed'),
                     'candidate_count':len(found),'errors':failures}
        for it in found:
            if not it['doi']:continue
            existing=candidates.get(it['doi'])
            if not existing or (not existing.get('abstract') and it.get('abstract')) or (it['origin']=='crossref' and existing['origin']!='crossref'):
                candidates[it['doi']]=it
        if index % 20==0:
            print(f'checked {index}/{len(journals)} journals',flush=True)
    json_write(REGISTRY,reg)
    counts=Counter(x['status'] for x in status.values())
    report={'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(), 'window_start':start.isoformat(),
            'window_end':now.isoformat(),'counts':dict(counts),'journal_status':status,
            'possible_new_count':len(candidates),'mail_sent':False,'dry_run':opt.dry_run}
    # Persist diagnostic report even if later mail sending fails.
    json_write(REPORT,report)
    if opt.initialize_ids:
        return 0
    # Metadata collection only: this workflow never calls OpenAI or sends mail.
    new=[c for doi,c in candidates.items() if doi not in state.get('sent_dois',{})]
    new.sort(key=lambda a:(a['date'],a['doi']))
    major_ids={j['id'] for j in source['journals'] if is_priority(j)}
    major_status={jid:v for jid,v in status.items() if jid in major_ids}
    report['major_publishers']={
        'total':80,
        'checked':sum(1 for v in major_status.values() if v['status']=='checked'),
        'unresolved':sum(1 for v in major_status.values() if v['status']=='unresolved'),
        'failed':sum(1 for v in major_status.values() if v['status']=='failed'),
        'truncated':sum(1 for v in major_status.values() if v['status']=='truncated')}
    report['major_unchecked']=[{'journal':jmap[jid]['journal'], 'status':v['status'],
                               'detail':v.get('detail') or '; '.join(v.get('errors',[]))}
                              for jid,v in major_status.items() if v['status'] != 'checked']
    report['candidate_count']=len(new)
    report['mode']='metadata_only_no_OpenAI_API_no_Gmail'
    report['publisher_pages_verified']=False
    report['mail_sent']=False
    # Preserve every candidate and full original abstracts for subsequent
    # publisher verification, deduplication, and Japanese summaries in ChatGPT.
    json_write(ROOT/'state'/'latest-candidates.json',
               {'collected_on':now.isoformat(),'window_start':start.isoformat(),
                'window_end':now.isoformat(),'needs_publisher_verification':True,
                'items':new})
    DIGEST.write_text(digest_text(new,jmap,report),encoding='utf-8')
    state['first_run']=False
    state['last_metadata_scan']=now.isoformat()
    json_write(STATE,state)
    json_write(REPORT,report)
    print(json.dumps({'counts':dict(counts),'major':report['major_publishers'],
                      'candidates':len(new),'mail_sent':False},ensure_ascii=False),flush=True)
    return 0

if __name__ == '__main__':
    sys.exit(run())