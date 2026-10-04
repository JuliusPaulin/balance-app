from datetime import date

import pytest

from services.portfolio_math import analyze_risk, eur_price_monthly, frontier, xirr


def _market(n=36):
    months = [f"2020-{i:02d}" for i in range(1, 13)] + [f"2021-{i:02d}" for i in range(1, 13)] + [f"2022-{i:02d}" for i in range(1, 13)]
    months = months[:n]
    return {"returns": {
        "World": {m: 0.01 if i % 2 else -0.005 for i, m in enumerate(months)},
        "US": {m: 0.015 if i % 2 else -0.004 for i, m in enumerate(months)},
    }}


def test_risk_is_annualised_and_tracks_drawdown():
    result = analyze_risk({"World": 1}, _market())
    assert result["available"] is True
    assert result["n"] == 36
    assert result["volatility"] > 0
    assert result["max_drawdown"] > 0
    assert result["start"] == "2020-01"


def test_risk_uses_covered_subset_and_refuses_short_history():
    market = _market()
    del market["returns"]["US"]["2020-01"]
    result = analyze_risk({"World": 0.5, "US": 0.5, "Unknown": 0.5}, market)
    assert result["available"] is True
    assert set(result["weights"]) == {"World", "US"}
    assert abs(sum(result["weights"].values()) - 1) < 1e-9
    assert analyze_risk({"World": 1}, {"returns": {"World": {"2020-01": 0.1}}})["available"] is False


def test_frontier_respects_caps_and_returns_only_feasible_points():
    result = frontier({"World": 0.7, "US": 0.3}, _market(), {
        "returns": {"World": 0.06, "US": 0.09},
        "max_weights": {"World": 0.8, "US": 0.8}, "risk_free": 0.018,
    })
    assert result["available"] is True
    assert result["points"]
    assert all(sum(p["weights"].values()) == pytest.approx(1) for p in result["points"])
    assert all(max(p["weights"].values()) <= 0.800001 for p in result["points"])


def test_xirr_and_usd_to_eur_monthly_conversion():
    value = xirr([("2020-01-01", -100), ("2021-01-01", 110)])
    assert 0.09 < value < 0.11
    assert eur_price_monthly({"2024-01": 100}, {"2024-01": 1.1}) == {"2024-01": 100 / 1.1}


def test_correlations_not_average_volatility_and_initial_drawdown():
    market=_market()
    market['returns']['US']={m:-v for m,v in market['returns']['World'].items()}
    result=analyze_risk({'World':.5,'US':.5},market)
    assert result['volatility']==pytest.approx(0,abs=1e-12)
    assert result['max_drawdown']==0
    assert result['sharpe'] is None
    assert analyze_risk({'World':1},market)['max_drawdown']>=.005


def test_same_return_is_exact_and_bond_universe_is_optional():
    market=_market()
    market['returns']['Bonds']={m:v/5 for m,v in market['returns']['World'].items()}
    model={'returns':{'World':.06,'US':.09,'Bonds':.02},'max_weights':{'World':.8,'US':.8,'Bonds':.8},'include_bonds':True}
    result=frontier({'World':.7,'US':.3},market,model)
    assert result['same_return']['available']
    assert result['same_return']['return']==pytest.approx(.069,abs=1e-8)
    assert result['benchmark']['volatility']>0
    assert result['hypothetical_80_20']['weights']['Bonds']==.2
    assert result['current']['weights']['Bonds']==0
    assert result['current']['compound_approx']==pytest.approx(.069-result['current']['volatility']**2/2)
    model['include_bonds']=False
    result=frontier({'World':.7,'US':.3},market,model)
    assert result['hypothetical_80_20'] is None
    assert all('Bonds' not in p['weights'] for p in result['points'])


def test_zero_length_xirr_and_bad_matrix_are_unavailable():
    assert xirr([('2026-01-01',-100),('2026-01-01',100)]) is None
    market=_market();market['returns']['World']['2020-03']=float('nan')
    assert not analyze_risk({'World':1},market)['available']


def test_infeasible_caps_and_short_history_never_become_zero_risk():
    model={'returns':{'World':.07,'US':.07},'max_weights':{'World':.2,'US':.3}}
    assert not frontier({'World':1},_market(),model)['available']
    result=analyze_risk({'World':.5,'Unknown':.5},_market(12))
    assert not result['available']
    assert 'volatility' not in result


def test_equal_equity_returns_frontier_endpoint_matches_same_return_solution():
    import json
    from pathlib import Path
    from services.portfolio_market import _dataset, PROXIES
    groups = ['World', 'US', 'Europe', 'EM', 'Finland', 'Norway', 'Sweden', 'Apple', 'Bonds']
    # Public cached observations reproduce the degenerate maximum-return face.
    cached = json.loads((Path(__file__).parents[1] / 'services/portfolio_seed.json').read_text())
    prices = {g: json.loads(cached[s]['payload'])['prices'] for g,s,_ in PROXIES}
    market = _dataset(prices, None, [], 'cached')
    model = {'returns': {g: .035 if g == 'Bonds' else .07 for g in groups},
             'max_weights': {g: 1 for g in groups}, 'include_bonds': True}
    result = frontier({'World': 1}, market, model)
    equity_only = frontier({'World': 1}, market, {**model, 'include_bonds': False})
    assert result['same_return']['available']
    assert result['points'][-1]['return'] == pytest.approx(.07)
    assert result['points'][-1]['volatility'] == pytest.approx(result['same_return']['volatility'], abs=1e-7)
    assert result['points'][-1]['volatility'] == pytest.approx(equity_only['points'][0]['volatility'], abs=1e-7)
