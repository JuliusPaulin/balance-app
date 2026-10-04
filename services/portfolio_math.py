"""Aligned EUR proxy risk and inspectable long-only mean/variance models."""
import math
from datetime import date
from collections import defaultdict
import numpy as np
from scipy.optimize import minimize, linprog, brentq


def _finite(v):
    try: return math.isfinite(float(v))
    except (TypeError,ValueError): return False


def _source(market): return market.get('returns',{}) if isinstance(market,dict) else {}


def _contiguous(dates):
    run=[]
    for d in sorted(dates):
        try:
            y,m=map(int,d.split('-'))
            if not 1<=m<=12: continue
        except (TypeError,ValueError): continue
        if run:
            py,pm=map(int,run[-1].split('-'))
            if y*12+m!=py*12+pm+1: run=[]
        run.append(d)
    return run


def _aligned(names,source,months):
    if not names: return [],np.empty((0,0))
    dates=_contiguous(set.intersection(*(set(source[g]) for g in names)))[-months:]
    matrix=np.asarray([[source[g][d] for g in names] for d in dates],dtype=float).reshape((len(dates),len(names)))
    if not np.isfinite(matrix).all() or (matrix<=-1).any(): return [],np.empty((0,len(names)))
    return dates,matrix


def analyze_risk(group_weights,market,risk_free=.018,months=60):
    source=_source(market)
    requested={g:float(w) for g,w in group_weights.items() if _finite(w) and w>0}
    names=[g for g in requested if source.get(g)]
    mass=sum(requested.values()); covered=sum(requested[g] for g in names)
    coverage=covered/mass if mass else 0
    weights=np.array([requested[g]/covered for g in names]) if covered else np.array([])
    base={'available':False,'reason':'At least 24 consecutive aligned monthly observations are required.','coverage':coverage,
          'weights':dict(zip(names,map(float,weights))),'kind':'Hypothetical monthly rebalanced proxy backtest; covered investments normalised to 100%.',
          'refreshed_at':market.get('refreshed_at') if isinstance(market,dict) else None}
    dates,matrix=_aligned(names,source,int(months))
    base['n']=len(dates)
    if len(dates)<24 or not _finite(risk_free) or risk_free<=-1: return base
    monthly=matrix@weights
    sd=float(np.std(monthly,ddof=1)); vol=sd*math.sqrt(12)
    wealth=np.cumprod(np.r_[1.0,1+monthly])
    drawdown=wealth/np.maximum.accumulate(wealth)-1
    rf_month=(1+risk_free)**(1/12)-1
    return {**base,'available':True,'reason':None,'volatility':vol,'max_drawdown':float(-drawdown.min()),
            'sharpe':float((monthly.mean()-rf_month)/sd*math.sqrt(12)) if sd>1e-12 else None,
            'historical_arithmetic_return':float(monthly.mean()*12),'start':dates[0],'end':dates[-1]}


def _optimise(cov,er,caps,target=None):
    n=len(er)
    aeq=np.ones((1,n)); beq=np.array([1.0])
    bounds=[(0,float(c)) for c in caps]
    if target is not None:
        if np.ptp(er)<1e-10:
            if abs(float(er[0])-target)>1e-8: return None
        else:
            # At an extreme return, fix the optimal linear-program face.
            # A redundant return equality plus active zero bounds can make
            # SLSQP stop at its initial vertex instead of minimising risk.
            on_face=False
            for direction in (1,-1):
                ordered=sorted(range(n),key=lambda i:direction*er[i],reverse=True)
                remaining=1.0; extreme=0.0; threshold=None
                for i in ordered:
                    take=min(remaining,caps[i]); extreme+=take*er[i]; remaining-=take
                    if remaining<=1e-10: threshold=er[i]; break
                if threshold is not None and abs(extreme-target)<1e-10:
                    bounds=[(float(c),float(c)) if direction*(e-threshold)>1e-10 else
                            (0,0) if direction*(e-threshold)<-1e-10 else (0,float(c))
                            for e,c in zip(er,caps)]
                    on_face=True; break
            if not on_face: aeq=np.vstack([aeq,er]); beq=np.r_[beq,target]
    feasible=linprog(np.zeros(n),A_eq=aeq,b_eq=beq,bounds=bounds,method='highs')
    if not feasible.success: return None
    constraints={'type':'eq','fun':lambda w: aeq@w-beq,'jac':lambda w:aeq}
    result=minimize(lambda w:float(w@cov@w),feasible.x,jac=lambda w:2*cov@w,method='SLSQP',
                    bounds=bounds,constraints=constraints,options={'ftol':1e-12,'maxiter':500})
    w=result.x
    if not result.success or not np.isfinite(w).all() or np.max(np.abs(aeq@w-beq))>1e-7 or (w< -1e-8).any() or (w>caps+1e-8).any(): return None
    return w


def frontier(group_weights,market,model):
    source=_source(market); expected=model.get('returns',{}); limits=model.get('max_weights',{})
    names=[g for g in expected if source.get(g) and (g!='Bonds' or model.get('include_bonds',True))]
    missing=[g for g in expected if not source.get(g) and (g!='Bonds' or model.get('include_bonds',True))]
    fail={'available':False,'points':[],'reason':'At least 24 consecutive common monthly observations are required.','missing_groups':missing}
    if not names: return fail
    er=np.array([expected[g] for g in names],dtype=float); caps=np.array([limits.get(g,1) for g in names],dtype=float)
    if not np.isfinite(er).all() or not np.isfinite(caps).all() or (caps<0).any() or (caps>1).any() or caps.sum()<1-1e-8:
        return {**fail,'reason':'Allocation limits do not permit a fully invested long-only portfolio.'}
    dates,matrix=_aligned(names,source,int(model.get('months',60)))
    if len(dates)<24: return fail
    cov=np.atleast_2d(np.cov(matrix,rowvar=False,ddof=1))*12
    requested={g:float(w) for g,w in group_weights.items() if _finite(w) and w>0}
    mass=sum(requested.values()); raw=np.array([requested.get(g,0) for g in names]); covered=float(raw.sum())
    if not covered: return {**fail,'reason':'No current investments have a covered proxy in the chosen model.'}
    cur=raw/covered
    def point(w,label=None,returns=er,covariance=cov):
        variance=max(0,float(w@covariance@w)); mean=float(w@returns)
        return {'return':mean,'volatility':math.sqrt(variance),'weights':dict(zip(names,map(float,w))),
                'compound_approx':mean-variance/2,'label':label}
    current=point(cur,'Current covered investments')
    gmv=_optimise(cov,er,caps)
    maxmean=linprog(-er,A_eq=np.ones((1,len(names))),b_eq=[1],bounds=[(0,float(c)) for c in caps],method='highs')
    if gmv is None or not maxmean.success: return {**fail,'reason':'Optimisation failed; no weights are reported.'}
    points=[]
    for target in np.linspace(float(gmv@er),float(maxmean.x@er),25):
        w=_optimise(cov,er,caps,float(target))
        if w is not None:
            p=point(w)
            if not points or abs(points[-1]['return']-p['return'])>1e-8: points.append(p)
    same=_optimise(cov,er,caps,current['return'])
    same_result={'available':False,'reason':'Current assumed return is infeasible under the allocation limits.'}
    if same is not None:
        p=point(same)
        same_result={'available':True,'return':p['return'],'volatility':p['volatility'],'risk_reduction':current['volatility']-p['volatility'],'alternative':p['weights']}
    benchmark=None; alternative=None
    if 'World' in names:
        w=np.zeros(len(names));w[names.index('World')]=1;benchmark=point(w,'World equity benchmark')
        if 'Bonds' in names:
            w[names.index('World')]=.8;w[names.index('Bonds')]=.2;alternative=point(w,'Hypothetical 80% World / 20% bonds')
    sensitivity={}
    if len(dates)>=36:
        recentcov=np.atleast_2d(np.cov(matrix[-36:],rowvar=False,ddof=1))*12
        rw=_optimise(recentcov,er,caps,current['return'])
        sensitivity['last36_months']={'n':36,'current_volatility':point(cur,covariance=recentcov)['volatility'],
            'frontier_volatility':point(rw,covariance=recentcov)['volatility'] if rw is not None else None}
    if 'World' in names:
        for label,change in (('world_expected_minus_1ppt',-.01),('world_expected_plus_1ppt',.01)):
            altered=er.copy();altered[names.index('World')]+=change
            target=float(cur@altered); w=_optimise(cov,altered,caps,target)
            sensitivity[label]=point(w,returns=altered) if w is not None else None
    return {'available':bool(points),'reason':None if points else 'No feasible solutions.','points':points,'current':current,
            'benchmark':benchmark,'hypothetical_80_20':alternative,'alternatives':[alternative] if alternative else [],'same_return':same_result,
            'sensitivity':sensitivity,'coverage':covered/mass if mass else 0,'start':dates[0],'end':dates[-1],'n':len(dates),
            'missing_groups':missing,'methods':{'covariance':'Monthly sample covariance × 12; common consecutive EUR total-return observations.',
                'universe':names,'limits':dict(zip(names,map(float,caps))),'expected_returns':dict(zip(names,map(float,er))),
                'constraints':'Long only; no borrowing; sum of weights = 1. Benchmarks are illustrative and may exceed edited caps.',
                'compound':'Arithmetic expected return minus half model variance; approximation, not a forecast.',
                'interpretation':'Current point normalises covered investments; unclassified investments and emergency cash are excluded. Optimiser weights are diagnostics, not recommendations.'}}


def xirr(flows,guess=.1):
    grouped=defaultdict(float)
    try:
        for d,v in flows:
            d=date.fromisoformat(d) if isinstance(d,str) else d
            if not isinstance(d,date) or not _finite(v): return None
            grouped[d]+=float(v)
    except (TypeError,ValueError): return None
    items=sorted((d,v) for d,v in grouped.items() if abs(v)>1e-12)
    if len(items)<2 or items[-1][0]<=items[0][0] or not any(v<0 for _,v in items) or not any(v>0 for _,v in items): return None
    # Conventional contributions followed by final proceeds have one root.
    # More sign changes can produce ambiguous money-weighted returns.
    if sum((items[i][1]>0)!=(items[i-1][1]>0) for i in range(1,len(items)))>1: return None
    start=items[0][0]
    def npv_log(x): return sum(v*math.exp(-x*(d-start).days/365) for d,v in items)
    try:
        root=brentq(npv_log,-12,12,xtol=1e-12)
        return math.expm1(root)
    except (ValueError,OverflowError): return None


def eur_price_monthly(prices,fx=None):
    if fx is None: return dict(prices)
    return {m:float(v)/float(fx[m]) for m,v in prices.items() if m in fx and _finite(v) and v>0 and _finite(fx[m]) and fx[m]>0}

monthly_eur_prices=eur_price_monthly
