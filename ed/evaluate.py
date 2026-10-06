"""Measure the forecasts, the admission model and the forward simulation on departments the demo never uses. Writes docs/evaluation.md.

    python -m ed.evaluate
"""
import pathlib

import numpy as np

from . import engine, world

OUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "evaluation.md"
SEEDS = (101, 102, 103)
DAYS = 6                     # forward-simulation days scored per department
HOURS = 12
RUNS = 20


def table(head, rows):
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def main():
    model_rows, sim_rows, surge_rows = [], [], []
    for seed in SEEDS:
        m = engine.fit(seed)
        r = m["reports"]
        model_rows.append([seed, r["arrivals"]["mae_profile"], r["arrivals"]["mae_same_hour_last_week"], f"{r['arrivals']['inside_80pct_band']:.0%}", r["admission"]["auc"], r["admission"]["auc_acuity_only"], r["los"]["mae_min"], r["los"]["mae_median_by_acuity"]])
        # forward simulation from the live state on six consecutive mornings, scored against what the generator then did
        cover = {"peak_waiting": [], "door_p90": []}
        err = {"peak_waiting": [], "door_p90": [], "rw_peak_waiting": []}
        for d in range(DAYS):
            start = world.NOW - (DAYS - d) * 1440
            h = world.run(seed, start, record_from=start - 1440)
            truth = world.run(seed, start + HOURS * 60, record_from=start)
            sim = engine.simulate(seed + 50, h["state"], HOURS, runs=RUNS)
            tw = max(c["waiting"] for c in truth["census"] if c["minute"] > start)
            tk = world.waits(truth["patients"], since=start)
            pw = sim["kpis"]["peak_waiting"]
            yesterday_peak = max(c["waiting"] for c in h["census"] if start - 1440 < c["minute"] <= start - 1440 + HOURS * 60)
            err["peak_waiting"].append(abs(pw["p50"] - tw))
            err["rw_peak_waiting"].append(abs(yesterday_peak - tw))
            err["door_p90"].append(abs(sim["kpis"]["door_to_bed_p90"]["p50"] - (tk["door_to_bed_p90"] or 0)))
            cover["peak_waiting"].append(tw <= pw["p90"] * 1.0 + 0.5)
            cover["door_p90"].append(sim["kpis"]["door_to_bed_p90"]["p10"] <= (tk["door_to_bed_p90"] or 0) <= sim["kpis"]["door_to_bed_p90"]["p90"])
        sim_rows.append([seed, DAYS, round(np.mean(err["peak_waiting"]), 1), round(np.mean(err["rw_peak_waiting"]), 1), f"{np.mean(cover['peak_waiting']):.0%}", round(np.mean(err["door_p90"]), 1), f"{np.mean(cover['door_p90']):.0%}"])
        # the surge: does the package of actions help, and by how much, on this department
        h = engine.history(seed)
        d = world.DEMO
        base = engine.simulate(seed + 9, h["state"], 12, runs=RUNS, surge=d["surge"], slowdown=d["slowdown"])
        act = engine.simulate(seed + 9, h["state"], 12, runs=RUNS, surge=d["surge"], slowdown=d["slowdown"], actions={"surge_beds": 6, "extra_nurses": {"day": 2, "evening": 2}, "discharge_push": 1.5})
        nurses_only = engine.simulate(seed + 9, h["state"], 12, runs=RUNS, surge=d["surge"], slowdown=d["slowdown"], actions={"extra_nurses": {"day": 4, "evening": 4}})
        beds_only = engine.simulate(seed + 9, h["state"], 12, runs=RUNS, surge=d["surge"], slowdown=d["slowdown"], actions={"surge_beds": 6})
        surge_rows.append([seed, base["kpis"]["peak_waiting"]["p50"], act["kpis"]["peak_waiting"]["p50"], beds_only["kpis"]["peak_waiting"]["p50"], nurses_only["kpis"]["peak_waiting"]["p50"], base["kpis"]["door_to_bed_median"]["p50"], act["kpis"]["door_to_bed_median"]["p50"], base["kpis"]["peak_boarding"]["p50"], act["kpis"]["peak_boarding"]["p50"]])
    mean = lambda rows, k: round(float(np.mean([float(r[k]) for r in rows])), 1)        # noqa: E731
    doc = f"""# Evaluation

Everything here is measured on synthetic departments generated with seeds the demo does not use (`{SEEDS}`). Reproduce with
`python -m ed.evaluate` (about two minutes). The numbers say how well the methods do against the department as the simulator plays it.
The forward simulation is the generator itself run from the live state with fresh randomness, so its errors are the floor a real twin
could reach, not what one would reach: on a real department the twin is an approximation and the bands would be wider.

## 1. Models fitted on sixteen weeks, tested on the last two

{table(["department (seed)", "arrivals/h error", "same hour last week", "inside 80% band", "admission AUC", "triage level alone", "ED stay error (min)", "median by triage"], model_rows)}

The arrival profile beats the same-hour-last-week baseline by about {1 - mean(model_rows, 1) / mean(model_rows, 2):.0%} and its Poisson band is about right. The admission model adds a
little over triage level alone (age, complaint and hour carry some signal). The length-of-stay model is no better than the median by
triage level, which the page says: most of the variation in this world is noise around a triage-level mean, and the model cannot see
imaging or the nurse ratio at arrival.

## 2. The forward simulation, scored on {DAYS} mornings

At 07:00 each day the department is run forward {HOURS} hours {RUNS} times and the P50 compared with what then happened. "Yesterday" is the
same window the day before, which is what a charge nurse has without a simulation.

{table(["department (seed)", "mornings", "peak waiting error", "yesterday's peak", "truth under P90", "door-to-bed P90 error (min)", "truth inside P10–P90"], sim_rows)}

On the point estimate the simulation is no better than yesterday's peak, and the page should not claim otherwise: the morning
state carries little information about the afternoon, and both the simulation and yesterday are guesses around the same profile. The
truth falls under the simulation's P90 {np.mean([float(r[4][:-1]) for r in sim_rows]):.0f}% of the time, so the band is a little narrow for a P90; what the simulation adds is a band at all,
and a reaction to the live state (how full the beds already are, who is boarding) that yesterday cannot give.

## 3. The surge, with and without action

The demo's disruption on each department (30 injured patients over an hour, ward discharges at 40% for eight hours), and the packages of
actions, over 12 hours and {RUNS} runs. Peak waiting room, median door-to-bed and peak boarding, P50 of the runs.

{table(["department (seed)", "peak waiting", "with the package", "6 beds only", "4 nurses only", "door-to-bed (min)", "with the package", "peak boarding", "with the package"], surge_rows)}

The package (six surge beds, two nurses on two shifts, a discharge push) cuts peak waiting by about {1 - mean(surge_rows, 2) / mean(surge_rows, 1):.0%} and halves the median wait,
and the waiting room is still far from clear after twelve hours. Beds alone do most of it; nurses alone do little, because the ratio
only bites when the ED is far over its beds. Boarding stays low here because the ED itself is the bottleneck: it admits patients no faster
than it can treat them, so the wards' slowdown is felt as a few boarders late in the day rather than a crisis. That is a property of this
department's numbers, and a department whose wards run fuller would show the opposite.

## 4. What is not measured

- The staffing optimiser is a small exact solve; its value is the simulation's, which is measured above.
- Nothing clinical: the simulator has no diagnoses, treatments or outcomes, only times, levels and dispositions.
- Real EHR feeds, HL7v2, and a department whose rules differ from this generator's.
"""
    OUT.write_text(doc)
    print(doc)


if __name__ == "__main__":
    main()
