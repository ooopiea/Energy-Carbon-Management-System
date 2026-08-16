# Stage-0 Data Contracts (revise_guide 12.0)

This file freezes the current wire-level shapes of reports, approvals,
commands, ACKs, feedback and disturbance events as of 2026-08-03.
Subsequent stages must not silently break these contracts.

## AgentReport (core/state.py)

| Field           | Type                          | Notes                              |
|-----------------|-------------------------------|------------------------------------|
| report_id       | str (uuid hex 12)             | unique per report                  |
| agent_type      | AgentType enum                | data/prediction/storage/hvac/monitor|
| title           | str                           |                                    |
| content         | str                           | narrative summary                  |
| data            | dict[str, Any]                | report_detail: situation_summary, schedule_table(96), key_metrics, plan_table, risks |
| created_at      | datetime                      |                                    |
| status          | "pending"/"approved"/"rejected"|                                   |
| severity        | "info"/"warning"/"critical"   |                                    |
| content_hash    | str (sha256 hex)              | binding anchor for approval+dispatch|
| run_id          | str                           | "run-<hex12>"                      |

## ApprovalGate (core/state.py)

| Field        | Type                                    | Notes                         |
|--------------|-----------------------------------------|-------------------------------|
| gate_id      | str                                     | forecast/storage/hvac_approval|
| name         | str                                     |                               |
| description  | str                                     |                               |
| status       | NodeStatus enum                         | idle/running/completed/pending_approval/approved/rejected/failed/warning |
| report       | AgentReport or None                     |                               |
| report_id    | str or None                             | must match at submit time     |
| report_hash  | str or None                             | must match at submit time     |
| decision     | "approve"/"reject"/"revise" or None     |                               |
| comment      | str                                     |                               |
| decided_at   | datetime or None                        |                               |
| decided_by   | str or None                             |                               |

## DispatchCommand (core/state.py)

| Field               | Type           | Notes                            |
|---------------------|----------------|----------------------------------|
| command_id          | str (uuid)     | "cmd-<hex12>"                    |
| run_id              | str            |                                  |
| step                | int (0-95)     | 15-min slot index                |
| sim_time            | datetime       |                                  |
| storage_power_kw    | float          | + discharge / - charge           |
| hvac_power_kw       | float          |                                  |
| hvac_supply_temp_c  | float          | [5, 12]                          |
| storage_report_id   | str            | binding from approval gate       |
| storage_report_hash | str            |                                  |
| hvac_report_id      | str            |                                  |
| hvac_report_hash    | str            |                                  |
| created_at          | datetime       |                                  |
| manual_override     | dict or None   | operator action payload          |

## ExecutionAck (core/state.py)

| Field          | Type     | Notes                        |
|----------------|----------|------------------------------|
| command_id     | str      | must match command           |
| accepted       | bool     |                              |
| status         | str      | "executed"/"rejected"/"failed"|
| message        | str      |                              |
| acknowledged_at| datetime |                              |

## DispatchFeedback (core/state.py)

| Field                       | Type   | Notes                            |
|-----------------------------|--------|----------------------------------|
| command_id                  | str    | must match command               |
| measured_storage_power_kw   | float  | from device adapter              |
| measured_hvac_power_kw      | float  |                                  |
| measured_storage_soc        | float  | [0.10, 0.90]                     |
| measured_storage_temp_c     | float  |                                  |
| measured_hvac_supply_temp_c | float  | [5, 12]                          |
| measured_hvac_return_temp_c | float  |                                  |
| storage_deviation_kw        | float  | measured - setpoint              |
| hvac_deviation_kw           | float  | measured - setpoint              |
| max_deviation_ratio         | float  | >0.03 triggers warning alert     |
| measured_at                 | datetime|                                 |

## DisturbanceEvent (core/state.py)

| Field       | Type           | Notes                                   |
|-------------|----------------|-----------------------------------------|
| event_id    | str (uuid)     |                                         |
| run_id      | str            |                                         |
| actor       | str            |                                         |
| actor_role  | str            | "engineer"/"facility"                   |
| source_text | str            | original natural language               |
| event_type  | str enum       | equipment_failure/recovery/load_adjustment/weather_override/price_override/schedule_change/operational_note |
| target      | str            |                                         |
| start_time  | datetime       |                                         |
| end_time    | datetime/None  |                                         |
| parameters  | dict           | unavailable_units/load_delta_kw/temperature_delta_c/price_multiplier |
| summary     | str            |                                         |
| confidence  | float [0,1]    |                                         |
| parsed_by   | str            | model name or "rule_fallback"           |
| status      | str enum       | proposed/applied/cancelled/failed       |
| created_at  | datetime       |                                         |
| decided_at  | datetime/None  |                                         |
| decided_by  | str/None       |                                         |
| impact_summary | str          |                                         |

## Archive Structure (core/archive.py)

```
<archive_root>/<sim_date>/<run_id>/
  reports/*.json        - append-only report events
  approvals/*.json      - approval decision events
  commands/*.json       - dispatch command events
  acks/*.json           - execution ACK events
  feedback/*.json       - device feedback events
  workflow/*.json       - workflow state snapshots
  control_actions/*.json - operator action events
  disturbances/*.json   - disturbance event lifecycle
  realtime.csv          - append-only per-tick measurements
```

## Known Gaps (to be addressed in later stages)

1. **G3 gap (invariant 5)**: No `(run_id, step)` idempotency key in executor.
   Re-running the same step generates a new command_id.
2. **G4 gap**: ArchiveStore is append-only with no `load()`/`save()` for runtime state.
3. **G1 gap**: Two separate graph definitions (GRAPH_NODES/EDGES vs build_energy_workflow).
4. **G2 gap**: Storage and HVAC run sequentially, not in parallel branches.