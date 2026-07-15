# NFCU ServiceNow Agent — PS Implementation README

**Audience:** PS-side implementation team (Codex / Claude Code)
**Purpose:** Build the PS-side LangGraph ServiceNow agent against a **local mock server**, then switch to the **real NFCU ServiceNow endpoint** by changing configuration only — no code changes.

---

## 0. TL;DR — read this first

- The agent calls **one generic incident list endpoint** and builds query filters **dynamically** from the user's question. Do **not** hardcode one API call per use case.
- The resource path is `/api/nfcui/v1/etsva_support/incidents` on **Dev5 & QA**, but `/api/nfcui/etsva_support/incidents` (**no `/v1/`**) on **Production**. **No hyphen** in `nfcui` anywhere. Keep the incident path **configurable per environment** — do not hardcode `/v1/`.
- **The same filter set works across Dev5, QA, and Production** — there are no per-environment filters. Validated end-to-end on QA and Prod. See §3 for the complete list.
- The API does **plain substring matching**, not SQL `LIKE`. **Do not send `%` wildcards.** Send the plain keyword; spaces URL-encoded as `%20`.
- `cause` is **exact-match**, not substring. Use a known valid value.
- People filters use the **user code** (e.g. `D7834`), **not** the sys_id. Passing a sys_id returns 0 records.
- The people-name filter is **`resolved_by_name`**, not `solved_by_name`. `solved_by_name` does not exist and returns **HTTP 400**.
- `short_description_contains`, `cause_contains`, `probable_cause_contains`, and `resolution_notes_contains` are **not** incident filters — do not use them.
- Final user-facing answers must use `display_value`. **Never show raw `sys_id`** for people fields.
- There are **four hosts**: local mock, Dev5, QA, and Production. See §1 and §2.

---

## 1. Environments & endpoints

There are now **four** hosts in play (mock + three NFCU environments). The host changes between environments, and so does the `/v1/` segment: **Dev5 and QA use `/v1/`; Production does not.** This applies to the incident endpoint (and, separately, the knowledge endpoint). Treat the incident path as **configurable per environment**, not a constant.

**The filter set is the same across Dev, QA, and Production.** Every filter that exists on Dev5 is available and behaves identically on QA and Production — there is no per-environment filter difference. The only thing that changes between NFCU environments is the host. (Mock simply replicates that same filter set locally.)

| Environment | Host | Auth | Use |
|---|---|---|---|
| **Local mock** | `http://localhost:8000` | none or dummy bearer | PS-side dev & test without NFCU access |
| **Dev5** | `https://nfcudev5.service-now.com` | OAuth bearer token | NFCU development instance |
| **QA** | `https://nfcuqa.service-now.com` | OAuth bearer token | QA validation instance |
| **Production** | `https://nfcu.service-now.com` | OAuth bearer token | NFCU production instance |

### Incident endpoint (path differs by environment)

Lower environments include `/v1/`; Production omits it:

```http
GET /api/nfcui/v1/etsva_support/incidents      # Dev5 & QA (has /v1/)
GET /api/nfcui/etsva_support/incidents         # Production (NO /v1/)
```

Full examples:

```http
GET http://localhost:8000/api/nfcui/v1/etsva_support/incidents       # mock (matches lower-env shape)
GET https://nfcudev5.service-now.com/api/nfcui/v1/etsva_support/incidents
GET https://nfcuqa.service-now.com/api/nfcui/v1/etsva_support/incidents
GET https://nfcu.service-now.com/api/nfcui/etsva_support/incidents    # Production — no /v1/
```

**Wrong path — never use (hyphen is incorrect):**

```http
GET /api/nfcu-i/v1/etsva_support/incidents
```

### Knowledge Article endpoint (OUT OF SCOPE for now — kept for reference)

> **Not needed for the current build.** A second endpoint exists for ServiceNow Knowledge Articles. It is documented here and in **Appendix A** for future reference only — do **not** implement it in this phase. Skip to §2.

A second endpoint exists for ServiceNow Knowledge Articles. **Important:** on QA it has **no `/v1/`** segment.

```http
GET https://nfcuqa.service-now.com/api/nfcui/etsva_support/knowledge
GET https://nfcudev5.service-now.com/api/nfcui/v1/etsva_support/knowledge
```

> Path difference is real and confirmed in QA. **For the future build only:** keep the knowledge path **configurable** separately from the incident path so Dev5 (`/v1/`) and QA (no `/v1/`) can both be targeted. See Appendix A.

---

## 2. Dev vs QA vs Production — what's different

The incident **filter set is identical across all three NFCU environments.** A filter validated on Dev5 works the same on QA and Production. What differs is the **path**: Dev5 and QA carry `/v1/`, Production does not.

| Concern | Dev5 (`nfcudev5`) | QA (`nfcuqa`) | Production (`nfcu`) |
|---|---|---|---|
| Incident path | `/api/nfcui/v1/etsva_support/incidents` | same (`/v1/`) | `/api/nfcui/etsva_support/incidents` (**no `/v1/`**) |
| Incident filter set | full set (see §3) | identical | identical |
| Knowledge path *(out of scope — see Appendix A)* | `/api/nfcui/v1/etsva_support/knowledge` | `/api/nfcui/etsva_support/knowledge` (**no `/v1/`**) | confirm against instance |
| Data completeness | Dev data may be sparse; fields can be null even when they exist in prod | used for filter validation; data parity checks done here | full production data |
| Purpose | Development | QA sign-off of filters/fields before prod | Live |

Every incident filter test returned **HTTP 200 / PASS on both QA and Production**. The filter list and rules in §3 are the source of truth and apply to Dev5, QA, and Production alike.

---

## 3. Filters — current, corrected, and new

### 3.1 Critical matching rules (confirmed on QA + Production)

These three rules caused the original test failures. Apply them everywhere:

1. **No `%` wildcards.** The API does plain substring match automatically. `description_contains=%25TSYS%25` URL-decodes to `%TSYS%` (literal percent signs) and is wrong. **Use** `description_contains=TSYS`.
2. **Spaces are `%20`, not `%`.** e.g. `close_notes_contains=cluster%20issue`.
3. **`cause` is exact-match, not substring.** Only known valid values work — `cause` is the "Probable cause" field, a closed list on the NFCU instance: `Action Request`, `Code Error`, `Data Availability`, `Data Quality`, `Deployment Issue`, `Documentation Issues`, `Education/Training`, `False Positive`, `Holiday`, `Maintenance`, `Network Cluster Issue`, `Network or Connectivity Issue`, `Requirements Issues`, `Software Upgrade`, `Subnet Issue`, `Timing/Scheduling Issue`. A made-up or off-list value (e.g. `network outage`) returns 0 records.

### 3.2 Supported filters (complete list — identical on Dev5, QA, and Production)

This is the full, final filter set as provided by the ServiceNow team and validated end-to-end. Every filter below passed on **both QA and Production**. There are no environment-specific filters — what works on Dev works on QA and Prod.

| Filter | Type | Behavior / notes |
|---|---|---|
| `number` | string | One incident, or comma-separated list (`INC298507,INC2996708`). |
| `assignment_group` | string | Assignment group name (URL-encoded) or sys_id, comma-separated. |
| `priority` | string/int | e.g. `1`,`2`,`3`,`4`. |
| `state` | string/int | e.g. `1`,`2` (In Progress),`3`,`6`,`7` (Closed),`8`. Confirm full map against instance. |
| `active` | bool | `true`/`false`/`1`/`0`. Use `active=true` for open scenarios only. |
| `description_contains` | string | Plain substring, case-insensitive. **No `%`.** Searches the long `description` field (not `short_description`). |
| `close_notes_contains` | string | Plain substring. Spaces `%20`. Key for cluster classification — cluster evidence often lives only in close notes. |
| `cause` | string | **Exact match**, not substring. The "Probable cause" closed list: `Action Request`, `Code Error`, `Data Availability`, `Data Quality`, `Deployment Issue`, `Documentation Issues`, `Education/Training`, `False Positive`, `Holiday`, `Maintenance`, `Network Cluster Issue`, `Network or Connectivity Issue`, `Requirements Issues`, `Software Upgrade`, `Subnet Issue`, `Timing/Scheduling Issue`. Off-list values return 0. |
| `assigned_to` | string | **User code** e.g. `D7834`. **NOT sys_id** (sys_id returns 0). |
| `assigned_to_name` | string | **Exact full name incl. code in parens**, e.g. `Dhanalakshmi Sundharam (D7834)`. Partial names fail. Note: the `assigned_to_name` *field* comes back **empty in the response body**, but the filter still works. |
| `resolved_by` | string | User code, same rules as `assigned_to`. |
| `resolved_by_name` | string | Exact full name incl. code, same rules as `assigned_to_name`. |
| `created_after` | datetime | `YYYY-MM-DD%2000:00:00`. |
| `created_before` | datetime | `YYYY-MM-DD%2000:00:00`. |
| `updated_after` | datetime | `YYYY-MM-DD%2000:00:00`. Lower bound on updated date. |
| `updated_before` | datetime | `YYYY-MM-DD%2000:00:00`. |
| `limit` | int | Page size. Default `25` for agent calls. |
| `offset` | int | Pagination start. |

> **People-filter rule of thumb:** code-based filters (`assigned_to`, `resolved_by`) take the **user code**; name-based filters (`assigned_to_name`, `resolved_by_name`) take the **exact full name with code in parens**. Never pass a sys_id to any people filter.

### 3.3 NOT supported — do not use on `/incidents`

These were tested and confirmed **not** to exist on the incident endpoint. Sending them errors or is meaningless:

- `solved_by_name` — **not a valid filter.** Returns **HTTP 400 `invalid_filter`** (confirmed on QA and Production as an intentional negative test). The correct people filter is **`resolved_by_name`**.
- `short_description_contains` — **not a filter on `/incidents`.** There is no short-description filter for incidents; `description_contains` searches the long `description` field only. (A `short_description_contains` filter exists on the *knowledge* endpoint only — see Appendix A, out of scope.)
- `cause_contains`, `probable_cause_contains`, `resolution_notes_contains` — **not real filters.** They are not part of the ServiceNow team's supported set and were removed. Use `cause` (exact match) for cause; `close_notes_contains` for cluster/close-note evidence.

---

## 4. Response envelope & field contract

Every list response is wrapped in a `result` envelope. A single incident is a **one-item `incidents` array**, not a bare object.

```json
{
  "result": {
    "result_count": 3,
    "limit": 25,
    "offset": 0,
    "next_offset": 3,
    "has_more": false,
    "incidents": [ /* incident objects */ ]
  }
}
```

### Reference-field rule

Reference fields (state, priority, assignment_group, people, CI, etc.) return an object with both `value` (raw) and `display_value` (human-readable). **The agent must use `display_value` in user output.** If `display_value` is empty, show `Not available` and document the gap. Never surface a raw sys_id such as `fbc68791b64c15080a84c0604bcb2291` — show `Dhanalakshmi Sundharam (D7834)` instead.

---

## 5. Mock server — full incident JSON to replicate

The mock server must return realistic ServiceNow-shaped JSON so the agent can be built and tested locally. Below is the **canonical single-incident contract** (corrected per QA). Replicate this shape for every seed incident.

```json
{
  "result": {
    "result_count": 1,
    "limit": 25,
    "offset": 0,
    "next_offset": 1,
    "has_more": false,
    "incidents": [
      {
        "sys_id": "fbc68791b64c15080a84c0604bcb2291",
        "number": "INC2996708",
        "short_description": "MDUS: TSYS SCORES data incorrectly mapped in ASL",
        "description": "Migrated credit card data from the SCORES table in TSYS to TSYS_SCORES_DAILY_VW in ASL had fields incorrectly mapped. Score_Reason_Code did not align to the source TSYS value.",
        "priority": { "value": "2", "display_value": "2 - High" },
        "state": { "value": "7", "display_value": "Closed" },
        "active": { "value": "false", "display_value": "false" },
        "category": { "value": "Data Quality", "display_value": "Data Quality" },
        "assignment_group": {
          "value": "a1b2c3d4e5f6000089ab0123456789cd",
          "display_value": "MISSION DATA - SUPPORT TEAM L3"
        },
        "assigned_to": {
          "value": "D7834",
          "display_value": "Dhanalakshmi Sundharam (D7834)"
        },
        "assigned_to_name": "",
        "resolved_by": {
          "value": "D7834",
          "display_value": "Dhanalakshmi Sundharam (D7834)"
        },
        "cause": "Data Availability",
        "u_alert_payload": null,
        "opened_at": "2025-11-25 19:30:42",
        "sys_created_on": "2025-11-25 19:30:42",
        "sys_updated_on": "2025-12-18 20:00:07",
        "resolved_at": "2025-12-11 19:39:41",
        "closed_at": "2025-12-18 20:00:07",
        "close_code": {
          "value": "Solved (Permanently)",
          "display_value": "Solved (Permanently)"
        },
        "close_notes": "Source-to-target mapping from TSYS has been updated. Score_Reason_Code in ASL now maps to Score_Reason in TSYS."
      }
    ]
  }
}
```

### 5.1 Seed-data coverage (one incident per use case)

Seed the mock with at least these archetypes so all use cases are testable. **Keywords the filters match on must appear in the long `description` field**, since `description_contains` searches `description`, not `short_description` (put a readable summary in `short_description` too, but the filter targets `description`). Real sample incident numbers from the Sheet1 use-case matrix are listed so seed data mirrors what was tested.

- **TSYS data-quality** (canonical incident above) — for related-incidents (#2) and engineer lookup (#3). Sheet1 samples: `INC2985087`, `INC2996708`, `INC1801165`, `INC2809078`, `INC2902497`.
- **Pipeline failure (#4)** — `description` contains pipeline evidence like `PL-106-00-MD-UADNOTES-DAILY-INGEST_DATA` / `AzureDataFactory` / `pipeline` / `PL_`; `active = true`. Sheet1 samples: `INC3114648`, `INC3120644`, `INC3122497`, `INC3122655`.
- **Missing data (#5)** — `description` contains `missing data` plus the dataset name; `category = "Data Quality"`; `active = true`; **empty `assigned_to`** on at least one (tests the `resolved_by` → `assigned_to` fallback). Sheet1 samples: `INC3079048`, `INC3122130`, `INC3106823`.
- **Cluster issue (#6)** — `cause = "Network Cluster Issue"`; `close_notes` describing a cluster issue (e.g. `CLOUD_PROVIDER_LAUNCH_FAILURE`); `state = Closed` / `active = false` (cluster tickets are usually already closed — do **not** require `active=true`). Sheet1 samples: `INC3114722`, `INC3120169`, `INC3114723`.
- **ATM/POS similar-incident (#7)** — seed at least one **historical closed** incident: `short_description` like `ATM/POS Processing System`, the **error keywords + `ATM`** in `description` (so `description_contains` can find it), `state = Closed` / `active = false`, with populated `close_notes` (the "how it was resolved" output the agent reads back). Optionally also seed an **open** incident with the same error in its `description` but **no `close_notes`**, to represent the user's input ticket. Sheet1 samples: `INC1745986`, `INC1881338`.
- **Null/missing-field record** — at least one incident with empty `display_value`s to exercise the `Not available` path.

### 5.2 Known quirks to bake into the mock

- `assigned_to_name` field is **empty in the body** even when the name filter matches.
- `cause` only matches **exact** valid values.
- `description_contains` searches **`description`**, not `short_description` (in QA, 84% of records also had the keyword in `short_description`, but the filter targets the long `description` field — seed keywords there).
- For cluster (#6), seed the cluster signal into **`close_notes` and `cause`**, not `description` — that mirrors production, where cluster evidence rarely appears in the description.
- A valid filter with no matching data returns `result_count: 0` and `incidents: []` with `HTTP 200` — not an error.

---

## 6. Use cases → dynamic filter mapping

The parser/router extracts filters from natural language and calls the **same** endpoint. These are behavior patterns, not hardcoded branches. Patterns below reflect the Sheet1 use-case matrix and the QA/Production-validated URLs.

| # | Priority | User wording | Filters built |
|---|---|---|---|
| 1 | #1 | Summarize INCxxxx with supporting links (Wiki + SharePoint) | `number=<INC>`, then read the incident `description`, extract keywords, query AI Search for Wiki/SharePoint links, and combine ServiceNow + AI Search into one grounded answer |
| 2 | #2 | Related incidents for data source X | `description_contains=<DATA_SOURCE>&active=true&offset=0`. **Add `active=true`**; `limit` is **optional** (Sheet1 note: "add Active filter also and remove limit=25" — keep `limit` only if you need to cap the page) |
| 3 | #3 | Which engineer worked on X (last 1–3 months) | `description_contains=<DATA_SOURCE>&updated_after=<DATE>`. Prefer **closed/resolved** incidents; return engineer names via `resolved_by.display_value`, falling back to `assigned_to.display_value`; dedupe names; fall back to 3–6 months if nothing found |
| 4 | #4 | Pipeline incidents for dataset X (last 7 days) | **Two-pass.** First fetch the dataset's incidents: `description_contains=<DATA_SOURCE>&active=true&updated_after=<7d>&offset=0`. Then **post-filter in the agent** for pipeline evidence in **`description`** (ADF / `AzureDataFactory` / `pipeline` / `PL_`). If the dataset isn't named in the ticket, use AI Search to identify the data source for that pipeline |
| 5 | #5 | Missing-data open incidents for a dataset | `description_contains=missing%20data&active=true&offset=0`, then narrow by dataset (Sheet1 note: "multiple calls if you have more data"). Classify as Data Quality |
| 6 | #6 | Cluster issues last month (closed) | Closed incidents (`active=false`) + date range. **Priority filters:** `close_notes_contains=cluster%20issue` **and** `cause=Network Cluster Issue` — cluster evidence usually lives in close notes, not description. `description_contains` is a **low-priority optional** add only; don't rely on it |
| 7 | #7 | Resolution notes for a similar incident | The user's incident is **usually open**, so it has **no `close_notes` of its own** — you cannot search by `close_notes_contains` on it. Fetch the given `number=<INC>`, extract data source + error keywords from its **`description`** (the only reliable field on an open ticket), then search **historical closed** incidents of the **same dataset** via **`description_contains`** (the search/lead filter). From those closed results, **read** `close_notes` as the resolution **output** and rank by similarity. (If the input incident happens to be closed and already has close notes, the LLM may use those too — but don't depend on it.) |
| — | Next phase | Pipeline `pip-xxx` hierarchy | Deferred — needs the ADF agent to resolve hierarchy first (not tested) |

---

## 8. Configuration template (env vars)

```env
# --- Switch environment by changing these only ---
SERVICENOW_INSTANCE_URL=http://localhost:8000/          # local mock
# SERVICENOW_INSTANCE_URL=https://nfcudev5.service-now.com/
# SERVICENOW_INSTANCE_URL=https://nfcuqa.service-now.com/
# SERVICENOW_INSTANCE_URL=https://nfcu.service-now.com/   # production

# Incident path differs by env: Dev5/QA (and mock) use /v1/; Production does NOT.
# Set these to match the target environment.
SERVICENOW_INCIDENT_LIST_API_PREFIX=/api/nfcui/v1/etsva_support/incidents   # Dev5 / QA / mock
SERVICENOW_INCIDENT_API_PREFIX=/api/nfcui/v1/etsva_support/incidents        # Dev5 / QA / mock
# Production (no /v1/):
# SERVICENOW_INCIDENT_LIST_API_PREFIX=/api/nfcui/etsva_support/incidents
# SERVICENOW_INCIDENT_API_PREFIX=/api/nfcui/etsva_support/incidents

# Knowledge endpoint is OUT OF SCOPE this phase (Appendix A). When built, the path
# differs by env (QA has no /v1/) — keep separate + configurable:
# SERVICENOW_KNOWLEDGE_API_PREFIX=/api/nfcui/v1/etsva_support/knowledge      # Dev5
# SERVICENOW_KNOWLEDGE_API_PREFIX=/api/nfcui/etsva_support/knowledge       # QA

SERVICENOW_TIMEOUT_SECONDS=20

# --- Real instances: Key Vault preferred ---
AZURE_KEYVAULT_URI=<key vault URI>
SERVICENOW_CLIENT_ID_KEYVAULT_SECRET_NAME=ETSVA-SNOW-ClientID
SERVICENOW_CLIENT_SECRET_KEYVAULT_SECRET_NAME=ETSVA-SNOW-Secret

# --- Local fallback (mock can ignore auth entirely) ---
SERVICENOW_CLIENT_ID=<local dev client id>
SERVICENOW_CLIENT_SECRET=<local dev secret>
```

Client rules: keep only the **origin** from `SERVICENOW_INSTANCE_URL`; build the path from the prefix vars. Because the incident path itself differs by environment (Dev5/QA include `/v1/`, Production does not), the path **must** come from `SERVICENOW_INCIDENT_LIST_API_PREFIX` / `SERVICENOW_INCIDENT_API_PREFIX` — never hardcode `/v1/` in code. Prefer Key Vault when `AZURE_KEYVAULT_URI` is set, `.env` fallback otherwise. Percent-encode spaces as `%20`. **Never log tokens/secrets.**

---

## 10. Final user-facing output rules

- Never return raw JSON to the user unless asked.
- Use a clean list/table: Incident #, Short Description, State, Priority, Active, Category, Assignment Group, Assigned To / Resolved By, Updated, Cause, Close Notes, Why Matched.
- People fields use `display_value` only. Missing → `Not available`.
- Summarize long notes but keep full text available in the tool output for grounding.

---

## 11. Acceptance criteria

1. Agent uses the correct path per environment — `/api/nfcui/v1/etsva_support/incidents` on Dev5/QA and `/api/nfcui/etsva_support/incidents` (no `/v1/`) on Production — driven by config, never hyphenated (`nfcu-i`), and `/v1/` never hardcoded.
2. Filters built dynamically; no seven hardcoded flows.
3. No `%` wildcards sent; spaces `%20`; `cause` passed as exact value; people filters use codes/exact names, never sys_id.
4. Current + new (George/Josh) filters supported.
5. Responses use `display_value`; sys_id never shown for people.
6. Null/missing fields handled (`Not available`), and `result_count: 0` is treated as a valid empty result, not an error.
7. Mock server seeds cover all seven use cases incl. a null-field record.
8. Switching mock → Dev5 → QA → Production requires only env-var/config changes.

---

## 12. Open items to confirm against the real instance

- Full `state` and `priority` integer→label maps.
- OAuth token URL and scopes for Dev5/QA/Production service accounts (lower-env vs prod model).

*(Knowledge Article endpoint details live in Appendix A — out of scope this phase.)*

---

## Appendix A — Knowledge Article endpoint (OUT OF SCOPE this phase — reference only)

> **Do not build this in the current phase.** Captured here so the QA findings aren't lost and so a future phase can pick it up without re-discovery. The incident agent above is the deliverable.

A second ServiceNow endpoint exists for Knowledge Articles. Confirmed in QA on 2026-06-02: 5 endpoints, all HTTP 200, QA article count (6) matches Dev5 — data parity confirmed.

### Path (note the env difference)

```http
GET https://nfcudev5.service-now.com/api/nfcui/v1/etsva_support/knowledge    # Dev5 (has /v1/)
GET https://nfcuqa.service-now.com/api/nfcui/etsva_support/knowledge          # QA (NO /v1/)
```

When this is eventually built, keep the knowledge path configurable **separately** from the incident path because of the `/v1/` difference.

### Filters (QA validated)

| Filter | Notes |
|---|---|
| (none) | Returns all articles (QA base count = 6). |
| `ownership_group` | Exact match, e.g. `Rstudio%20PROD%20SUPPORT`. All 6 QA articles belong to `RSTUDIO PROD SUPPORT`. |
| `short_description_contains` | Substring. **Supported here** (unlike `/incidents`). e.g. `password`. |
| `article_body_contains` | Substring over full article body, e.g. `NFCU%20Package%20Manager`. |
| `keyword` | Searches multiple fields (title + body). For this dataset, `article_body_contains` and `keyword` resolved to the **same** article (`KB0040665`). |

Article record exposes at least: `article_number` (e.g. `KB0035869`), `short_description`, `ownership_group`. Apply the same `display_value` rules as incidents.

### Open item (future)

- Exact knowledge article field list beyond article #, short_description, ownership_group.
