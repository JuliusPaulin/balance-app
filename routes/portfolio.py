"""Portfolio views and local-only planning settings."""
from copy import deepcopy
from datetime import date
import math
import re
from flask import Blueprint, jsonify, request
from core import current_user_id
from data import db
from services import portfolio
from services.portfolio_market import load_market, refresh_market
from services.portfolio_math import analyze_risk, frontier

bp=Blueprint('portfolio',__name__)


def _error(message): return jsonify({'error':message}),400

def _finite(v): return type(v) in (int,float) and math.isfinite(v)

def _date(v):
    if not isinstance(v,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',v): return False
    try: date.fromisoformat(v); return True
    except ValueError: return False

def _body():
    data=request.get_json(silent=True)
    return data if isinstance(data,dict) else None


def _validate_model(model):
    if not isinstance(model,dict): return 'model must be an object'
    for field in ('returns','max_weights'):
        vals=model.get(field,{})
        if not isinstance(vals,dict) or any(k not in portfolio.GROUPS for k in vals): return f'{field} contains an unknown market group'
        if any(not _finite(v) or (not -1<=v<=1 if field=='returns' else not 0<=v<=1) for v in vals.values()): return f'{field} must contain finite values within allowed bounds'
    if not _finite(model.get('risk_free')) or not -.99<model['risk_free']<=1: return 'risk_free must be finite and greater than -100%'
    if type(model.get('months')) is not int or not 24<=model['months']<=120: return 'months must be between 24 and 120'
    if type(model.get('include_bonds')) is not bool: return 'include_bonds must be boolean'
    enabled=[g for g in portfolio.GROUPS if g!='Bonds' or model['include_bonds']]
    if sum(model['max_weights'].get(g,1) for g in enabled)<1-1e-9: return 'maximum weights cannot form a fully invested portfolio'
    return None


def _valid_settings(data,conn,uid):
    modes=data.get('account_modes',{})
    if not isinstance(modes,dict): return 'account_modes must be an object'
    accounts={str(r['id']):r for r in conn.execute('SELECT id,type FROM accounts WHERE user_id=%s',(uid,)).fetchall()}
    if any(k not in accounts for k in modes): return 'account_modes contains an unknown account'
    if any(v not in ('investment','excluded','emergency') for v in modes.values()): return 'invalid account mode'
    if any(accounts[k]['type']=='liability' and v=='investment' for k,v in modes.items()): return 'liabilities cannot be investment accounts'
    targets=data.get('targets',{})
    if not isinstance(targets,dict): return 'targets must be an object'
    for basis,values in targets.items():
        if basis not in portfolio.BASES or not isinstance(values,dict): return 'invalid target basis'
        if any(not isinstance(k,str) or not k.strip() or len(k)>80 for k in values): return 'invalid target name'
        if any(k not in portfolio.TARGET_OPTIONS[basis] for k in values): return f'target names must use the {basis} basis'
        if any(not _finite(v) or not 0<=v<=1 for v in values.values()): return 'targets must be finite weights from 0 to 1'
        if values and abs(sum(values.values())-1)>1e-6: return 'targets must sum to 1'
    err=_validate_model(data.get('model'))
    if err: return err
    start,end=data.get('flow_complete_from'),data.get('flow_complete_to')
    if any(v is not None and not _date(v) for v in (start,end)): return 'flow dates must be YYYY-MM-DD'
    if bool(start)!=bool(end) or (start and start>=end): return 'complete period needs an opening date before the closing date'
    if end and end>date.today().isoformat(): return 'complete period cannot end in the future'
    return None


def _merged_model(saved,patch):
    result=deepcopy(saved)
    if not isinstance(patch,dict): return None
    for k,v in patch.items():
        if k in ('returns','max_weights') and isinstance(v,dict): result[k].update(v)
        else: result[k]=v
    return result


@bp.get('/api/portfolio')
def get_portfolio():
    uid=current_user_id()
    with db.db_conn() as conn:
        result=portfolio.portfolio_summary(conn,uid)
        settings=result['settings']; market=load_market(conn); model=settings['model']
        weights=portfolio.group_weights(result['holdings'])
        result['market']=market
        result['risk']=({'available':False,'coverage':0,'reason':result['invalid_reason']} if result['invalid_reason'] else analyze_risk(weights,market,model['risk_free'],model['months']))
        result['coverage']['risk']=result['risk'].get('coverage',0)
        result['history']=portfolio.build_history(conn,uid,settings,market)
        flows=[dict(r) for r in conn.execute('SELECT id,account_id,date,amount FROM portfolio_flows WHERE user_id=%s ORDER BY date,id',(uid,)).fetchall()]
        included={a['id'] for a in result['accounts'] if a['included']}
        result['flows']=flows
        result['recorded_net_flows']=sum(f['amount'] for f in flows if f['account_id'] in included)
        result['performance']=portfolio.performance(conn,uid,settings,flows)
        result['stale_accounts']=[{'name':a['name'],'as_of':a['as_of']} for a in result['accounts'] if a['included'] and a['balance'] and (not a['as_of'] or (date.today()-date.fromisoformat(a['as_of'])).days>45)]
    return jsonify(result)


@bp.put('/api/portfolio/settings')
def put_settings():
    data=_body()
    if data is None: return _error('settings must be an object')
    uid=current_user_id()
    with db.db_conn() as conn:
        current=portfolio.load_settings(conn,uid)
        merged={**current,**data}
        if 'model' in data: merged['model']=_merged_model(current['model'],data['model'])
        err=_valid_settings(merged,conn,uid)
        if err: return _error(err)
        # Inclusion changes require cash-flow completeness to be reviewed again.
        if 'account_modes' in data and data['account_modes']!=current['account_modes']:
            merged['flow_complete_from']=merged['flow_complete_to']=None
        conn.execute('INSERT INTO portfolio_settings (user_id,payload) VALUES (%s,%s) ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload',(uid,db.Json(merged)))
    return jsonify(merged)


@bp.route('/api/portfolio/model',methods=['GET','POST'])
def portfolio_model():
    data=_body() if request.method=='POST' else {}
    if data is None: return _error('model must be an object')
    uid=current_user_id()
    with db.db_conn() as conn:
        settings=portfolio.load_settings(conn,uid)
        model=_merged_model(settings['model'],data.get('model',data))
        err=_validate_model(model)
        if err: return _error(err)
        snapshot=portfolio.snapshot(conn,uid,settings=settings)
        if any(r['value']<0 for r in snapshot['rows']): return jsonify({'available':False,'reason':'Negative included investment balance; model unavailable.','points':[]})
        weights=portfolio.group_weights(snapshot['rows'])
        result=frontier(weights,load_market(conn),model)
        result['assumptions']=model
    return jsonify(result)


@bp.post('/api/portfolio/preview')
def portfolio_preview():
    data=_body()
    if data is None: return _error('preview must be an object')
    amount,steps,key=data.get('amount'),data.get('steps',1),data.get('key')
    if not _finite(amount) or not 0<amount<=1e9: return _error('amount must be positive and finite')
    if type(steps) is not int or not 1<=steps<=120: return _error('steps must be between 1 and 120')
    with db.db_conn() as conn:
        before=portfolio.portfolio_summary(conn,current_user_id())
        if before['invalid_reason']: return _error(before['invalid_reason'])
        target=next((h for h in before['holdings'] if h['key']==key),None)
        if not target or not target['classified']: return _error('choose a listed holding as the contribution destination')
        after_rows=deepcopy(before['holdings'])
        for row in after_rows:
            if row['key']==key: row['value']+=amount*steps
        after_alloc={b:portfolio._allocation(after_rows,b) for b in portfolio.BASES}
        targets=before['settings']['targets']
    return jsonify({'amount':amount,'steps':steps,'before':{'total':before['total'],'allocation':before['allocation']},
                    'after':{'total':before['total']+amount*steps,'allocation':after_alloc},'targets':targets,
                    'drift':portfolio.drift(before['allocation'],targets,after_alloc),
                    'assumptions':'Unchanged market prices and cached fund composition; existing holdings retained. Each contribution buys the selected holding; fees, taxes and unit rounding are excluded.'})


@bp.route('/api/portfolio/flows',methods=['GET','POST'])
def flows():
    uid=current_user_id()
    with db.db_conn() as conn:
        if request.method=='GET': return jsonify([dict(r) for r in conn.execute('SELECT id,account_id,date,amount FROM portfolio_flows WHERE user_id=%s ORDER BY date,id',(uid,)).fetchall()])
        data=_body()
        if data is None: return _error('flow must be an object')
        if not _date(data.get('date')) or data['date']>date.today().isoformat(): return _error('date must be YYYY-MM-DD and not in the future')
        if not _finite(data.get('amount')) or data['amount']==0: return _error('amount must be finite and non-zero')
        settings=portfolio.load_settings(conn,uid)
        if data.get('account_id') not in {a['id'] for a in portfolio.included_accounts(conn,uid,settings)}: return _error('choose an included investment account')
        row=conn.execute('INSERT INTO portfolio_flows (user_id,account_id,date,amount) VALUES (%s,%s,%s,%s) RETURNING id,account_id,date,amount',(uid,data['account_id'],data['date'],data['amount'])).fetchone()
        reset=bool(settings.get('flow_complete_from') and settings['flow_complete_from']<data['date']<=settings['flow_complete_to'])
        if reset:
            settings['flow_complete_from']=settings['flow_complete_to']=None
            conn.execute('UPDATE portfolio_settings SET payload=%s WHERE user_id=%s',(db.Json(settings),uid))
    return jsonify({**dict(row),'completeness_reset':reset}),201


@bp.delete('/api/portfolio/flows/<int:flow_id>')
def delete_flow(flow_id):
    uid=current_user_id()
    with db.db_conn() as conn:
        row=conn.execute('DELETE FROM portfolio_flows WHERE id=%s AND user_id=%s RETURNING id,date',(flow_id,uid)).fetchone()
        settings=portfolio.load_settings(conn,uid)
        if row and settings.get('flow_complete_from') and settings['flow_complete_from']<row['date']<=settings['flow_complete_to']:
            settings['flow_complete_from']=settings['flow_complete_to']=None
            conn.execute('UPDATE portfolio_settings SET payload=%s WHERE user_id=%s',(db.Json(settings),uid))
    return jsonify({'deleted':flow_id}) if row else (jsonify({'error':'flow not found'}),404)


@bp.post('/api/portfolio/refresh')
def refresh_portfolio_market():
    with db.db_conn() as conn: result=refresh_market(conn)
    return jsonify(result)
