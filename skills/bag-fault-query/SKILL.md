---
name: bag-fault-query
description: >
  Given a 17-digit VIN and one or more timestamps/dates, finds corresponding
  bags via ESS and reads /mff_md/enable_signal_cmd faults using pybagmining
  (no full bag download required).
allowed-tools:
  - Bash
---

# Bag Fault Query Skill

Query `/mff_md/enable_signal_cmd` faults from bags by VIN + time range.

## What You Need From the User

| Input | Format | Example |
|-------|--------|---------|
| VIN | 17 characters | `LHUAS1234567890123` |
| Time point(s) | One or more (see formats below) | `2026-03-04` |

**Accepted time formats** (same as vin-ess-query):
- `YYYY-MM-DD` — queries the entire day (CST)
- `YYYY-MM-DDTHH:MM:SS` — queries ±5 min around the timestamp (CST)
- `YYYYMMDDHHMMSS` — same, compact form

## Steps

### 1. Run the script

```bash
python3 ~/.claude/skills/bag-fault-query/bag_fault_query.py <VIN> <time_point> [time_point2 ...]
```

Options:
- `--size N` — max ESS events to fetch (default 20)
- `--debug` — print raw API responses for troubleshooting

Examples:
```bash
# Single day
python3 bag_fault_query.py LHUAS1234567890123 2026-03-04

# Specific timestamp (±5 min)
python3 bag_fault_query.py LHUAS1234567890123 2026-03-04T10:30:00

# Date range, more results
python3 bag_fault_query.py LHUAS1234567890123 2026-03-04 2026-03-05 --size 50

# Debug mode if bag MD5 can't be resolved
python3 bag_fault_query.py LHUAS1234567890123 2026-03-04 --debug
```

### 2. Present results

Show the user:
- Anonymous VIN and time range used
- For each bag: whether faults were detected
- If faults: `error_codes` and which `degradation_functions` were disabled

## Credentials Setup (one-time, on the run machine)

### Keycloak (for ESS)
Same as vin-ess-query — credentials in `~/.pymdi/credentials.json`:
```json
{"keycloak_username": "...", "keycloak_password": "..."}
```

### CDI token (for pybagmining)
Get token from: https://cla.momenta.works/cdi/token
Save to: `~/.pymdi/pymdi_token.txt`

```bash
echo "your-token-here" > ~/.pymdi/pymdi_token.txt
```

Or set env var: `export PYMDI_TOKEN=your-token-here`

## Dependencies

```bash
pip install requests pybagmining \
  --extra-index-url https://artifactory.momenta.works/artifactory/api/pypi/pypi-pl/simple
```

## How It Works

```
VIN (17 chars)
  │
  ▼ SHA1 uppercase
Anonymous VIN (40 chars)
  │
  ▼ ESS search API (Keycloak auth)
  │  EQL: collect_time in range AND vin IN ('SHA1_VIN')
  │
ESS events (filter_name, collect_time, event_id)
  │
  ▼ ESS mviz-meta endpoint → bag MD5
  │
  ▼ pybagmining (CDI token, DownloadByTopic mode)
  │  get_ros_bag_info(md5) → read_messages('/mff_md/enable_signal_cmd')
  │
  ▼ Parse frame_id JSON
     error_code: []        → no fault
     error_code: [64037]   → fault active
     degradation_functions → which capabilities disabled
```

## Fault Format

Each message's `frame_id` is a JSON string:

**Normal state:**
```json
{"error_code": [], "active_error_codes": [20545, ...], ...}
```

**Fault state:**
```json
{
  "error_code": [64037],
  "degradation_functions": {
    "can_enter_hnp":  {"value": false, "error_codes": [[64037]]},
    "can_retain_hnp": {"value": false, "error_codes": [[64037]]},
    ...
  }
}
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No events found | Widen time range; verify VIN is 17 chars |
| Bag MD5 not resolved | Run `--debug` to see raw mviz-meta response; check if event has bag linked |
| `pybagmining` not installed | `pip install pybagmining --extra-index-url ...` |
| CDI token error | Refresh token at https://cla.momenta.works/cdi/token |
| Topic not found in bag | That bag may not contain MFF data; try other events |
