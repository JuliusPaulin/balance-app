"""Local portfolio valuations, allocation and cash-flow eligibility."""
from collections import defaultdict
from copy import deepcopy
from datetime import date

from data import db
from services.portfolio_catalog import CATALOG

BASES = ('mandate', 'asset_class', 'country', 'region')
GROUPS = ('World', 'US', 'Europe', 'EM', 'Finland', 'Norway', 'Sweden', 'Apple', 'Bonds')
DEFAULT_MODEL = {'returns': {g: .035 if g == 'Bonds' else .07 for g in GROUPS},
                 'max_weights': {g: 1.0 for g in GROUPS}, 'risk_free': .018,
                 'months': 60, 'include_bonds': True}
REGIONS = {
    **dict.fromkeys(('Finland','Sweden','Norway','Denmark','Iceland'), 'Nordics'),
    **dict.fromkeys(('United States','Canada'), 'North America'),
    **dict.fromkeys(('United Kingdom','France','Germany','Switzerland','Netherlands','Spain','Italy','Belgium','Austria','Ireland','Portugal','Greece','Poland','Luxembourg','Czech Republic'), 'Europe ex Nordics'),
    **dict.fromkeys(('Japan','Australia','New Zealand','Singapore','Hong Kong','China','Taiwan','South Korea','India','Indonesia','Malaysia','Thailand','Philippines'), 'Asia Pacific'),
    **dict.fromkeys(('Brazil','Mexico','Chile','Colombia','Peru'), 'Latin America'),
    **dict.fromkeys(('South Africa','Saudi Arabia','United Arab Emirates','Israel','Turkey','Qatar','Kuwait'), 'Middle East & Africa'),
}
TARGET_OPTIONS = {'mandate': (*GROUPS, 'Unknown'),
                  'asset_class': ('Equities', 'Bonds', 'Unknown'),
                  'country': (*sorted(REGIONS), 'Unknown'),
                  'region': (*sorted(set(REGIONS.values())), 'Unknown')}


def default_mode(account):
    if account.get('type') != 'asset': return 'excluded'
    name = (account.get('name') or '').lower()
    if any(w in name for w in ('pension','eläke','investment','sijoitus','fund','rahasto')): return 'investment'
    if any(w in name for w in ('cash','checking','current','käyttö','säästö','savings','tili','talletus','bank')): return 'emergency'
    return 'excluded'


def load_settings(conn, uid):
    row = conn.execute('SELECT payload FROM portfolio_settings WHERE user_id = %s', (uid,)).fetchone()
    payload = db.load_json(row['payload']) if row else {}
    payload = payload if isinstance(payload, dict) else {}
    model = deepcopy(DEFAULT_MODEL)
    stored = payload.get('model', {})
    for key in model:
        if key in stored:
            if isinstance(model[key], dict): model[key].update(stored[key])
            else: model[key] = stored[key]
    return {'account_modes': payload.get('account_modes', {}), 'targets': payload.get('targets', {}),
            'model': model, 'flow_complete_from': payload.get('flow_complete_from'),
            'flow_complete_to': payload.get('flow_complete_to')}


def account_mode(account, settings, has_holdings=False):
    if account.get('type') != 'asset': return 'excluded'
    explicit = settings.get('account_modes', {}).get(str(account['id']))
    if explicit in ('investment','excluded','emergency'): return explicit
    return 'investment' if has_holdings else default_mode(account)


def _accounts(conn, uid):
    return [dict(r) for r in conn.execute('SELECT a.*, EXISTS(SELECT 1 FROM holdings h WHERE h.account_id=a.id) AS has_holdings FROM accounts a WHERE a.user_id=%s ORDER BY a.name', (uid,)).fetchall()]


def included_accounts(conn, uid, settings):
    return [a for a in _accounts(conn, uid) if account_mode(a, settings, a['has_holdings']) == 'investment']


def _balance_at(conn, account_id, as_of):
    return conn.execute('SELECT * FROM account_balances WHERE account_id=%s AND as_of<=%s ORDER BY as_of DESC LIMIT 1', (account_id, as_of)).fetchone()


def _allocation(rows, basis):
    if any(r["value"] < 0 for r in rows): return []
    totals = defaultdict(float)
    for row in rows:
        value = row['value']
        if basis in ('country','region'):
            known = 0.0
            for country, fraction in (row.get('countries') or {}).items():
                fraction = float(fraction)
                if fraction < 0: continue
                label = country if basis == 'country' else REGIONS.get(country, 'Unknown')
                totals[label] += value * fraction; known += fraction
            totals['Unknown'] += value * max(0, 1-known)
        else: totals[row.get(basis) or 'Unknown'] += value
    total = sum(r['value'] for r in rows)
    return [{'name': k, 'value': round(v, 2), 'weight': v/total if total else 0}
            for k,v in sorted(totals.items(), key=lambda x:(-x[1],x[0])) if v > 1e-9]


def _unknown(account, balance, as_of, warning):
    return {'key': f"{account['id']}:unknown", 'name': f"{account['name']} — unclassified", 'value': balance,
            'account_id': account['id'], 'account_name': account['name'], 'as_of': as_of,
            'mandate': 'Unknown', 'asset_class': 'Unknown', 'countries': {}, 'annual_cost': None,
            'classified': False, 'warning': warning, 'note': warning, 'estimated': False}


def snapshot(conn, uid, as_of=None, settings=None):
    settings = settings if settings is not None else load_settings(conn, uid)
    accounts = _accounts(conn, uid)
    today = date.today().isoformat()
    if as_of is None:
        dates = [r['d'] for a in accounts if account_mode(a, settings, a['has_holdings'])=='investment'
                 for r in [conn.execute('SELECT MAX(as_of) AS d FROM account_balances WHERE account_id=%s AND as_of<=%s', (a['id'],today)).fetchone()] if r['d']]
        as_of = max(dates) if dates else today
    rows, payload, total = [], [], 0.0
    for a in accounts:
        bal = _balance_at(conn,a['id'],as_of)
        balance = float(bal['balance']) if bal else 0
        mode = account_mode(a,settings,a['has_holdings'])
        payload.append({'id':a['id'],'name':a['name'],'type':a['type'],'balance':balance,'as_of':bal['as_of'] if bal else None,
                        'included':mode=='investment','emergency':mode=='emergency','mode':mode,
                        'default_included':account_mode(a,{'account_modes':{}},a['has_holdings'])=='investment'})
        if mode!='investment' or not bal or balance==0: continue
        total += balance
        if balance < 0:
            rows.append(_unknown(a,balance,bal['as_of'],'Negative asset balance; allocation and risk unavailable.')); continue
        holdings = [dict(h) for h in conn.execute('SELECT * FROM holdings WHERE account_id=%s AND as_of=%s ORDER BY value_eur DESC', (a['id'],bal['as_of'])).fetchall()]
        covered = sum(float(h['value_eur']) for h in holdings)
        if not holdings or covered > balance+.011 or any(h['value_eur']<0 for h in holdings):
            rows.append(_unknown(a,balance,bal['as_of'],'Holdings do not match this valuation.' if holdings else 'Holdings are missing or dated differently from the balance.')); continue
        start = len(rows)
        for h in holdings:
            meta = CATALOG.get(h['name'], {'mandate':'Unknown','asset_class':'Unknown','countries':{},'annual_cost':None,'estimated':True,'note':'Instrument composition is unavailable.'})
            rows.append({**meta,'source':meta.get('source_url'),'key':f"{a['id']}:{h['name']}",'name':h['name'],'isin':h['isin'],
                         'value':float(h['value_eur']),'account_id':a['id'],'account_name':a['name'],'as_of':h['as_of'],'classified':True})
        diff = balance-covered
        if abs(diff)<=.011:
            rows[start]['value'] += diff
            rows[start]['rounding_adjustment'] = diff
        elif diff>0: rows.append(_unknown(a,diff,bal['as_of'],'Account balance not explained by listed holdings.'))
    valid = not any(r["value"] < 0 for r in rows)
    for r in rows: r['weight'] = r['value']/total if total>0 and valid else 0
    return {'date':as_of if rows else None,'total':round(total,2),'rows':rows,'accounts':payload}


def valuation_dates(conn,uid,settings=None):
    settings = settings if settings is not None else load_settings(conn,uid)
    dates = set(); today=date.today().isoformat()
    for a in included_accounts(conn,uid,settings):
        dates.update(r['d'] for r in conn.execute('SELECT DISTINCT as_of AS d FROM account_balances WHERE account_id=%s AND as_of<=%s',(a['id'],today)).fetchall())
    return sorted(dates)


def group_weights(rows):
    groups=defaultdict(float)
    for r in rows: groups[r.get('mandate') or 'Unknown'] += r['weight']
    return dict(groups)


def drift(allocation, targets, after=None):
    result={}
    for basis in BASES:
        current={r['name']:r['weight'] for r in allocation.get(basis,[])}
        future={r['name']:r['weight'] for r in (after or allocation).get(basis,[])}
        goal=targets.get(basis,{})
        result[basis]=[{'name':n,'current':current.get(n,0),'after':future.get(n,0),'target':goal.get(n,0) if goal else None,
                        'gap':future.get(n,0)-goal.get(n,0) if goal else None}
                       for n in sorted(set(current)|set(future)|set(goal))]
    return result


def portfolio_summary(conn,uid,settings=None):
    settings=settings if settings is not None else load_settings(conn,uid)
    s=snapshot(conn,uid,settings=settings); rows=s['rows']; total=s['total']
    invalid = any(r['value'] < 0 for r in rows)
    allocation={k:_allocation(rows,k) for k in BASES}
    known_cost=sum(r['value'] for r in rows if r.get('annual_cost') is not None)
    costs=sum(r['value']*r['annual_cost'] for r in rows if r.get('annual_cost') is not None)
    listed=sum(r['value'] for r in rows if r['classified'])
    combined=defaultdict(float)
    for r in rows: combined[r.get('isin') or r['name']] += r['weight']
    hhi=sum(w*w for w in combined.values())
    overlap=[]; mandates=defaultdict(list)
    for r in rows:
        if r.get('mandate') not in ('Unknown','Apple'): mandates[r.get('mandate')].append(r['name'])
    for name,names in mandates.items():
        if len(set(names))>1: overlap.append(f"{name}: {len(set(names))} funds share a market mandate; company-level overlap is not fully quantified.")
    if 'World' in mandates and 'US' in mandates: overlap.append('World funds also hold US equities. Separate US funds increase that exposure.')
    if any(r.get('mandate')=='Apple' for r in rows) and 'World' in mandates: overlap.append('Apple is held directly and through World and US funds. Fund counts overstate independent diversification.')
    return {'invalid_reason': 'Negative included investment balance: allocation, risk and frontier are unavailable. Review account roles and balances.' if invalid else None, 'total':total,'listed_value':round(listed,2),'valuation_date':s['date'],'valuation_dates':valuation_dates(conn,uid,settings),
            'holdings':rows,'accounts':s['accounts'],'allocation':allocation,
            'coverage':{'holdings':listed/total if total>0 and not invalid else 0,'country':sum(r['value']*sum((r.get('countries') or {}).values()) for r in rows)/total if total>0 and not invalid else 0,
                        'cost':known_cost/total if total>0 and not invalid else 0,'risk':0},
            'annual_cost_eur':costs if known_cost else None,'cost_known_value':known_cost,
            'concentration':{'largest':max(combined.values(),default=0),'holding_hhi':hhi,'effective_holdings':1/hhi if hhi else None,'overlap':overlap},
            'settings':settings,'target_options':TARGET_OPTIONS,'drift':drift(allocation,settings['targets'])}


def build_history(conn,uid,settings,market=None):
    from services.portfolio_math import analyze_risk
    history=[]
    for d in valuation_dates(conn,uid,settings):
        s=snapshot(conn,uid,d,settings)
        risk=({'available':False,'coverage':0,'reason':'Negative investment balance.'} if any(r['value']<0 for r in s['rows']) else analyze_risk(group_weights(s['rows']),market or {},settings['model']['risk_free'],settings['model']['months']))
        history.append({'date':d,'value':s['total'],'allocation':{k:_allocation(s['rows'],k) for k in BASES},'risk':risk})
    return history


def performance(conn,uid,settings,flows):
    start,end=settings.get('flow_complete_from'),settings.get('flow_complete_to')
    result={'available':False,'reason':'Complete dated investment cash flows and matching opening/closing valuations are needed. Value changes include contributions.'}
    if not start or not end or start>=end: return result
    ids={a['id'] for a in included_accounts(conn,uid,settings)}
    for aid in ids:
        for boundary in (start,end):
            bal=_balance_at(conn,aid,boundary)
            if not bal or (bal['as_of']!=boundary and bal['balance']!=0):
                result['reason']='Every included account needs an actual valuation at both period boundaries. Carried-forward values cannot establish returns.'; return result
    opening=snapshot(conn,uid,start,settings); closing=snapshot(conn,uid,end,settings)
    if any(r['value']<0 for r in opening['rows']+closing['rows']): return result
    before=opening['total']; after=closing['total']
    if before<=0 or not ids: return result
    selected=[f for f in flows if f['account_id'] in ids and start<f['date']<=end]
    net=sum(f['amount'] for f in selected)
    from services.portfolio_math import xirr
    mwr=xirr([(start,-before)]+[(f['date'],-f['amount']) for f in selected]+[(end,after)])
    return {'available':True,'start':start,'end':end,'opening_value':before,'closing_value':after,'net_flows':net,'gains':after-before-net,
            'mwr':mwr,'reason':None,'method':'Money-weighted annual XIRR; end-of-day valuations; opening-date flows already included. Cash-flow completeness is user-confirmed.'}
