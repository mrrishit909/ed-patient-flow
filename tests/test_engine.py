"""Unit tests for the simulator, the fitted models, the forward Monte Carlo and the staffing solver: no database."""
import numpy as np

from ed import engine, world

NOW = world.NOW


def test_simulator_is_deterministic_and_conserves_patients():
    a = world.run(3, 7 * 1440, record_from=0)
    b = world.run(3, 7 * 1440, record_from=0)
    assert [p["arrived"] for p in a["patients"]] == [p["arrived"] for p in b["patients"]] and 800 < len(a["patients"]) < 1200
    for p in a["patients"]:
        if "departed" in p:
            assert p["arrived"] <= p["bed_at"] <= p["dispo_at"] <= p["departed"] and p["disposition"] in ("admit", "discharge")
            if p["disposition"] == "admit":
                assert p["ward_bed_at"] >= p["dispo_at"]
    c = a["census"][-1]
    assert c["in_ed"] <= world.ED_BEDS and c["ward_occupied"] <= world.WARD_BEDS and c["boarding"] <= c["in_ed"]


def test_restart_from_state_matches_a_continuous_run_in_shape():
    h = world.run(5, 20 * 1440, record_from=19 * 1440)
    cont = world.run(5, 21 * 1440, record_from=20 * 1440)
    fwd = world.run(5, 21 * 1440, start_state=engine._copy_state(h["state"]), record_from=20 * 1440)
    assert len(fwd["census"]) == 24 and len(cont["census"]) >= 24 and all(0 <= c["in_ed"] <= world.ED_BEDS for c in fwd["census"])
    # the state's occupants all leave eventually: nothing is stranded in a bed without a pending event
    stuck = [i for i in h["state"]["sim"]["in_ed"] if "departed" not in fwd["patients"][i] and fwd["patients"][i].get("dispo_at", 1e12) > 21 * 1440]
    assert len(stuck) <= 3


def test_surge_raises_waiting_and_actions_lower_it():
    h = engine.history(17)
    st = h["state"]
    d = world.DEMO
    base = engine.simulate(17, st, 10, runs=8)
    surge = engine.simulate(17, st, 10, runs=8, surge=d["surge"], slowdown=d["slowdown"])
    acted = engine.simulate(17, st, 10, runs=8, surge=d["surge"], slowdown=d["slowdown"], actions={"surge_beds": 6, "extra_nurses": {"day": 2, "evening": 2}, "discharge_push": 1.5})
    assert surge["kpis"]["peak_waiting"]["p50"] > base["kpis"]["peak_waiting"]["p50"] + 15 and acted["kpis"]["peak_waiting"]["p50"] < surge["kpis"]["peak_waiting"]["p50"]
    assert len(base["hourly"]) == 10 and all(r["waiting"]["p10"] <= r["waiting"]["p50"] <= r["waiting"]["p90"] for r in base["hourly"])


def test_models_beat_their_baselines_where_they_do_and_say_so_where_they_do_not():
    m = engine.fit(17)
    r = m["reports"]
    assert r["arrivals"]["mae_profile"] < r["arrivals"]["mae_same_hour_last_week"] < r["arrivals"]["mae_flat_mean"] and 0.7 <= r["arrivals"]["inside_80pct_band"] <= 0.95
    assert r["admission"]["auc"] > r["admission"]["auc_acuity_only"] > 0.6 and all(abs(c["predicted"] - c["observed"]) < 0.12 for c in r["admission"]["calibration"] if c["n"] >= 50)
    assert abs(r["los"]["mae_min"] - r["los"]["mae_median_by_acuity"]) < 10                 # honest: the LOS model is no better than the median
    fc = engine.forecast_arrivals(m, NOW, 24)
    assert len(fc) == 24 and 120 < sum(x["expected"] for x in fc) < 170 and all(x["lower"] <= x["expected"] <= x["upper"] for x in fc)
    fb = engine.forecast_beds(m, engine.history(17)["state"], NOW, 24)
    assert len(fb["hours"]) == 24 and fb["occupied"] <= world.WARD_BEDS and all(h["projected_boarding"] >= 0 for h in fb["hours"])


def test_poisson_band_and_staffing_solver():
    assert engine.poisson_band(0.0) == (0, 0) and engine.poisson_band(9.0)[0] < 9 < engine.poisson_band(9.0)[1]
    pool = [{"name": f"N{i}", "skill": "ED" if i < 3 else "agency", "cost_per_shift": 480 if i < 3 else 760, "shifts": ["day", "evening", "night"]} for i in range(6)]
    plan = engine.staffing({"day": 44.0, "evening": 36.0, "night": 24.0}, pool)
    assert plan["status"] in ("OPTIMAL", "FEASIBLE") and plan["by_shift"]["day"]["called_in"] == 2 and plan["by_shift"]["evening"]["called_in"] == 0 and plan["by_shift"]["night"]["called_in"] == 0
    assert all(p["skill"] == "ED" for p in plan["call_ins"]) and len({p["name"] for p in plan["call_ins"]}) == len(plan["call_ins"])      # cheapest first, one shift per person
