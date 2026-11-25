// Balance — planning-math.js
//
// The long-term projection. This is the same rules as services/planning.py;
// the two have to stay in step, and tests/test_planning.py checks that they do
// when Node is available. Nothing in this file touches the page.

var planningProject, planningSummarize;

(function () {
    function num(source, key, fallback) {
        if (fallback === undefined) fallback = 0;
        if (!source || typeof source !== "object") return fallback;
        var value = source[key];
        if (typeof value === "boolean" || value == null || value === "") return fallback;
        var number = Number(value);
        return isFinite(number) ? number : fallback;
    }

    // JavaScript `value || fallback`, with 0 falling through and a negative kept.
    function jsOr(value, fallback) {
        if (typeof value === "boolean" || value == null || value === "") return fallback;
        var number = Number(value);
        if (!isFinite(number) || number === 0) return fallback;
        return number;
    }

    function annuity(principal, ratePct, years) {
        principal = Number(principal);
        var rate = Number(ratePct) / 100;
        var term = Number(years);
        if (!isFinite(principal) || !isFinite(rate) || !isFinite(term)) return 0;
        term = Math.max(1, term);
        if (principal === 0) return 0;
        if (rate === 0) return principal / term;
        return principal * rate / (1 - Math.pow(1 + rate, -term));
    }

    function payDown(loans) {
        var paid = 0;
        for (var i = 0; i < loans.length; i++) {
            var loan = loans[i];
            if (loan.bal <= 0.5) continue;
            var interest = loan.bal * loan.r;
            var principal = Math.min(loan.pmt - interest, loan.bal);
            paid += interest + principal;
            loan.bal -= principal;
        }
        return paid;
    }

    planningProject = function (assumptions, events, startYear) {
        assumptions = assumptions || {};
        events = events || [];
        var age0 = num(assumptions, "age");
        var end = Math.max(num(assumptions, "endAge"), age0 + 1);
        var inv = num(assumptions, "startInv");
        var ranOut = null;
        var enabled = [];
        var customs = [];
        var homes = [];
        for (var e = 0; e < events.length; e++) {
            var event = events[e];
            if (!event || !event.on) continue;
            enabled.push(event);
            if (event.type === "custom") {
                customs.push({ src: event, loans: [], assets: [] });
            } else if (event.type === "house") {
                homes.push({ src: event, bought: false, bal: 0, value: 0 });
            }
        }

        var rows = [];
        var age = age0;
        var steps = 0;
        while (age <= end + 1e-9 && steps < 200) {
            steps++;
            var t = age - age0;
            var inflation = num(assumptions, "infl") / 100;
            var defl = Math.pow(1 + inflation, t);
            var retired = age >= num(assumptions, "retAge");
            var income;
            if (retired) {
                income = age >= num(assumptions, "pensionAge")
                    ? num(assumptions, "pension") * 12 * defl : 0;
            } else {
                income = num(assumptions, "income") * 12 * Math.pow(1 + num(assumptions, "incG") / 100, t);
            }
            var scale = retired ? num(assumptions, "retPct") / 100 : 1;
            var growth = Math.pow(1 + num(assumptions, "expG") / 100, t);
            var living = num(assumptions, "expenses") * 12 * growth * scale;
            var rentNow = num(assumptions, "rent") * 12 * growth * scale;
            var big = 0, inflow = 0, shock = 0;
            var marks = [], minor = [];

            for (var p = 0; p < enabled.length; p++) {
                var ev = enabled[p];
                var kind = ev.type;
                var eventAge = num(ev, "age");
                if (kind !== "house" && kind !== "custom" && age === eventAge) marks.push(ev.name || "");
                var years = Math.max(1, jsOr(ev.years, 1));
                var inRange = eventAge <= age && age < eventAge + years;
                if (kind === "sabbatical") {
                    if (inRange && !retired) income *= 1 - num(ev, "pct") / 100;
                } else if (kind === "child" || kind === "oneoff") {
                    if (inRange) big += num(ev, "amount") * defl;
                } else if (kind === "extra") {
                    if (inRange) inflow += num(ev, "amount") * defl;
                } else if (kind === "windfall") {
                    if (age === eventAge) inflow += num(ev, "amount") * defl;
                } else if (kind === "car") {
                    var every = jsOr(ev.every, 0);
                    var hit = every > 0
                        ? (age >= eventAge && (age - eventAge) % every === 0 && age <= jsOr(ev.until, end))
                        : age === eventAge;
                    if (hit) {
                        big += num(ev, "amount") * defl;
                        if (age !== eventAge) minor.push(ev.name || "");
                    }
                } else if (kind === "shock") {
                    if (age === eventAge) shock = num(ev, "pct") / 100;
                }
            }

            var otherAssets = 0, otherDebt = 0;
            for (var c = 0; c < customs.length; c++) {
                var custom = customs[c];
                var src = custom.src;
                var cEvery = Math.max(1, jsOr(src.every, 1));
                var cYears = Math.max(1, jsOr(src.years, 1));
                var cAge = num(src, "age");
                var repeat = src.repeat;
                var cHit;
                if (repeat === "yearly") cHit = cAge <= age && age < cAge + cYears;
                else if (repeat === "every") cHit = age >= cAge && (age - cAge) % cEvery === 0 && age <= jsOr(src.until, end);
                else cHit = age === cAge;
                var factor = src.real ? defl : 1;
                var assetGrowth = jsOr(src.assetG, 0) / 100;
                for (var a = 0; a < custom.assets.length; a++) custom.assets[a].value *= 1 + assetGrowth;
                if (cHit) {
                    if (age === cAge) marks.push(src.name || "");
                    else if (repeat === "every") minor.push(src.name || "");
                    var amount = num(src, "amount") * factor;
                    if (src.dir === "income") {
                        inflow += amount;
                    } else {
                        if (src.loan) {
                            var down = num(src, "downPct");
                            var ratePct = num(src, "rate");
                            var term = Math.max(1, num(src, "term"));
                            var principal = amount * (1 - down / 100);
                            big += amount * down / 100;
                            custom.loans.push({
                                bal: principal, r: ratePct / 100,
                                pmt: annuity(principal, ratePct, term),
                            });
                        } else big += amount;
                        if (src.asset) {
                            if (repeat === "every" && custom.assets.length) {
                                var traded = 0;
                                for (var ta = 0; ta < custom.assets.length; ta++) traded += custom.assets[ta].value;
                                inflow += traded;
                                custom.assets = [];
                            }
                            custom.assets.push({ value: amount * num(src, "assetPct") / 100 });
                        }
                    }
                }
                big += payDown(custom.loans);
                var running = num(src, "running");
                if (src.dir !== "income" && src.asset && custom.assets.length && running) big += running * factor;
                for (var oa = 0; oa < custom.assets.length; oa++) otherAssets += custom.assets[oa].value;
                for (var od = 0; od < custom.loans.length; od++) otherDebt += Math.max(0, custom.loans[od].bal);
            }

            var housing = 0, housingUpfront = 0, equity = 0, mortgage = 0, homeValue = 0;
            for (var h = 0; h < homes.length; h++) {
                var home = homes[h];
                var hs = home.src;
                if (home.bought) home.value *= 1 + num(hs, "appr") / 100;
                if (age === num(hs, "age")) {
                    var price = num(hs, "price") * defl;
                    var hDown = num(hs, "downPct");
                    var hRate = num(hs, "rate");
                    var hTerm = Math.max(1, num(hs, "term"));
                    home.bought = true;
                    home.value = price;
                    home.priceNom = price;
                    home.bal = price * (1 - hDown / 100);
                    home.r = hRate / 100;
                    home.pmt = annuity(home.bal, hRate, hTerm);
                    housingUpfront += price * (hDown + num(hs, "tax")) / 100;
                    marks.push(hs.name || "");
                }
                if (home.bought) {
                    housing += payDown([home]);
                    var yearsOwned = age - num(hs, "age");
                    housing += (home.priceNom || 0) * num(hs, "costPct") / 100 * Math.pow(1 + inflation, yearsOwned);
                    if (hs.replacesRent) {
                        var cut = Math.min(rentNow, living);
                        living -= cut;
                        rentNow -= cut;
                    }
                    equity += home.value - home.bal;
                    mortgage += Math.max(0, home.bal);
                    homeValue += home.value;
                }
            }

            var spend = living + housing + housingUpfront + big;
            var net = income + inflow - spend;
            if (shock && inv > 0) inv *= 1 - shock;
            var returnRate = (retired ? num(assumptions, "retPost") : num(assumptions, "retPre")) / 100;
            inv = (inv > 0 ? inv * (1 + returnRate) : inv) + net;
            if (inv < 0 && ranOut === null) ranOut = age;
            var swr = num(assumptions, "swr") / 100;
            var year = Number(startYear) + t;
            rows.push({
                age: age,
                year: year === Math.trunc(year) ? Math.trunc(year) : year,
                defl: defl,
                retired: retired,
                income: income + inflow,
                salary: income,
                inflow: inflow,
                living: living,
                housing: housing + housingUpfront,
                big: big,
                spend: spend,
                net: net,
                inv: inv,
                equity: equity,
                mortgage: mortgage,
                home_value: homeValue,
                other_assets: otherAssets,
                other_debt: otherDebt,
                other_net: otherAssets - otherDebt,
                loans: mortgage + otherDebt,
                nw: inv + equity + otherAssets - otherDebt,
                fi_target: swr > 0 ? (living + housing) / swr : Infinity,
                marks: marks,
                minor: minor,
            });
            age += 1;
        }

        var fiAge = null;
        for (var r = 0; r < rows.length; r++) {
            if (rows[r].inv >= rows[r].fi_target && rows[r].inv > 0 && !rows[r].retired) {
                fiAge = rows[r].age;
                break;
            }
        }
        return { rows: rows, ran_out: ranOut, fi_age: fiAge };
    };

    planningSummarize = function (result, assumptions, unit) {
        var rows = result.rows;
        var retirementAge = num(assumptions, "retAge");
        var ret = rows[rows.length - 1];
        for (var i = 0; i < rows.length; i++) if (rows[i].age === retirementAge) { ret = rows[i]; break; }
        var salary = rows[0].salary;
        var expensesYear = num(assumptions, "expenses") * 12;
        var savingsRate = salary > 0 ? (salary - expensesYear) / salary * 100 : 0;
        var pensionNow = num(assumptions, "pensionAge") <= retirementAge;
        var pensionYear = pensionNow ? num(assumptions, "pension") * 12 * ret.defl : 0;
        var sustainable = (Math.max(0, ret.inv) * num(assumptions, "swr") / 100 + pensionYear) / 12;
        function inUnit(row, value) { return unit === "real" ? value / row.defl : value; }
        var peak = rows[0];
        for (var p = 1; p < rows.length; p++) {
            if (inUnit(rows[p], rows[p].nw) > inUnit(peak, peak.nw)) peak = rows[p];
        }
        return {
            net_worth_at_retirement: inUnit(ret, ret.nw),
            retirement_year: ret.year,
            fi_age: result.fi_age,
            ran_out: result.ran_out,
            sustainable_monthly: unit === "real" ? sustainable / ret.defl : sustainable,
            pension_already: pensionNow,
            savings_rate: savingsRate,
            monthly_surplus: num(assumptions, "income") - num(assumptions, "expenses"),
            peak_nw: inUnit(peak, peak.nw),
            peak_age: peak.age,
        };
    };
})();
