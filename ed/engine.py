"""Models fitted on the department's own history, the forward Monte Carlo (the same simulator run from the live state), the
staffing optimiser, and the KPIs."""
import functools

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import roc_auc_score

from . import world

VERSION = "ed-flow-1"
HOLDOUT_DAYS = 14


@functools.lru_cache(maxsize=4)
def history(seed):
    """The department's past up to the demo clock: every patient and the hourly census of the last eight weeks."""
    out = world.run(seed, world.NOW, record_from=world.NOW - 56 * 1440)
    return out


def features(p):
    return [p["acuity"], p["age"], world.COMPLAINTS.index(p["complaint"]), int(p["arrived"] // 60) % 24, int(p["arrived"] // 1440) % 7]


def hourly_arrivals(patients, start, end):
    """Arrivals per clock hour between two minutes, with the acuity-2-or-worse share. -> arrays"""
    n = int((end - start) // 60)
    counts, high = np.zeros(n), np.zeros(n)
    for p in patients:
        if start <= p["arrived"] < end:
            k = int((p["arrived"] - start) // 60)
            counts[k] += 1
            high[k] += p["acuity"] <= 2
    return counts, high


@functools.lru_cache(maxsize=4)
def fit(seed):
    """Arrival profile, admission classifier, ED length-of-stay regressor and the ward-stay survival curve, with held-out reports."""
    h = history(seed)
    pts = [p for p in h["patients"] if "departed" in p]
    cut = world.NOW - HOLDOUT_DAYS * 1440
    train, test = [p for p in pts if p["arrived"] < cut], [p for p in pts if p["arrived"] >= cut]
    # arrivals: mean count by (day of week, hour) over the training weeks; Poisson band
    counts, high = hourly_arrivals(h["patients"], 0, cut)
    hours = np.arange(len(counts))
    key = (hours // 24 % 7) * 24 + hours % 24
    profile = np.array([counts[key == k].mean() for k in range(168)])
    high_share = np.array([high[key == k].sum() / max(1, counts[key == k].sum()) for k in range(168)])
    tc, _ = hourly_arrivals(h["patients"], cut, world.NOW)
    th = np.arange(len(tc)) + cut // 60
    pred = profile[(th // 24 % 7) * 24 + th % 24]
    last_week = hourly_arrivals(h["patients"], cut - 7 * 1440, world.NOW - 7 * 1440)[0]
    arrivals_report = {"test_hours": int(len(tc)), "mae_profile": round(float(np.abs(pred - tc).mean()), 2), "mae_same_hour_last_week": round(float(np.abs(last_week - tc).mean()), 2), "mae_flat_mean": round(float(np.abs(counts.mean() - tc).mean()), 2),
                       "inside_80pct_band": round(float(np.mean([lo <= y <= hi for y, (lo, hi) in zip(tc, [poisson_band(m) for m in pred])])), 2)}
    # admission and length of stay
    x, y = np.array([features(p) for p in train], float), np.array([p["admit"] for p in train])
    xt, yt = np.array([features(p) for p in test], float), np.array([p["admit"] for p in test])
    adm = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_depth=4, categorical_features=[2], random_state=0).fit(x, y)
    pa = adm.predict_proba(xt)[:, 1]
    by_acuity = {a: y[x[:, 0] == a].mean() for a in range(1, 6)}
    base_p = np.array([by_acuity[int(a)] for a in xt[:, 0]])
    bins = np.digitize(pa, [0.1, 0.25, 0.5, 0.75])
    adm_report = {"auc": round(float(roc_auc_score(yt, pa)), 3), "auc_acuity_only": round(float(roc_auc_score(yt, base_p)), 3), "admit_rate": round(float(yt.mean()), 3), "trained_on": len(y), "tested_on": len(yt),
                  "calibration": [{"predicted": round(float(pa[bins == k].mean()), 3), "observed": round(float(yt[bins == k].mean()), 3), "n": int((bins == k).sum())} for k in range(5) if (bins == k).any()]}
    los_y = np.array([p["departed"] - p["arrived"] for p in train])
    los_t = np.array([p["departed"] - p["arrived"] for p in test])
    los = HistGradientBoostingRegressor(loss="absolute_error", max_iter=200, learning_rate=0.05, max_depth=4, categorical_features=[2], random_state=0).fit(x, los_y)
    pl = los.predict(xt)
    med_by_acuity = {a: float(np.median(los_y[x[:, 0] == a])) for a in range(1, 6)}
    los_report = {"mae_min": round(float(np.abs(pl - los_t).mean()), 1), "mae_median_by_acuity": round(float(np.abs(np.array([med_by_acuity[int(a)] for a in xt[:, 0]]) - los_t).mean()), 1), "median_los_min": round(float(np.median(los_t)), 1)}
    # ward stays: empirical survival of completed stays, in hours
    stays = np.array([p["ward_min"] for p in train if p["admit"]]) / 60
    grid = np.arange(0, 24 * 15 + 1, 1.0)
    surv = np.array([(stays > g).mean() for g in grid])
    # ward turnover: departures balance admissions over eight weeks, so the discharge hazard is admissions per occupied bed-hour
    occ = np.mean([c["ward_occupied"] for c in h["census"]])
    ed_admits_h = sum(1 for p in train if p["admit"]) / ((cut) / 60)
    electives_h = world.ELECTIVES_PER_DAY * (5 + 2 * 0.3) / 7 / 24
    turnover = (ed_admits_h + electives_h) / occ
    return {"profile": profile, "high_share": high_share, "admission": adm, "los": los, "survival": (grid, surv), "turnover": float(turnover), "electives_per_hour": float(electives_h),
            "reports": {"arrivals": arrivals_report, "admission": adm_report, "los": los_report, "ward_stays": {"median_hours": round(float(np.median(stays)), 1), "n": int(len(stays)), "mean_occupied": round(float(occ), 1), "discharges_per_day": round(float(turnover * occ * 24), 1)}}}


def poisson_band(mean):
    from math import exp, factorial
    cdf, k, lo, hi = 0.0, 0, None, None
    while k < 60:
        cdf += exp(-mean) * mean ** k / factorial(k)
        if lo is None and cdf >= 0.1:
            lo = k
        if cdf >= 0.9:
            hi = k
            break
        k += 1
    return lo or 0, hi if hi is not None else k


def forecast_arrivals(m, now, hours=24):
    rows = []
    for k in range(hours):
        t = now + k * 60
        idx = int(t // 1440) % 7 * 24 + int(t // 60) % 24
        mean = float(m["profile"][idx])
        lo, hi = poisson_band(mean)
        rows.append({"hour": k, "minute": int(t), "expected": round(mean, 2), "lower": lo, "upper": hi, "acuity_1_2_share": round(float(m["high_share"][idx]), 3)})
    return rows


def ready_probability(m, elapsed_h, horizon_h):
    """P(discharge-ready within `horizon_h` more hours | stayed `elapsed_h`), from the empirical survival curve."""
    grid, surv = m["survival"]
    s_now = float(np.interp(elapsed_h, grid, surv))
    s_later = float(np.interp(elapsed_h + horizon_h, grid, surv))
    return 0.0 if s_now <= 1e-9 else max(0.0, 1 - s_later / s_now)


def forecast_beds(m, state, now, hours=24):
    """Expected ward discharges, expected admissions and the projected ward position, hour by hour, without running the simulator."""
    sim = state["sim"]
    pts = sim["patients"]
    occupied = len(sim["ward"])
    undecided = [i for i in sim["in_ed"] if "dispo_at" not in pts[i]]
    adm_probs = m["admission"].predict_proba(np.array([features(pts[i]) for i in undecided], float))[:, 1].tolist() if undecided else []
    boarding = len(sim["boarding"])
    arrivals = forecast_arrivals(m, now, hours)
    rows, free = [], world.WARD_BEDS - occupied
    exp_adm_from_ed = sum(adm_probs)
    backlog = occupied * m["turnover"] * 8                                           # overnight's ready patients waiting for the morning round
    for k in range(hours):
        t = now + k * 60
        h = int(t // 60) % 24
        backlog += occupied * m["turnover"]
        discharges = min(backlog, 6.0) if 10 <= h < 20 else min(backlog, 0.5)       # wards process discharges in daytime, a few at night
        backlog -= discharges
        new_adm = arrivals[k]["expected"] * m["reports"]["admission"]["admit_rate"] + (exp_adm_from_ed / 4 if k < 4 else 0)
        electives = m["electives_per_hour"] * 24 / 6 if 8 <= h < 14 and int(t // 1440) % 7 < 5 else 0
        free = free + discharges - new_adm - electives
        occupied = world.WARD_BEDS - free
        rows.append({"hour": k, "minute": int(t), "expected_discharges": round(discharges, 2), "expected_admissions": round(new_adm + electives, 2), "projected_free_beds": round(free, 1), "projected_boarding": round(max(0.0, boarding - free), 1)})
    return {"ward_beds": world.WARD_BEDS, "occupied": len(sim["ward"]), "boarding_now": boarding, "expected_admissions_from_ed_now": round(exp_adm_from_ed, 2), "hours": rows}


def simulate(seed, state, hours, runs=30, surge=None, slowdown=None, actions=None, record_from=None):
    """Monte Carlo forward from the live state. `actions`: {"surge_beds": n, "extra_nurses": {shift: n}, "discharge_push": factor}.
    The twin is the generator itself, which is the honest limit of a slice: on a real department the twin would be an approximation."""
    actions = actions or {}
    now = state["t"]
    nurse_extra = {}
    for shift, n in (actions.get("extra_nurses") or {}).items():
        for d in range(int(now // 1440) % 7, int(now // 1440) % 7 + 2):
            nurse_extra[(d % 7, shift)] = n
    push = (now, now + hours * 60, actions["discharge_push"]) if actions.get("discharge_push") else None
    runs_out = []
    for k in range(runs):
        o = world.run(seed * 1000 + k, now + hours * 60, start_state=_copy_state(state), surge=surge, slowdown=slowdown, nurse_extra=nurse_extra or None, surge_beds=actions.get("surge_beds", 0), discharge_push=push, record_from=record_from if record_from is not None else now)
        runs_out.append(o)
    keys = ("waiting", "in_ed", "boarding", "ward_occupied", "ct_queue")
    hourly = []
    n_hours = min(len(o["census"]) for o in runs_out)
    for h in range(n_hours):
        row = {"hour": h, "minute": runs_out[0]["census"][h]["minute"]}
        for key in keys:
            v = np.array([o["census"][h][key] for o in runs_out])
            row[key] = {"p10": float(np.quantile(v, 0.1)), "p50": float(np.median(v)), "p90": float(np.quantile(v, 0.9))}
        hourly.append(row)
    kp = [world.waits(o["patients"], since=now) for o in runs_out]
    kpis = {k: {"p10": round(float(np.quantile([x[k] or 0 for x in kp], 0.1)), 1), "p50": round(float(np.median([x[k] or 0 for x in kp])), 1), "p90": round(float(np.quantile([x[k] or 0 for x in kp], 0.9)), 1)} for k in ("door_to_bed_median", "door_to_bed_p90", "boarding_median", "boarding_p90", "ed_los_median")}
    kpis["peak_waiting"] = {"p50": round(float(np.median([max(c["waiting"] for c in o["census"]) for o in runs_out])), 1), "p90": round(float(np.quantile([max(c["waiting"] for c in o["census"]) for o in runs_out], 0.9)), 1)}
    kpis["peak_boarding"] = {"p50": round(float(np.median([max(c["boarding"] for c in o["census"]) for o in runs_out])), 1), "p90": round(float(np.quantile([max(c["boarding"] for c in o["census"]) for o in runs_out], 0.9)), 1)}
    return {"runs": runs, "hours": hours, "hourly": hourly, "kpis": kpis}


def _copy_state(state):
    import copy
    return {"t": state["t"], "sim": {k: copy.deepcopy(v) if k in ("patients", "waiting", "boarding", "ward", "ct_queue", "events", "in_ed") else v for k, v in state["sim"].items()}}


def staffing(census_p80_by_shift, pool, ratio=world.NURSE_RATIO, max_per_shift=4):
    """CP-SAT: how many nurses to call in per shift so that the P80 census is covered at the ratio, cheapest first.
    pool: [{"name", "skill", "cost_per_shift", "shifts": [...]}] -> plan"""
    from ortools.sat.python import cp_model
    mdl = cp_model.CpModel()
    use, short = {}, {}
    for i, n in enumerate(pool):
        for s in n["shifts"]:
            use[(i, s)] = mdl.NewBoolVar(f"u{i}_{s}")
    for s, need in census_p80_by_shift.items():
        rostered = world.SHIFTS[s][2]
        required = int(np.ceil(need / ratio))
        extra = max(0, required - rostered)
        vars_s = [use[(i, s)] for i, n in enumerate(pool) if s in n["shifts"]]
        short[s] = mdl.NewIntVar(0, 50, f"short_{s}")
        mdl.Add(sum(vars_s) + short[s] >= extra)                                           # a nurse short is allowed, at a price the cost can never beat
        mdl.Add(sum(vars_s) <= max_per_shift)
    for i, n in enumerate(pool):
        mdl.Add(sum(use[(i, s)] for s in n["shifts"]) <= 1)                               # one shift per person
    mdl.Minimize(sum(int(pool[i]["cost_per_shift"]) * v for (i, s), v in use.items()) + 100_000 * sum(short.values()))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    status = solver.Solve(mdl)
    picked = [{"name": pool[i]["name"], "skill": pool[i]["skill"], "shift": s, "cost": pool[i]["cost_per_shift"]} for (i, s), v in use.items() if solver.Value(v)]
    need = {s: {"census_p80": round(float(c), 1), "nurses_rostered": world.SHIFTS[s][2], "nurses_required": int(np.ceil(c / ratio)), "called_in": sum(1 for p in picked if p["shift"] == s), "still_short": int(solver.Value(short[s]))} for s, c in census_p80_by_shift.items()}
    return {"status": solver.StatusName(status), "call_ins": picked, "by_shift": need, "cost": sum(p["cost"] for p in picked), "ratio": ratio}
