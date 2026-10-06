"""Integration, end-to-end and security tests against a real Postgres."""
import uuid

import pytest

from core import db, jobs, scenario
from ed import world

from .conftest import bearer

DIRECTOR, CHARGE, VIEWER, OTHER = bearer("hosp", "ops_director"), bearer("hosp", "charge_nurse"), bearer("hosp", "viewer"), bearer("other", "ops_director")


def test_nothing_works_before_a_hospital_is_loaded(client):
    assert client.get("/v1/capacity/current", headers=VIEWER).json()["code"] == "no_hospital" and client.post("/v1/simulations/flow", headers=CHARGE, json={}).json()["code"] == "no_hospital"


@pytest.fixture(scope="module")
def demo(client):
    results, _ = scenario.run(client, drain=jobs.drain)
    return results


def one(sql, params=(), tenant=None):
    with db.tx(tenant) as c:
        return c.execute(sql, params).fetchone()


def test_demo_journey(demo):
    load = demo["load"]["result"]
    assert load["encounters_loaded"] == one("SELECT count(*) AS n FROM encounters")["n"] - demo["adv"]["result"]["new_encounters"] and load["beds"] == {"ed": 32, "ward": 122, "ct_scanners": 2}
    assert one("SELECT count(*) AS n FROM encounters WHERE patient_token ~ '^[0-9a-f]{16}$'")["n"] == one("SELECT count(*) AS n FROM encounters")["n"]            # every token is a hash
    assert one("SELECT count(*) AS n FROM beds")["n"] == 154 and one("SELECT count(*) AS n FROM bed_occupancy WHERE end_ts IS NULL")["n"] == demo["cap0"]["ed"]["occupied"] + demo["cap0"]["wards"]["occupied"]
    c0 = demo["cap0"]
    assert c0["ed"]["waiting"] == 0 and c0["alerts"] == [] and c0["staff"]["nurses_on"] == 9 and c0["ed"]["beds"] == 32
    assert len(demo["arr"]["hours"]) == 24 and demo["arr"]["model"]["mae_profile"] < demo["arr"]["model"]["mae_same_hour_last_week"] and one("SELECT count(*) AS n FROM forecasts WHERE forecast_type = 'arrivals'")["n"] == 24
    assert len(demo["beds"]["hours"]) == 24 and demo["beds"]["occupied"] == c0["wards"]["occupied"]
    s0 = demo["sim0"]
    assert s0["runs"] == 30 and len(s0["baseline"]["hourly"]) == 12 and "with_actions" not in s0 and s0["baseline"]["kpis"]["peak_waiting"]["p50"] < 15
    adv = demo["adv"]["result"]
    assert adv["minute"] == world.NOW + 240 and adv["census_now"]["waiting"] >= 10 and adv["new_encounters"] >= 50 and one("SELECT now_minute FROM hospitals")["now_minute"] == world.NOW + 240
    assert one("SELECT count(*) AS n FROM encounters WHERE arrived_at > %s", [demo["load"]["result"]["clock"]])["n"] == adv["new_encounters"]
    f = demo["fhir"]
    assert len(f["accepted"]) == 2 and f["rejected"][0]["why"].startswith("period.start") and one("SELECT jsonb_array_length(told->'arrivals') AS n FROM hospitals")["n"] == 2
    c1 = demo["cap1"]
    assert c1["ed"]["waiting"] >= 10 and any(a["level"] == "high" for a in c1["alerts"]) and c1["ed"]["occupied"] == 32
    s1 = demo["sim1"]
    assert s1["with_actions"]["kpis"]["peak_waiting"]["p50"] < s1["baseline"]["kpis"]["peak_waiting"]["p50"] and s1["difference"]["door_to_bed_median"] < 0 and s1["known_constraints"]["discharge_slowdown_factor"] == 0.4
    st = demo["staff"]
    assert st["status"] == "proposed" and st["rationale"]["solver"] in ("OPTIMAL", "FEASIBLE") and st["actions"]["surge_beds"] == 6 and len(st["call_ins"]) >= 1 and one("SELECT status FROM recommendations")["status"] == "approved"
    assert demo["self"]["status"] == 403 and demo["approve"]["status"] == "approved" and set(demo["approve"]["staff_called_in"]) == {c["name"] for c in st["call_ins"]}
    assert one("SELECT count(*) AS n FROM staff_shifts WHERE status = 'called_in'")["n"] == len(st["call_ins"])
    c2 = demo["cap2"]
    assert c2["ed"]["surge_beds_open"] == 6 and c2["ed"]["beds"] == 38 and c2["active_actions"]["discharge_push"] == 1.5
    assert demo["recs"]["items"][0]["status"] == "approved" and demo["audit"]["chain_valid"] and {"hospital.loaded", "hospital.advanced", "fhir.received", "simulation.run", "recommendation.proposed", "recommendation.approved"} <= {e["action"] for e in demo["audit"]["events"]}


def test_validation(client, demo):
    post = lambda path, body, h=CHARGE: client.post(path, headers=h, json=body)     # noqa: E731
    enc = lambda **kw: {"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Encounter", "id": "x", "class": {"code": "EMER"}, "priority": {"coding": [{"code": "3"}]}, "subject": {"reference": "Patient/zz"}, "period": {"start": "2026-10-05T11:30:00Z"}, "extension": [{"url": "u/patient-age", "valueInteger": 40}], **kw}}]}     # noqa: E731
    assert post("/v1/events/fhir", enc(**{"class": {"code": "IMP"}})).json()["rejected"][0]["why"] == "not an emergency Encounter"
    assert post("/v1/events/fhir", enc(priority={"coding": [{"code": "9"}]})).json()["rejected"][0]["why"] == "priority must be ESI 1-5"
    assert post("/v1/events/fhir", enc(period={"start": "2026-10-06T11:30:00Z"})).json()["rejected"][0]["why"].startswith("arrival must be")
    assert post("/v1/events/fhir", enc(subject={"reference": "Patient/transfer-1"})).json()["rejected"][0]["why"] == "already announced"
    assert post("/v1/events/fhir", enc(extension=[])).json()["rejected"][0]["why"].startswith("missing")
    assert post("/v1/simulations/flow", {"hours": 1}).status_code == 422 and post("/v1/simulations/flow", {"actions": {"surge_beds": 40}}).status_code == 422
    assert post(f"/v1/recommendations/{uuid.uuid4()}/approve", {}, DIRECTOR).status_code == 404 and post(f"/v1/recommendations/{demo['staff']['recommendation_id']}/approve?decision=rejected", {}, DIRECTOR).json()["code"] == "already_decided"
    assert post("/v1/hospital:advance", {"hours": 40}, DIRECTOR).status_code == 422


def test_roles(client, demo):
    assert client.get("/v1/capacity/current").status_code == 401 and client.get("/v1/capacity/current", headers=VIEWER).status_code == 200
    for path in ("/v1/events/fhir", "/v1/simulations/flow", "/v1/optimizations/staffing"):
        assert client.post(path, headers=VIEWER, json={}).status_code == 403, path
    assert client.post(f"/v1/recommendations/{demo['staff']['recommendation_id']}/approve", headers=CHARGE).status_code == 403
    assert client.post("/v1/hospital:advance", headers=CHARGE, json={"hours": 1}).status_code == 403 and client.post("/v1/hospital:load", headers=CHARGE, json={}).status_code == 403
    assert client.get("/v1/audit", headers=CHARGE).status_code == 403 and client.get("/v1/audit", headers=DIRECTOR).status_code == 200


def test_another_hospital_sees_nothing(client, demo, seeded):
    assert client.get("/v1/capacity/current", headers=OTHER).json()["code"] == "no_hospital"
    for table in ("hospitals", "encounters", "encounter_events", "beds", "bed_occupancy", "staff_shifts", "forecasts", "recommendations"):
        assert one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["other"])["n"] == 0 < one(f"SELECT count(*) AS n FROM {table}", tenant=seeded["hosp"])["n"], table
    with pytest.raises(Exception, match="row-level security"), db.tx(seeded["other"]) as c:
        c.execute("INSERT INTO hospitals (id, tenant_id, name, model_seed, now_minute, start_date) VALUES (%s,%s,'x',1,0,'2026-06-15')", [uuid.uuid4(), seeded["hosp"]])


def test_idempotent_writes(client, demo):
    body = {"hours": 16, "runs": 30, "known_constraints": {"discharge_slowdown_factor": 0.4, "hours": 6}}
    again = client.post("/v1/optimizations/staffing", headers={**CHARGE, "Idempotency-Key": "demo-1:staff"}, json=body)
    assert again.status_code == 201 and again.headers["Idempotent-Replay"] == "true" and again.json()["recommendation_id"] == demo["staff"]["recommendation_id"] and one("SELECT count(*) AS n FROM recommendations")["n"] == 1
    assert client.post("/v1/optimizations/staffing", headers={**CHARGE, "Idempotency-Key": "demo-1:staff"}, json={**body, "hours": 8}).json()["code"] == "idempotency_key_reused"
    assert client.post("/v1/hospital:load", headers=DIRECTOR, json={"seed": 17}).json()["code"] == "hospital_exists"
