#!/usr/bin/env bash
# =============================================================================
# Smoke-test the deployed fin-deepagents-dev: auth enforcement + Postgres.
#
# Suites:
#   health    - public endpoints (/ok, /info, /metrics) respond
#   auth      - protected endpoints reject missing/garbage tokens (401) and
#               accept a valid token (200)
#   postgres  - (a) /metrics shows a healthy PG connection pool, and
#               (b) end-to-end CRUD on /threads proves read/write persistence
#
# A valid JWT is needed for the authenticated checks. By default we mint one
# with the Azure CLI for the API audience; override by exporting ACCESS_TOKEN.
#
# Usage:
#   ./infra/test-deployment.sh                 # all suites against the dev URL
#   ./infra/test-deployment.sh auth            # only the auth suite
#   BASE_URL=https://... ./infra/test-deployment.sh
#   ACCESS_TOKEN=ey... ./infra/test-deployment.sh
# =============================================================================
set -uo pipefail

BASE_URL="${BASE_URL:-https://fin-deepagents-dev.example-0000000.eastus2.azurecontainerapps.io}"
# Default the API audience client-id from the committed source of truth
# (infra/.env.deploy → ENTRA_CLIENT_ID) so the value lives in exactly one place;
# fall back to the known dev client-id if the file is unreadable.
_ENV_DEPLOY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.env.deploy"
_CID="$(sed -nE 's/^(export +)?ENTRA_CLIENT_ID=//p' "${_ENV_DEPLOY}" 2>/dev/null | tail -1)"
API_CLIENT_ID="${API_CLIENT_ID:-${_CID:-11111111-1111-1111-1111-111111111111}}"
SUITE="${1:-all}"

PASS=0
FAIL=0
SKIP=0
green=$'\e[32m'; red=$'\e[31m'; yellow=$'\e[33m'; dim=$'\e[2m'; rst=$'\e[0m'

ok()   { PASS=$((PASS+1)); echo "  ${green}PASS${rst} $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  ${red}FAIL${rst} $1"; [[ -n "${2:-}" ]] && echo "       ${dim}$2${rst}"; }
skip() { SKIP=$((SKIP+1)); echo "  ${yellow}SKIP${rst} $1"; }
hdr()  { echo; echo "── $1 ── ${dim}${BASE_URL}${rst}"; }

# req METHOD PATH [TOKEN] [BODY]  -> sets globals CODE and BODY (call directly,
# NOT inside $( ), so the globals survive in the parent shell).
CODE=""; BODY=""
req() {
  local method=$1 path=$2 token=${3:-} body=${4:-} tmp; tmp="$(mktemp)"
  local args=(-sS -o "$tmp" -w "%{http_code}" -X "$method" "${BASE_URL}${path}")
  [[ -n $token ]] && args+=(-H "Authorization: Bearer ${token}")
  [[ -n $body  ]] && args+=(-H 'content-type: application/json' -d "$body")
  CODE="$(curl "${args[@]}" 2>/dev/null)"
  BODY="$(cat "$tmp")"; rm -f "$tmp"
}
# status METHOD PATH [TOKEN] [BODY] -> echoes the HTTP status (body in $RESP_BODY)
RESP_BODY=""
status() { req "$@"; RESP_BODY="$BODY"; echo "$CODE"; }

expect() { # EXPECTED ACTUAL LABEL
  if [[ "$2" == "$1" ]]; then ok "$3 (-> $2)"; else bad "$3 (expected $1, got $2)" "${RESP_BODY:0:160}"; fi
}

jget() { python3 -c 'import sys,json;d=json.load(sys.stdin); print(d.get("'"$1"'","") if isinstance(d,dict) else "")' 2>/dev/null; }

# ---------------------------------------------------------------------------
# Acquire a token (unless one was provided)
# ---------------------------------------------------------------------------
TOKEN="${ACCESS_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  TOKEN="$(az account get-access-token --scope "api://${API_CLIENT_ID}/.default" --query accessToken -o tsv 2>/dev/null || true)"
fi
if [[ -n "$TOKEN" ]]; then
  WHO="$(printf '%s' "$TOKEN" | cut -d. -f2 | python3 -c 'import sys,base64,json;t=sys.stdin.read().strip();t+="="*(-len(t)%4);c=json.loads(base64.urlsafe_b64decode(t));print(c.get("name") or c.get("upn") or c.get("appid",""))' 2>/dev/null || true)"
  echo "${dim}Using token for: ${WHO:-<unknown>}${rst}"
else
  echo "${yellow}No token (az login + access to api://${API_CLIENT_ID} required, or set ACCESS_TOKEN). Authenticated checks will be skipped.${rst}"
fi

run_health() {
  hdr "HEALTH (public, no auth)"
  expect 200 "$(status GET /ok)"    "GET /ok"
  expect 200 "$(status GET /info)"  "GET /info"
  expect 200 "$(status GET /metrics)" "GET /metrics"
}

run_auth() {
  hdr "AUTH ENFORCEMENT (protected endpoints must reject)"
  # No token -> 401
  expect 401 "$(status POST /threads '' '{}')"            "POST /threads            (no token)"
  expect 401 "$(status POST /assistants/search '' '{}')"  "POST /assistants/search  (no token)"
  expect 401 "$(status GET  /threads/00000000-0000-0000-0000-000000000000)" "GET /threads/{id}        (no token)"
  expect 401 "$(status GET  /starter_prompts)"            "GET /starter_prompts     (no token, custom route)"
  expect 401 "$(status GET  /feedback)"                   "GET /feedback            (no token, custom route)"
  # Garbage token -> 401
  expect 401 "$(status POST /threads 'not.a.jwt' '{}')"   "POST /threads            (garbage token)"

  hdr "AUTH POSITIVE (valid token accepted)"
  if [[ -z "$TOKEN" ]]; then skip "valid-token checks (no token available)"; return; fi
  expect 200 "$(status POST /assistants/search "$TOKEN" '{"limit":10}')" "POST /assistants/search  (valid token)"
}

run_postgres() {
  hdr "POSTGRES HEALTH (via /metrics pool gauges)"
  local m; m="$(curl -sS "${BASE_URL}/metrics" 2>/dev/null)"
  local size errs maxp
  size="$(printf '%s\n' "$m" | awk '/^lg_api_pg_pool_size\{/{print $NF; exit}')"
  errs="$(printf '%s\n' "$m" | awk '/^lg_api_pg_pool_requests_errors\{/{print $NF; exit}')"
  maxp="$(printf '%s\n' "$m" | awk '/^lg_api_pg_pool_max\{/{print $NF; exit}')"
  echo "  ${dim}pg_pool_max=${maxp:-?} pg_pool_size=${size:-?} pg_pool_requests_errors=${errs:-?}${rst}"
  if [[ -n "$size" ]] && awk "BEGIN{exit !(${size}>=1)}"; then ok "postgres pool has >=1 connection"; else bad "postgres pool size missing/zero (size=${size:-none})"; fi
  if [[ -n "$errs" ]] && awk "BEGIN{exit !(${errs}==0)}"; then ok "postgres pool connection errors == 0"; else bad "postgres pool has connection errors (errors=${errs:-none})"; fi

  hdr "POSTGRES PERSISTENCE (end-to-end CRUD on /threads)"
  if [[ -z "$TOKEN" ]]; then skip "persistence CRUD (no token available)"; return; fi

  # CREATE
  local tid
  req POST /threads "$TOKEN" '{"metadata":{"test":"deepagents-smoke"}}'
  tid="$(printf '%s' "$BODY" | jget thread_id)"
  if [[ "$CODE" == "200" && -n "$tid" ]]; then ok "create thread (-> $tid)"; else bad "create thread (code $CODE)" "${BODY:0:160}"; return; fi

  # READ BACK (separate request -> proves it persisted, not in-request memory)
  req GET "/threads/${tid}" "$TOKEN"
  local rid; rid="$(printf '%s' "$BODY" | jget thread_id)"
  if [[ "$CODE" == "200" && "$rid" == "$tid" ]]; then ok "read back persisted thread"; else bad "read back thread (code $CODE, id '$rid')" "${BODY:0:160}"; fi

  # SEARCH (lists from Postgres) -> our thread is present
  local found
  found="$(curl -sS -X POST "${BASE_URL}/threads/search" -H "Authorization: Bearer ${TOKEN}" -H 'content-type: application/json' -d '{"limit":100}' 2>/dev/null \
            | python3 -c 'import sys,json;d=json.load(sys.stdin);print("yes" if any(t.get("thread_id")=="'"$tid"'" for t in d) else "no")' 2>/dev/null)"
  if [[ "$found" == "yes" ]]; then ok "thread appears in /threads/search"; else bad "thread not found in search"; fi

  # DELETE
  req DELETE "/threads/${tid}" "$TOKEN"
  if [[ "$CODE" == "200" || "$CODE" == "204" ]]; then ok "delete thread (-> $CODE)"; else bad "delete thread (code $CODE)" "${BODY:0:160}"; fi

  # CONFIRM GONE -> 404 (proves the delete was persisted)
  req GET "/threads/${tid}" "$TOKEN"
  if [[ "$CODE" == "404" ]]; then ok "deleted thread is gone (-> 404)"; else bad "deleted thread still present (code $CODE)" "${BODY:0:160}"; fi
}

case "$SUITE" in
  health)   run_health ;;
  auth)     run_auth ;;
  postgres) run_postgres ;;
  all)      run_health; run_auth; run_postgres ;;
  *) echo "unknown suite '$SUITE' (use: health|auth|postgres|all)"; exit 2 ;;
esac

echo
echo "──────────────────────────────────────────"
echo "  ${green}${PASS} passed${rst}, ${red}${FAIL} failed${rst}, ${yellow}${SKIP} skipped${rst}"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
