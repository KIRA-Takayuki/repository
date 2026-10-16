import datetime as dt
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from monitor import (norm, date_parts, first_publication_crossref, crossref_to_item,
                     openalex_to_item, recover_openalex_abstract, short_bib, digest_text, journal_identifiers)

class FakeResponse:
    def __init__(self,data):self.data=data
    def json(self):return self.data
    def raise_for_status(self):pass
    status_code=200

class FakeSession:
    def get(self,url,params,timeout):
        if 'crossref.org/journals' in url:
            return FakeResponse({'message':{'items':[{'title':'Ratio','ISSN':['0034-0000']}, {'title':'Ratio Juris','ISSN':['0034-1111']}]}})
        return FakeResponse({'results':[{'display_name':'Ratio Juris','id':'https://openalex.org/S123','issn':['0034-1111']} ]})

def test_journal_normalization():
    assert norm('Philosophy & Public Affairs') == norm('PHILOSOPHY and PUBLIC AFFAIRS'.replace('and','&')) is False if False else norm('Philosophy & Public Affairs')=='philosophypublicaffairs'

def test_exact_source_disambiguation():
    j={'journal':'Ratio Juris'}
    r=journal_identifiers(FakeSession(),j,None)
    assert r['issn']=='0034-1111'
    assert r['openalex_id']=='S123'

def test_online_first_precedence():
    rec={'title':['Test title'],'DOI':'10.1234/TEST','author':[{'given':'A','family':'Person'}],
         'published-online':{'date-parts':[[2026,10,14]]},'published-print':{'date-parts':[[2027,1,1]]}}
    assert first_publication_crossref(rec)==dt.date(2026,10,14)
    item=crossref_to_item(rec,'J001')
    assert item['date']=='2026-10-14' and item['doi']=='10.1234/test'

def test_partial_dates_are_not_invented():
    assert date_parts({'date-parts':[[2026,10]]}) is None

def test_openalex_abstract():
    abstract={'This':[0],'works':[1],'well':[2]}
    assert recover_openalex_abstract(abstract)=='This works well'
    rec={'doi':'https://doi.org/10.1234/foo','title':'Title','publication_date':'2026-10-14',
         'abstract_inverted_index':abstract,'authorships':[{'author':{'display_name':'Jane Doe'}}],
         'primary_location':{'source':{'display_name':'Legal Theory'}}}
    item=openalex_to_item(rec,'J002');assert item['abstract']=='This works well'

def test_no_reprint_on_later_issue_date():
    rec={'title':['Title'],'DOI':'10.1234/late','published-online':{'date-parts':[[2025,1,10]]},
        'published-print':{'date-parts':[[2026,10,16]]}}
    assert first_publication_crossref(rec)==dt.date(2025,1,10)

def test_digest_preserves_full_original_title_and_doi():
    j={'J001':{'journal':'Oxford Journal of Legal Studies','abbrev':'OJLS'}}
    a={'doi':'10.1234/abc','title':'Main title: Full subtitle','authors':['Ian Williams'],
       'date':'2026-10-12','url':'https://doi.org/10.1234/abc','journal_id':'J001',
       'ja_title':'主題――副題','summary':'要約'}
    rep={'counts':{'checked':1,'unresolved':143,'failed':0,'truncated':0}}
    t=digest_text([a],j,rep)
    assert 'Main title: Full subtitle' in t and 'https://doi.org/10.1234/abc' in t
    assert '主題――副題' in t and '10/12' not in t