"""An invented emergency department and the inpatient units behind it, run as a discrete-event simulation minute by minute.
Patients arrive by an hour-of-day and day-of-week profile with an acuity mix; each waits for an ED bed, is treated for a time that
depends on acuity, imaging and how stretched the nurses are, and is admitted or discharged; the admitted wait (board) for an inpatient
bed, which frees when a ward discharges, and wards discharge mostly in the afternoon. Everything is a pure function of a seed.
Nothing here is a real hospital or real patient data."""
import heapq

import numpy as np

HOURLY = np.array([0.55, 0.42, 0.35, 0.30, 0.30, 0.38, 0.55, 0.85, 1.20, 1.45, 1.55, 1.55, 1.50, 1.45, 1.40, 1.35, 1.35, 1.40, 1.45, 1.40, 1.25, 1.05, 0.85, 0.70])   # arrivals per hour, relative
DOW = np.array([1.10, 1.02, 0.98, 0.97, 0.98, 0.95, 1.00])                                        # Monday first
DAILY = 140.0                                                                                      # mean arrivals a day
ACUITY_MIX = {1: 0.02, 2: 0.18, 3: 0.45, 4: 0.28, 5: 0.07}                                        # ESI 1 (resuscitation) to 5
COMPLAINTS = ["chest pain", "abdominal pain", "injury", "respiratory", "neurological", "fever", "other"]
ED_BEDS, CT_SCANNERS, WARD_BEDS = 32, 2, 122
NURSE_RATIO = 4.0                                                                                  # patients per nurse before care slows
SHIFTS = {"day": (7, 15, 9), "evening": (15, 23, 9), "night": (23, 7, 6)}                           # start hour, end hour, nurses rostered
ELECTIVES_PER_DAY = 14                                                                             # ward admissions not through the ED
HISTORY_DAYS = 112                                                                                 # sixteen weeks before the demo clock
NOW = HISTORY_DAYS * 1440 + 7 * 60                                                                 # the demo's clock: a Tuesday 07:00 in week 17
DEMO = {"surge": {"at": NOW + 180, "patients": 30, "spread_min": 60, "complaint": "injury"}, "slowdown": {"from": NOW + 120, "to": NOW + 600, "factor": 0.4}}


def nurses_on(minute):
    h = int(minute // 60) % 24
    for name, (a, b, n) in SHIFTS.items():
        if (a < b and a <= h < b) or (a > b and (h >= a or h < b)):
            return n, name
    return 6, "night"


def arrival_rate(minute):
    """Expected arrivals per minute at a clock minute."""
    d, h = int(minute // 1440) % 7, int(minute // 60) % 24
    return DAILY * DOW[d] * HOURLY[h] / HOURLY.sum() / 60


def draw_patient(r, t, complaint=None, acuity=None, age=None, token=None):
    ac = acuity or int(r.choice(list(ACUITY_MIX), p=list(ACUITY_MIX.values())))
    age = age or int(np.clip(r.normal(48, 22), 1, 97))
    comp = complaint or COMPLAINTS[int(r.choice(len(COMPLAINTS), p=[0.14, 0.18, 0.2, 0.13, 0.07, 0.1, 0.18]))]
    # hidden truths: imaging need, admission, treatment and ward stay
    imaging = r.random() < {1: 0.9, 2: 0.65, 3: 0.4, 4: 0.12, 5: 0.03}[ac] * (1.3 if comp in ("injury", "neurological", "chest pain") else 0.9)
    p_admit = 1 / (1 + np.exp(-(-2.0 + 1.1 * (3 - ac) + 0.028 * (age - 48) + (0.6 if comp in ("chest pain", "respiratory") else 0) + (0.5 if imaging else 0))))
    admit = r.random() < p_admit
    treat = float(np.exp(r.normal(np.log({1: 300, 2: 250, 3: 180, 4: 100, 5: 55}[ac]), 0.35))) + (60 if imaging else 0)
    ward_los = float(np.exp(r.normal(np.log(3.0 * 1440 * (1.3 if ac <= 2 else 1)), 0.5))) if admit else 0.0
    return {"arrived": t, "acuity": ac, "age": age, "complaint": comp, "imaging": imaging, "p_admit": round(float(p_admit), 3), "admit": admit, "treat_min": treat, "ward_min": ward_los, **({"token": token} if token else {})}


def run(seed, until, surge=None, slowdown=None, start_state=None, nurse_extra=None, surge_beds=0, discharge_push=None, arrivals_scale=1.0, record_from=0, extra_arrivals=None):
    """Run the department from minute 0 (or from `start_state`) to `until`. -> {"patients": [...], "state": snapshot at `until`, "census": hourly rows}
    `nurse_extra` {(shift_day_index, shift_name): n} adds nurses; `surge_beds` adds ED beds; `discharge_push` (from, to, factor) speeds ward discharges."""
    r = np.random.default_rng(seed)
    st = start_state["sim"] if start_state else None
    t0 = start_state["t"] if start_state else 0
    ev = []                                                                       # (time, seq, kind, payload)
    seq = 0

    def push(t, kind, p):
        nonlocal seq
        heapq.heappush(ev, (t, seq, kind, p))
        seq += 1
    patients = list(st["patients"]) if st else []
    waiting = list(st["waiting"]) if st else []                                   # indices waiting for an ED bed
    in_ed = set(st["in_ed"]) if st else set()                                     # indices occupying an ED bed
    boarding = list(st["boarding"]) if st else []                                 # admitted, waiting for a ward bed
    ward = list(st["ward"]) if st else []                                         # (ready_minute, idx or None, started_minute) occupying ward beds
    ct_queue, ct_busy = (list(st["ct_queue"]), st["ct_busy"]) if st else ([], 0)
    if st:
        for e in st["events"]:
            push(*e)
    else:
        for k in range(int(WARD_BEDS * 0.86)):                                    # wards start 86% full with staggered discharge-ready times
            ready = float(r.uniform(0, 4 * 1440))
            ward.append((ready, None, ready - 2.5 * 1440))
    # arrivals as a thinned Poisson process
    peak = DAILY * DOW.max() * HOURLY.max() / HOURLY.sum() / 60 * arrivals_scale
    t = t0
    while True:
        t += r.exponential(1 / peak)
        if t > until:
            break
        if r.random() < arrival_rate(t) * arrivals_scale / peak:
            push(t, "arrive", None)
    if surge and t0 <= surge["at"] <= until:
        for k in range(surge["patients"]):
            push(surge["at"] + r.uniform(0, surge["spread_min"]), "arrive", {"complaint": surge["complaint"], "acuity": int(r.choice([2, 3, 4], p=[0.35, 0.45, 0.2]))})
    for a in (extra_arrivals or []):                                               # patients announced from outside (FHIR), with their own features
        if t0 < a["at"] <= until:
            push(float(a["at"]), "arrive", {k: a[k] for k in ("complaint", "acuity", "age", "token") if k in a})
    for d in range(int(t0 // 1440), int(until // 1440) + 1):                       # elective ward admissions, mornings
        for k in range(r.poisson(ELECTIVES_PER_DAY * (0.3 if (d % 7) >= 5 else 1))):
            te = d * 1440 + r.uniform(8 * 60, 14 * 60)
            if t0 <= te <= until:
                push(te, "elective", None)
    for h in range(int(t0 // 60) + 1, int(until // 60) + 1):
        push(h * 60.0, "tick", None)
    census = []

    def beds_total(tm):
        return ED_BEDS + surge_beds

    def discharge_factor(tm):
        f = 1.0
        if slowdown and slowdown["from"] <= tm < slowdown["to"]:
            f *= slowdown["factor"]
        if discharge_push and discharge_push[0] <= tm < discharge_push[1]:
            f *= discharge_push[2]
        return f

    def try_ward(tm):
        """Discharge wards that are ready (afternoon-weighted, rate-limited), then move boarders into free beds."""
        h = int(tm // 60) % 24
        hourly_cap = (6 if 10 <= h < 20 else 1) * discharge_factor(tm) / 60                  # discharges processed per minute
        ready = sorted([w for w in ward if w[0] <= tm], key=lambda w: w[0])
        n = r.poisson(hourly_cap * 60) if int(tm) % 60 == 0 else 0
        for w in ready[:n]:
            ward.remove(w)
        while boarding and len(ward) < WARD_BEDS:
            i = boarding.pop(0)
            p = patients[i]
            p["ward_bed_at"] = tm
            p["departed"] = tm
            in_ed.discard(i)
            ward.append((tm + p["ward_min"], i, tm))
        while len(ward) > WARD_BEDS:                                                           # electives never bump a boarder; cap
            ward.pop()

    def try_ed(tm):
        while waiting and len(in_ed) < beds_total(tm):
            i = waiting.pop(0)
            p = patients[i]
            p["bed_at"] = tm
            in_ed.add(i)
            n, _ = nurses_on(tm)
            n += (nurse_extra or {}).get((int(tm // 1440) % 7, nurses_on(tm)[1]), 0)
            stretch = max(1.0, len(in_ed) / (NURSE_RATIO * n))
            p["stretch"] = round(float(stretch), 2)
            if p["imaging"]:
                push(tm + p["treat_min"] * 0.4 * stretch, "ct_request", i)
            else:
                push(tm + p["treat_min"] * stretch, "dispo", i)

    def ct_next(tm):
        nonlocal ct_busy
        while ct_queue and ct_busy < CT_SCANNERS:
            i = ct_queue.pop(0)
            ct_busy += 1
            patients[i]["ct_at"] = tm
            push(tm + 20, "ct_done", i)

    while ev and ev[0][0] <= until:
        tm, _, kind, p = heapq.heappop(ev)
        if kind == "arrive":
            pat = draw_patient(r, tm, **(p or {}))
            pat["idx"] = len(patients)
            patients.append(pat)
            if pat["acuity"] <= 5:
                waiting.append(pat["idx"])
                waiting.sort(key=lambda i: (patients[i]["acuity"], patients[i]["arrived"]))
            try_ed(tm)
        elif kind == "ct_request":
            ct_queue.append(p)
            ct_next(tm)
        elif kind == "ct_done":
            ct_busy -= 1
            pat = patients[p]
            push(tm + pat["treat_min"] * 0.6 * pat.get("stretch", 1.0), "dispo", p)
            ct_next(tm)
        elif kind == "dispo":
            pat = patients[p]
            pat["dispo_at"] = tm
            if pat["admit"]:
                pat["disposition"] = "admit"
                boarding.append(p)
                boarding.sort(key=lambda i: (patients[i]["acuity"], patients[i]["dispo_at"]))
                try_ward(tm)
            else:
                pat["disposition"] = "discharge"
                pat["departed"] = tm
                in_ed.discard(p)
            try_ed(tm)
        elif kind == "elective":
            if len(ward) < WARD_BEDS:
                ward.append((tm + float(np.exp(r.normal(np.log(2.2 * 1440), 0.4))), None, tm))
        elif kind == "tick":
            try_ward(tm)
            try_ed(tm)
            if tm >= record_from:
                census.append({"minute": int(tm), "waiting": len(waiting), "in_ed": len(in_ed), "boarding": len(boarding), "ward_occupied": len(ward), "ct_queue": len(ct_queue), "nurses": nurses_on(tm)[0] + (nurse_extra or {}).get((int(tm // 1440) % 7, nurses_on(tm)[1]), 0)})
    state = {"t": until, "sim": {"patients": patients, "waiting": waiting, "in_ed": sorted(in_ed), "boarding": boarding, "ward": ward, "ct_queue": ct_queue, "ct_busy": ct_busy, "events": [(e[0], e[2], e[3]) for e in ev if e[2] != "tick" and e[2] != "arrive" and e[2] != "elective"]}}
    return {"patients": patients, "state": state, "census": census}


def waits(patients, since=0):
    """Door-to-bed and boarding minutes for patients who arrived since `since`."""
    door = [p["bed_at"] - p["arrived"] for p in patients if p["arrived"] >= since and "bed_at" in p]
    board = [p["ward_bed_at"] - p["dispo_at"] for p in patients if p["arrived"] >= since and "ward_bed_at" in p]
    los = [p["departed"] - p["arrived"] for p in patients if p["arrived"] >= since and "departed" in p]
    return {"door_to_bed_median": round(float(np.median(door)), 1) if door else None, "door_to_bed_p90": round(float(np.quantile(door, 0.9)), 1) if door else None, "boarding_median": round(float(np.median(board)), 1) if board else None, "boarding_p90": round(float(np.quantile(board, 0.9)), 1) if board else None, "ed_los_median": round(float(np.median(los)), 1) if los else None, "n": len(door)}
