"""Public API (modular monolith). Blueprint services map to: ehr-adapter (a small FHIR Encounter reader), patient-flow and
bed-state (the live state), arrival-forecast, queueing-simulation (the simulator run forward from the live state), staffing-optimizer
(CP-SAT), capacity-alert (the flags on the capacity view), deidentified-analytics (tokens and age bands only), audit, ops-dashboard.
Not built: HL7v2, a real EHR, clinical content of any kind; this is operational decision support with a person approving every action.

    uvicorn ed.api:app          python -m core.jobs ed.api      # the worker
"""
import datetime
import functools
import hashlib
import uuid
from typing import Literal

import numpy as np
from fastapi import Depends, Header, Query
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from core import audit, db, jobs
from core.app import Ctx, Problem, create_app, run

from . import engine, world

READ = {"capacity:read", "jobs:read"}
WORK = READ | {"events:write", "simulations:run", "recommendations:propose"}
PERMISSIONS = {"viewer": READ, "charge_nurse": WORK, "ops_director": WORK | {"hospital:load", "hospital:advance", "recommendations:approve", "audit:read"}}
app = create_app("ed-patient-flow", PERMISSIONS)
auth = app.state.auth
IdemKey = Header(None, alias="Idempotency-Key")
START = datetime.datetime(2026, 6, 15, 0, 0, tzinfo=datetime.timezone.utc)      # minute 0 is a Monday
POOL = [{"name": f"RN {k + 1}", "skill": "ED" if k < 6 else "agency", "cost_per_shift": 480 if k < 6 else 760, "shifts": ["day", "evening", "night"]} for k in range(10)]


def ts(minute):
    return START + datetime.timedelta(minutes=float(minute))


def worker_ctx(job):
    return Ctx(job["tenant_id"], uuid.UUID(job["payload"]["actor_id"]), "worker", "system")


def hospital(c):
    h = c.execute("SELECT * FROM hospitals").fetchone()
    if not h:
        raise Problem(409, "no_hospital", "load a hospital first: POST /v1/hospital:load")
    return h


def token(seed, idx):
    return hashlib.sha256(f"{seed}:{idx}".encode()).hexdigest()[:16]


def age_band(age):
    return f"{(age // 10) * 10}-{(age // 10) * 10 + 9}"


def told_key(told):
    import json
    return json.dumps(told, sort_keys=True)


@functools.lru_cache(maxsize=8)
def _state(seed, now, told_json):
    """The live state at `now`: the history, then the generator moved forward with what it was told (surges, slowdowns, announced arrivals)."""
    import json
    told = json.loads(told_json)
    h = engine.history(seed)
    if now == world.NOW:
        return h["state"], h["patients"], []
    out = world.run(seed + 7, now, start_state=engine._copy_state(h["state"]), surge=told.get("surge"), slowdown=told.get("slowdown"), extra_arrivals=told.get("arrivals"), record_from=world.NOW)
    return out["state"], out["patients"], out["census"]


def live(hp):
    return _state(hp["model_seed"], hp["now_minute"], told_key(hp["told"]))


@functools.lru_cache(maxsize=16)
def _beds(seed, now, told_json, hours):
    return engine.forecast_beds(engine.fit(seed), _state(seed, now, told_json)[0], now, hours)


# ---------------------------------------------------------------- load and advance

class LoadIn(BaseModel):
    seed: int = Field(17, ge=0, le=10 ** 6)


@app.post("/v1/hospital:load", status_code=202, tags=["hospital"], summary="(+) Load the invented department at a Tuesday 07:00: sixteen weeks of history, the live state, beds, roster; fit the models and report them")
def load_hospital(body: LoadIn, ctx: Ctx = Depends(auth("hospital:load")), idem: str | None = IdemKey):
    def work(c):
        if c.execute("SELECT 1 FROM hospitals LIMIT 1").fetchone():
            raise Problem(409, "hospital_exists")
        return 202, jobs.enqueue(c, ctx, "hospital.load", body.model_dump())
    return run(ctx, idem, body, work)


def encounter_rows(t, seed, patients, since, upto):
    enc, events = [], []
    for p in patients:
        if p["arrived"] < since or p["arrived"] > upto:
            continue
        eid = uuid.uuid5(t, f"enc-{p['idx']}")
        enc.append((eid, t, p.get("token") or token(seed, p["idx"]), ts(p["arrived"]), p["acuity"], p.get("disposition"), ts(p["departed"]) if "departed" in p else None, age_band(p["age"]), p["complaint"], "fhir" if p.get("token") else "simulator", p["idx"]))
        events.append((uuid.uuid5(t, f"ev-{p['idx']}-arrive"), t, eid, "arrived", ts(p["arrived"]), "waiting room", Jsonb({"triage_level": p["acuity"]})))
        for k, loc in (("bed_at", "ED bed"), ("ct_at", "CT"), ("dispo_at", "ED bed"), ("ward_bed_at", "ward"), ("departed", "exit")):
            if k in p and p[k] <= upto:
                events.append((uuid.uuid5(t, f"ev-{p['idx']}-{k}"), t, eid, k.replace("_at", ""), ts(p[k]), loc, Jsonb({"disposition": p["disposition"]} if k == "dispo_at" else {})))
    return enc, events


@jobs.handler("hospital.load")
def load_job(c, job):
    t, seed, now = job["tenant_id"], job["payload"]["seed"], world.NOW
    hid = uuid.uuid5(t, "hospital")
    c.execute("INSERT INTO hospitals (id, tenant_id, name, model_seed, now_minute, start_date) VALUES (%s,%s,'Riverbend General ED',%s,%s,%s)", [hid, t, seed, now, START.date()])
    m = engine.fit(seed)
    state, patients, _ = live({"model_seed": seed, "now_minute": now, "told": {}})
    enc, events = encounter_rows(t, seed, patients, now - 56 * 1440, now)
    db.load(c, "encounters", ["id", "tenant_id", "patient_token", "arrived_at", "triage_level", "disposition", "departed_at", "age_band", "complaint", "source", "sim_index"], enc)
    db.load(c, "encounter_events", ["id", "tenant_id", "encounter_id", "event_type", "occurred_at", "location", "metadata"], events)
    beds = [(uuid.uuid5(t, f"bed-ED-{k}"), t, "ED", "treatment", "available", k < 4, f"ED-{k + 1:02d}") for k in range(world.ED_BEDS)]
    beds += [(uuid.uuid5(t, f"bed-W-{k}"), t, f"Ward {chr(65 + k // 31)}", "inpatient", "available", k % 8 == 0, f"W-{k + 1:03d}") for k in range(world.WARD_BEDS)]
    db.load(c, "beds", ["id", "tenant_id", "unit", "bed_type", "status", "isolation_capable", "label"], beds)
    sim = state["sim"]
    occ = [(uuid.uuid5(t, f"bed-ED-{k}"), ts(sim["patients"][i]["bed_at"]), None, uuid.uuid5(t, f"enc-{i}"), t) for k, i in enumerate(sorted(sim["in_ed"]))]
    occ += [(uuid.uuid5(t, f"bed-W-{k}"), ts(w[2]), None, uuid.uuid5(t, f"enc-{w[1]}") if w[1] is not None and sim["patients"][w[1]]["arrived"] >= now - 56 * 1440 else None, t) for k, w in enumerate(sim["ward"])]
    db.load(c, "bed_occupancy", ["bed_id", "start_ts", "end_ts", "encounter_id", "tenant_id"], occ)
    c.execute("UPDATE beds SET status = 'occupied' WHERE id IN (SELECT bed_id FROM bed_occupancy WHERE end_ts IS NULL)")
    shifts = []
    day0 = int(now // 1440) * 1440
    for d in range(2):
        for name, (a, b, n) in world.SHIFTS.items():
            s0 = day0 + d * 1440 + a * 60
            s1 = s0 + ((b - a) % 24) * 60
            for k in range(n):
                shifts.append((uuid.uuid5(t, f"shift-{d}-{name}-{k}"), t, "nurse", "ED", ts(s0), ts(s1), "ED", "rostered", f"Nurse {name[0].upper()}{k + 1}", 0))
            for p in POOL:
                shifts.append((uuid.uuid5(t, f"oncall-{d}-{name}-{p['name']}"), t, "nurse", p["skill"], ts(s0), ts(s1), "ED", "on_call", p["name"], p["cost_per_shift"]))
    db.load(c, "staff_shifts", ["id", "tenant_id", "role", "skill", "start_ts", "end_ts", "unit", "status", "staff_name", "cost_per_shift"], shifts)
    out = {"hospital_id": hid, "clock": str(ts(now)), "minute": now, "encounters_loaded": len(enc), "events_loaded": len(events), "beds": {"ed": world.ED_BEDS, "ward": world.WARD_BEDS, "ct_scanners": world.CT_SCANNERS}, "roster": {k: v[2] for k, v in world.SHIFTS.items()}, "nurse_ratio": world.NURSE_RATIO,
           "models": {"version": engine.VERSION, **m["reports"]}, "deidentification": "patient_token = sha256 of the simulator index; age in ten-year bands; no names, dates of birth or free text anywhere"}
    audit.record(c, worker_ctx(job), "hospital.loaded", "hospital", hid, {"encounters": len(enc), "minute": now})
    return out


class Surge(BaseModel):
    in_minutes: int = Field(ge=0, le=720)
    patients: int = Field(ge=1, le=80)
    spread_min: int = Field(60, ge=5, le=240)
    complaint: Literal["injury", "respiratory", "fever", "other"] = "injury"


class Slowdown(BaseModel):
    from_minutes: int = Field(ge=0, le=720)
    hours: int = Field(ge=1, le=24)
    factor: float = Field(ge=0.1, le=1.0, description="ward discharge processing rate, 1 = normal")


class AdvanceIn(BaseModel):
    hours: int = Field(ge=1, le=24)
    surge: Surge | None = None
    slowdown: Slowdown | None = None


@app.post("/v1/hospital:advance", status_code=202, tags=["hospital"], summary="(+) The clock moves: the generator runs the department forward, told what happens (a surge, a discharge slowdown). The service sees the encounters and events, not the telling")
def advance(body: AdvanceIn, ctx: Ctx = Depends(auth("hospital:advance")), idem: str | None = IdemKey):
    def work(c):
        hospital(c)
        return 202, jobs.enqueue(c, ctx, "hospital.advance", body.model_dump())
    return run(ctx, idem, body, work)


@jobs.handler("hospital.advance")
def advance_job(c, job):
    t = job["tenant_id"]
    hp = hospital(c)
    now0, now = hp["now_minute"], hp["now_minute"] + job["payload"]["hours"] * 60
    told = dict(hp["told"])
    if job["payload"]["surge"]:
        s = job["payload"]["surge"]
        told["surge"] = {"at": now0 + s["in_minutes"], "patients": s["patients"], "spread_min": s["spread_min"], "complaint": s["complaint"]}
    if job["payload"]["slowdown"]:
        s = job["payload"]["slowdown"]
        told["slowdown"] = {"from": now0 + s["from_minutes"], "to": now0 + s["from_minutes"] + s["hours"] * 60, "factor": s["factor"]}
    state, patients, census = _state(hp["model_seed"], now, told_key(told))
    enc, events = encounter_rows(t, hp["model_seed"], patients, now0 + 1e-9, now)
    db.load(c, "encounters", ["id", "tenant_id", "patient_token", "arrived_at", "triage_level", "disposition", "departed_at", "age_band", "complaint", "source", "sim_index"], enc)
    # events for patients who arrived earlier but moved since, and departures
    moved = []
    for p in patients:
        if p["arrived"] <= now0 and p["arrived"] >= now0 - 3 * 1440:
            for k, loc in (("bed_at", "ED bed"), ("ct_at", "CT"), ("dispo_at", "ED bed"), ("ward_bed_at", "ward"), ("departed", "exit")):
                if k in p and now0 < p[k] <= now:
                    moved.append((uuid.uuid5(t, f"ev-{p['idx']}-{k}"), t, uuid.uuid5(t, f"enc-{p['idx']}"), k.replace("_at", ""), ts(p[k]), loc, Jsonb({})))
            if "departed" in p and now0 < p["departed"] <= now:
                c.execute("UPDATE encounters SET disposition = %s, departed_at = %s WHERE id = %s", [p["disposition"], ts(p["departed"]), uuid.uuid5(t, f"enc-{p['idx']}")])
    db.load(c, "encounter_events", ["id", "tenant_id", "encounter_id", "event_type", "occurred_at", "location", "metadata"], events + moved)
    c.execute("UPDATE hospitals SET now_minute = %s, told = %s WHERE id = %s", [now, Jsonb(told), hp["id"]])
    audit.record(c, worker_ctx(job), "hospital.advanced", "hospital", hp["id"], {"from_minute": now0, "to_minute": now, "surge": bool(job["payload"]["surge"]), "slowdown": bool(job["payload"]["slowdown"])})
    sim = state["sim"]
    return {"hospital_id": hp["id"], "clock": str(ts(now)), "minute": now, "new_encounters": len(enc), "new_events": len(events) + len(moved), "census_now": {"waiting": len(sim["waiting"]), "in_ed_beds": len(sim["in_ed"]), "boarding": len(sim["boarding"]), "ward_occupied": len(sim["ward"])},
            "hourly_since": [{"minute": x["minute"], "waiting": x["waiting"], "in_ed": x["in_ed"], "boarding": x["boarding"], "ct_queue": x["ct_queue"]} for x in census if x["minute"] > now0],
            "simulation_truth": {"what": "what the generator was told happens; the service sees only encounters and events", "surge": job["payload"]["surge"], "slowdown": job["payload"]["slowdown"]}}


# ---------------------------------------------------------------- FHIR adapter

class FhirIn(BaseModel):
    resourceType: Literal["Bundle"]
    entry: list[dict] = Field(min_length=1, max_length=200)


@app.post("/v1/events/fhir", status_code=201, tags=["ehr"], summary="A FHIR Bundle of Encounter resources from an EHR or an ambulance service: arrivals announced ahead become part of the live state; nothing clinical is read beyond triage level, age band and a reason code")
def fhir(body: FhirIn, ctx: Ctx = Depends(auth("events:write")), idem: str | None = IdemKey):
    def work(c):
        hp = hospital(c)
        told = dict(hp["told"])
        arrivals = list(told.get("arrivals", []))
        accepted, rejected = [], []
        for e in body.entry:
            r = e.get("resource", {})
            if r.get("resourceType") != "Encounter" or r.get("class", {}).get("code") != "EMER":
                rejected.append({"id": r.get("id"), "why": "not an emergency Encounter"})
                continue
            try:
                start = datetime.datetime.fromisoformat(r["period"]["start"].replace("Z", "+00:00"))
                acuity = int(r["priority"]["coding"][0]["code"])
                age = int(next(x for x in r.get("extension", []) if x["url"].endswith("patient-age"))["valueInteger"])
                reason = r["reasonCode"][0]["text"] if r.get("reasonCode") else "other"
                tok = r["subject"]["reference"].split("/")[-1]
            except (KeyError, IndexError, StopIteration, ValueError) as ex:
                rejected.append({"id": r.get("id"), "why": f"missing or malformed: {type(ex).__name__}"})
                continue
            if start.tzinfo is None:
                rejected.append({"id": r.get("id"), "why": "period.start needs a timezone"})
                continue
            if not 1 <= acuity <= 5:
                rejected.append({"id": r.get("id"), "why": "priority must be ESI 1-5"})
                continue
            at = (start - START).total_seconds() / 60
            if at < hp["now_minute"] - 60 or at > hp["now_minute"] + 12 * 60:
                rejected.append({"id": r.get("id"), "why": "arrival must be within the last hour or the next twelve"})
                continue
            if any(a["token"] == hashlib.sha256(tok.encode()).hexdigest()[:16] for a in arrivals):
                rejected.append({"id": r.get("id"), "why": "already announced"})
                continue
            arrivals.append({"at": round(at, 1), "acuity": acuity, "age": age, "complaint": reason if reason in world.COMPLAINTS else "other", "token": hashlib.sha256(tok.encode()).hexdigest()[:16]})
            accepted.append({"id": r.get("id"), "expected_at": str(ts(at)), "triage_level": acuity, "age_band": age_band(age), "complaint": reason})
        told["arrivals"] = arrivals
        c.execute("UPDATE hospitals SET told = %s WHERE id = %s", [Jsonb(told), hp["id"]])
        audit.record(c, ctx, "fhir.received", "hospital", hp["id"], {"accepted": len(accepted), "rejected": len(rejected)})
        return 201, {"accepted": accepted, "rejected": rejected, "announced_arrivals_pending": len(arrivals), "note": "the subject reference is hashed; only triage level, age band and the reason text are kept"}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- capacity, forecasts

def active_actions(c):
    rows = c.execute("SELECT actions FROM recommendations WHERE status = 'approved'").fetchall()
    out = {"surge_beds": 0, "extra_nurses": {}, "discharge_push": None}
    for r in rows:
        a = r["actions"]
        out["surge_beds"] += a.get("surge_beds", 0)
        for s, n in (a.get("extra_nurses") or {}).items():
            out["extra_nurses"][s] = out["extra_nurses"].get(s, 0) + n
        if a.get("discharge_push"):
            out["discharge_push"] = max(out["discharge_push"] or 0, a["discharge_push"])
    return out


@app.get("/v1/capacity/current", tags=["capacity"], summary="The department now: waiting by triage level, beds, boarding, imaging queue, wards, nurses on and the ratio, with alerts where a threshold is crossed")
def capacity(ctx: Ctx = Depends(auth("capacity:read"))):
    with db.tx(ctx.tenant_id) as c:
        hp = hospital(c)
        acts = active_actions(c)
    state, patients, _ = live(hp)
    sim, now = state["sim"], hp["now_minute"]
    pts = sim["patients"]
    waiting = [pts[i] for i in sim["waiting"]]
    nurses, shift = world.nurses_on(now)
    nurses += acts["extra_nurses"].get(shift, 0)
    in_ed = len(sim["in_ed"])
    ratio = round(in_ed / nurses, 2)
    longest = max([now - p["arrived"] for p in waiting], default=0)
    alerts = []
    if len(waiting) >= 10:
        alerts.append({"level": "high" if len(waiting) >= 20 else "watch", "what": f"{len(waiting)} waiting for a bed; longest {round(longest)} min"})
    if ratio > world.NURSE_RATIO:
        alerts.append({"level": "high", "what": f"{in_ed} patients in beds for {nurses} nurses: {ratio} per nurse against {world.NURSE_RATIO}"})
    if len(sim["ct_queue"]) >= 3:
        alerts.append({"level": "watch", "what": f"{len(sim['ct_queue'])} waiting for CT"})
    if len(sim["boarding"]) >= 4:
        alerts.append({"level": "high", "what": f"{len(sim['boarding'])} admitted patients boarding in ED beds"})
    if world.WARD_BEDS - len(sim["ward"]) <= 4:
        alerts.append({"level": "watch", "what": f"{world.WARD_BEDS - len(sim['ward'])} ward beds free"})
    return jsonable_encoder({"clock": str(ts(now)), "minute": now, "shift": shift, "ed": {"beds": world.ED_BEDS + acts["surge_beds"], "surge_beds_open": acts["surge_beds"], "occupied": in_ed, "boarding": len(sim["boarding"]), "waiting": len(waiting), "waiting_by_triage": {str(a): sum(p["acuity"] == a for p in waiting) for a in range(1, 6)}, "longest_wait_min": round(longest), "ct_queue": len(sim["ct_queue"]), "ct_in_use": sim["ct_busy"]},
                             "wards": {"beds": world.WARD_BEDS, "occupied": len(sim["ward"]), "free": world.WARD_BEDS - len(sim["ward"])}, "staff": {"nurses_on": nurses, "patients_per_nurse": ratio, "ratio_limit": world.NURSE_RATIO}, "alerts": alerts, "active_actions": acts,
                             "arrivals_last_4_hours": sum(1 for p in patients if now - 240 <= p["arrived"] <= now)})


@app.get("/v1/forecast/arrivals", tags=["forecast"], summary="Expected arrivals per hour with an 80% Poisson band and the share at triage 1-2, from the department's own day-of-week and hour profile")
def forecast_arrivals(hours: int = Query(24, ge=1, le=48), ctx: Ctx = Depends(auth("capacity:read"))):
    with db.tx(ctx.tenant_id) as c:
        hp = hospital(c)
        m = engine.fit(hp["model_seed"])
        rows = engine.forecast_arrivals(m, hp["now_minute"], hours)
        if not c.execute("SELECT 1 FROM forecasts WHERE forecast_type = 'arrivals' AND detail->>'clock' = %s LIMIT 1", [str(hp["now_minute"])]).fetchone():      # stored once per clock, not on every read
            rid = uuid.uuid7()
            db.load(c, "forecasts", ["id", "tenant_id", "forecast_type", "window_start", "horizon_min", "value", "lower", "upper", "model_version", "detail"], [(uuid.uuid7(), ctx.tenant_id, "arrivals", ts(r["minute"]), 60, r["expected"], r["lower"], r["upper"], engine.VERSION, Jsonb({"acuity_1_2_share": r["acuity_1_2_share"], "run": str(rid), "clock": str(hp["now_minute"])})) for r in rows])
    return jsonable_encoder({"clock": str(ts(hp["now_minute"])), "hours": [{**r, "at": str(ts(r["minute"]))} for r in rows], "total_expected": round(sum(r["expected"] for r in rows), 1), "model": m["reports"]["arrivals"], "model_version": engine.VERSION})


@app.get("/v1/forecast/beds", tags=["forecast"], summary="Ward discharges and admissions expected hour by hour, the projected free beds and the boarding that implies")
def forecast_beds(hours: int = Query(24, ge=1, le=48), ctx: Ctx = Depends(auth("capacity:read"))):
    with db.tx(ctx.tenant_id) as c:
        hp = hospital(c)
    m = engine.fit(hp["model_seed"])
    fb = _beds(hp["model_seed"], hp["now_minute"], told_key(hp["told"]), hours)
    first_short = next((r for r in fb["hours"] if r["projected_free_beds"] < 2), None)
    return jsonable_encoder({"clock": str(ts(hp["now_minute"])), **fb, "ward_turnover_per_bed_hour": round(m["turnover"], 4), "first_hour_short_of_beds": first_short and str(ts(first_short["minute"])), "model": m["reports"]["ward_stays"], "model_version": engine.VERSION})


# ---------------------------------------------------------------- simulation, staffing, recommendations

class Actions(BaseModel):
    surge_beds: int = Field(0, ge=0, le=12)
    extra_nurses: dict[Literal["day", "evening", "night"], int] = Field(default_factory=dict)
    discharge_push: float | None = Field(None, ge=1.0, le=3.0, description="ward discharge processing multiplier")


class Constraint(BaseModel):
    discharge_slowdown_factor: float = Field(ge=0.1, le=1.0)
    hours: int = Field(ge=1, le=24)


class SimIn(BaseModel):
    hours: int = Field(12, ge=2, le=24)
    runs: int = Field(30, ge=5, le=100)
    actions: Actions | None = None
    known_constraints: Constraint | None = Field(None, description="what bed management has announced, such as a discharge slowdown")
    include_approved_actions: bool = True


@app.post("/v1/simulations/flow", status_code=201, tags=["simulation"], summary="Run the department forward from the live state many times: waiting, boarding, imaging queue and KPIs with bands; with actions, the same runs with the actions applied, side by side")
def simulate(body: SimIn, ctx: Ctx = Depends(auth("simulations:run")), idem: str | None = IdemKey):
    def work(c):
        hp = hospital(c)
        acts = active_actions(c) if body.include_approved_actions else {"surge_beds": 0, "extra_nurses": {}, "discharge_push": None}
        state, _, _ = live(hp)
        now = hp["now_minute"]
        slow = {"from": now, "to": now + body.known_constraints.hours * 60, "factor": body.known_constraints.discharge_slowdown_factor} if body.known_constraints else None
        base = engine.simulate(hp["model_seed"], state, body.hours, body.runs, slowdown=slow, actions=acts)
        out = {"clock": str(ts(now)), "hours": body.hours, "runs": body.runs, "known_constraints": body.known_constraints and body.known_constraints.model_dump(), "approved_actions_included": acts, "baseline": base}
        if body.actions:
            merged = {"surge_beds": acts["surge_beds"] + body.actions.surge_beds, "extra_nurses": {s: acts["extra_nurses"].get(s, 0) + body.actions.extra_nurses.get(s, 0) for s in set(acts["extra_nurses"]) | set(body.actions.extra_nurses)}, "discharge_push": body.actions.discharge_push or acts["discharge_push"]}
            with_actions = engine.simulate(hp["model_seed"], state, body.hours, body.runs, slowdown=slow, actions=merged)
            out["with_actions"] = with_actions
            out["actions"] = body.actions.model_dump()
            out["difference"] = {k: round(with_actions["kpis"][k]["p50"] - base["kpis"][k]["p50"], 1) for k in base["kpis"]}
        audit.record(c, ctx, "simulation.run", "hospital", hp["id"], {"hours": body.hours, "runs": body.runs, "with_actions": bool(body.actions)})
        out["how"] = "the simulator that generates this department, run forward from its live state with fresh randomness; on a real department this twin would be an approximation and its bands wider"
        return 201, out
    return run(ctx, idem, body, work)


class StaffIn(BaseModel):
    hours: int = Field(16, ge=4, le=24)
    runs: int = Field(30, ge=5, le=100)
    known_constraints: Constraint | None = None
    max_call_ins_per_shift: int = Field(4, ge=0, le=6)


@app.post("/v1/optimizations/staffing", status_code=201, tags=["recommendations"], summary="Nurses to call in per shift so the P80 census meets the ratio (CP-SAT over the on-call pool, cheapest first), plus surge beds and a discharge push where the simulation says they help. Proposed, not active, until a director approves")
def staffing(body: StaffIn, ctx: Ctx = Depends(auth("recommendations:propose")), idem: str | None = IdemKey):
    def work(c):
        hp = hospital(c)
        state, _, _ = live(hp)
        now = hp["now_minute"]
        slow = {"from": now, "to": now + body.known_constraints.hours * 60, "factor": body.known_constraints.discharge_slowdown_factor} if body.known_constraints else None
        base = engine.simulate(hp["model_seed"], state, body.hours, body.runs, slowdown=slow)
        census = {}
        for r in base["hourly"]:
            sh = world.nurses_on(r["minute"])[1]
            census.setdefault(sh, []).append((r["in_ed"]["p90"] + 0.5 * r["waiting"]["p90"]) * 0.5 + (r["in_ed"]["p50"] + 0.5 * r["waiting"]["p50"]) * 0.5)      # about P80 of patients needing nursing: those in beds plus half-weight for the waiting room
        p80 = {sh: max(v) for sh, v in census.items()}
        plan = engine.staffing(p80, POOL, max_per_shift=body.max_call_ins_per_shift)
        extra = {}
        for ci in plan["call_ins"]:
            extra[ci["shift"]] = extra.get(ci["shift"], 0) + 1
        actions = {"extra_nurses": extra, "surge_beds": 6 if base["kpis"]["peak_waiting"]["p50"] >= 10 else 0, "discharge_push": 1.5 if base["kpis"]["peak_boarding"]["p50"] >= 2 or slow else None}
        with_actions = engine.simulate(hp["model_seed"], state, body.hours, body.runs, slowdown=slow, actions=actions)
        effect = {k: {"before_p50": base["kpis"][k]["p50"], "after_p50": with_actions["kpis"][k]["p50"], "change": round(with_actions["kpis"][k]["p50"] - base["kpis"][k]["p50"], 1)} for k in base["kpis"]}
        rid = uuid.uuid7()
        rationale = {"census_p80_by_shift": {k: round(v, 1) for k, v in p80.items()}, "census_counts": "patients in beds plus half of those waiting", "ratio_limit": world.NURSE_RATIO, "solver": plan["status"], "by_shift": plan["by_shift"], "cost": plan["cost"], "peak_waiting_p50_without": base["kpis"]["peak_waiting"]["p50"], "peak_boarding_p50_without": base["kpis"]["peak_boarding"]["p50"], "known_constraints": body.known_constraints and body.known_constraints.model_dump(), "runs": body.runs, "hours": body.hours}
        c.execute("INSERT INTO recommendations (id, tenant_id, type, rationale, expected_effect, actions, created_by) VALUES (%s,%s,'staffing_and_capacity',%s,%s,%s,%s)", [rid, ctx.tenant_id, Jsonb(rationale), Jsonb(effect), Jsonb(actions), ctx.actor_id])
        for ci in plan["call_ins"]:
            c.execute("UPDATE staff_shifts SET status = 'proposed' WHERE staff_name = %s AND status = 'on_call' AND start_ts = (SELECT min(start_ts) FROM staff_shifts s2 WHERE s2.staff_name = staff_shifts.staff_name AND s2.status = 'on_call' AND s2.start_ts >= %s AND s2.unit = 'ED')", [ci["name"], ts(now - 8 * 60)])
        audit.record(c, ctx, "recommendation.proposed", "recommendation", rid, {"call_ins": len(plan["call_ins"]), "surge_beds": actions["surge_beds"], "cost": plan["cost"]})
        return 201, {"recommendation_id": rid, "status": "proposed", "actions": actions, "call_ins": plan["call_ins"], "rationale": rationale, "expected_effect": effect, "note": "nothing changes in the department until an operations director approves this; the simulation is the evidence, the person is the decision"}
    return run(ctx, idem, body, work)


@app.post("/v1/recommendations/{rec_id}/approve", tags=["recommendations"], summary="An operations director approves (or rejects) a proposed recommendation; only then do its actions count in the capacity view and later simulations")
def approve(rec_id: uuid.UUID, decision: Literal["approved", "rejected"] = Query("approved"), ctx: Ctx = Depends(auth("recommendations:approve")), idem: str | None = IdemKey):
    def work(c):
        rec = c.execute("SELECT * FROM recommendations WHERE id = %s FOR UPDATE", [rec_id]).fetchone()
        if not rec:
            raise Problem(404, "recommendation_not_found")
        if rec["status"] != "proposed":
            raise Problem(409, "already_decided", f"this recommendation is {rec['status']}")
        if rec["created_by"] == ctx.actor_id:
            raise Problem(409, "self_approval", "the person who proposed a recommendation cannot approve it")
        c.execute("UPDATE recommendations SET status = %s, approved_by = %s, decided_at = now() WHERE id = %s", [decision, ctx.actor_id, rec_id])
        c.execute("UPDATE staff_shifts SET status = %s WHERE status = 'proposed'", ["called_in" if decision == "approved" else "on_call"])
        audit.record(c, ctx, f"recommendation.{decision}", "recommendation", rec_id, {"actions": rec["actions"]})
        return 200, {"recommendation_id": rec_id, "status": decision, "actions_now_active": rec["actions"] if decision == "approved" else {}, "staff_called_in": [r["staff_name"] for r in c.execute("SELECT staff_name FROM staff_shifts WHERE status = 'called_in'")]}
    return run(ctx, idem, {"decision": decision}, work)


@app.get("/v1/recommendations", tags=["recommendations"], summary="(+) Recommendations with their status, rationale and expected effect")
def recommendations(ctx: Ctx = Depends(auth("capacity:read"))):
    with db.tx(ctx.tenant_id) as c:
        rows = c.execute("SELECT id, type, status, created_at, decided_at, approved_by, actions, rationale, expected_effect FROM recommendations ORDER BY created_at").fetchall()
    return jsonable_encoder({"items": rows})
