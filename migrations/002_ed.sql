-- Domain model. Names follow the blueprint's schema; additions are marked (+).

CREATE TABLE hospitals (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants UNIQUE, name text NOT NULL, model_seed int NOT NULL, now_minute int NOT NULL, start_date date NOT NULL, told jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now());   -- (+) seed, clock, and what the generator was told (surges, slowdowns, FHIR arrivals)
SELECT enable_tenant_rls('hospitals');

CREATE TABLE encounters (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, patient_token text NOT NULL, arrived_at timestamptz NOT NULL, triage_level int NOT NULL CHECK (triage_level BETWEEN 1 AND 5), disposition text, departed_at timestamptz,
  age_band text NOT NULL, complaint text NOT NULL, source text NOT NULL DEFAULT 'simulator', sim_index int);   -- (+) de-identified features and the simulator's index
SELECT enable_tenant_rls('encounters');
CREATE INDEX encounters_arrived ON encounters (tenant_id, arrived_at);

CREATE TABLE encounter_events (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, encounter_id uuid NOT NULL REFERENCES encounters ON DELETE CASCADE, event_type text NOT NULL, occurred_at timestamptz NOT NULL, location text NOT NULL, metadata jsonb NOT NULL DEFAULT '{}');
SELECT enable_tenant_rls('encounter_events');
CREATE INDEX encounter_events_enc ON encounter_events (tenant_id, encounter_id);

CREATE TABLE beds (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, unit text NOT NULL, bed_type text NOT NULL, status text NOT NULL, isolation_capable boolean NOT NULL DEFAULT false, label text NOT NULL);   -- (+) label
SELECT enable_tenant_rls('beds');

CREATE TABLE bed_occupancy (bed_id uuid NOT NULL REFERENCES beds, start_ts timestamptz NOT NULL, end_ts timestamptz, encounter_id uuid REFERENCES encounters, tenant_id uuid NOT NULL REFERENCES tenants, PRIMARY KEY (bed_id, start_ts));
SELECT enable_tenant_rls('bed_occupancy');

CREATE TABLE staff_shifts (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, role text NOT NULL, skill text NOT NULL, start_ts timestamptz NOT NULL, end_ts timestamptz NOT NULL, unit text NOT NULL, status text NOT NULL DEFAULT 'rostered', staff_name text NOT NULL, cost_per_shift numeric NOT NULL DEFAULT 0);   -- (+) status (rostered | on_call | called_in), name, cost
SELECT enable_tenant_rls('staff_shifts');

CREATE TABLE forecasts (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, forecast_type text NOT NULL, window_start timestamptz NOT NULL, horizon_min int NOT NULL, value numeric NOT NULL, lower numeric NOT NULL, upper numeric NOT NULL, model_version text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), detail jsonb NOT NULL DEFAULT '{}');   -- (+) detail
SELECT enable_tenant_rls('forecasts');

CREATE TABLE recommendations (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants, type text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), rationale jsonb NOT NULL, expected_effect jsonb NOT NULL, approved_by uuid,
  status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'approved', 'rejected')), actions jsonb NOT NULL, decided_at timestamptz, created_by uuid NOT NULL);   -- (+) status, the concrete actions, provenance
SELECT enable_tenant_rls('recommendations');
