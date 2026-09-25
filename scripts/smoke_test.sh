#!/usr/bin/env bash
# Credit Sniper — hosted deployment smoke test.
#
# Runs the deployment verification checks against a LIVE deployment. Run it
# from a machine that can reach the Railway API (e.g. your laptop). It needs
# only curl and python3.
#
# Required:
#   API_BASE      Railway public API origin, no trailing slash, no /api
#                 e.g. https://credit-sniper-api.up.railway.app
#
# Optional (enable the authenticated checks):
#   TOKEN_A       A Clerk session JWT for test user A. Get it in the browser
#                 while signed in to the app:
#                     open DevTools console →  await window.Clerk.session.getToken()
#                 copy the string it prints (no quotes).
#   TOKEN_B       A Clerk session JWT for a SECOND signed-in account (isolation
#                 check). Use a private window / different account.
#   WEB_ORIGIN    Your Vercel origin (for the CORS check),
#                 e.g. https://credit-sniper.vercel.app
#   EXPECTED_REV  Expected Alembic revision (default: 0003)
#
# Usage:
#   API_BASE=https://... TOKEN_A=... TOKEN_B=... WEB_ORIGIN=https://... ./scripts/smoke_test.sh
#
# Clerk session tokens are short-lived (~60s). If the authenticated steps fail
# with 401, grab a fresh token and re-run. This test uploads only a SYNTHETIC
# PDF — never a real credit report.

set -u
EXPECTED_REV="${EXPECTED_REV:-0003}"
PASS=0; FAIL=0; SKIP=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; SKIP=$((SKIP+1)); }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$1"; }

if [ -z "${API_BASE:-}" ]; then
  echo "Set API_BASE to your Railway API origin (no trailing slash). See header." >&2
  exit 2
fi
API_BASE="${API_BASE%/}"

# jget <json> <python-expression-on-variable d>  → prints value or empty
jget() { python3 -c 'import sys,json
try: d=json.loads(sys.stdin.read())
except Exception: sys.exit(0)
try: print(eval(sys.argv[1]))
except Exception: pass' "$1"; }

# req <method> <path> [auth] [extra curl args...]
# Sets globals CODE (HTTP status) and BODY (response body). Call it directly
# (not in $(...)) so the globals survive — a subshell would drop them.
CODE=""; BODY=""
req() {
  local method="$1" path="$2" auth="${3:-}"; shift 3 2>/dev/null || shift $#
  local args=(-sS -X "$method" -o /tmp/cs_body -w '%{http_code}' --max-time 45)
  [ -n "$auth" ] && args+=(-H "Authorization: Bearer $auth")
  CODE=$(curl "${args[@]}" "$@" "$API_BASE$path" 2>/tmp/cs_err || true)
  [ -n "$CODE" ] || CODE=000
  BODY="$(cat /tmp/cs_body 2>/dev/null || true)"
}

hdr "Target: $API_BASE   (expecting schema $EXPECTED_REV)"

# ── 1–4. Liveness, readiness, migration, auth_mode ───────────────────────
hdr "1-4. Health / readiness / migration / auth mode"
req GET /api/health ""; code=$CODE
[ "$code" = "200" ] && ok "/api/health 200" || bad "/api/health returned $code"

req GET /api/health/ready ""; code=$CODE
if [ "$code" = "200" ]; then
  db=$(printf '%s' "$BODY" | jget 'd.get("database")')
  rev=$(printf '%s' "$BODY" | jget 'd.get("schema_revision")')
  mode=$(printf '%s' "$BODY" | jget 'd.get("auth_mode")')
  store=$(printf '%s' "$BODY" | jget 'd.get("storage_backend")')
  aic=$(printf '%s' "$BODY" | jget 'd.get("ai_configured")')
  echo "     ready: database=$db schema=$rev auth_mode=$mode storage=$store ai_configured=$aic"
  [ "$db" = "ok" ] && ok "database = ok" || bad "database = $db"
  [ "$rev" = "$EXPECTED_REV" ] && ok "schema_revision = $EXPECTED_REV" || bad "schema_revision = $rev (expected $EXPECTED_REV)"
  [ "$mode" = "jwt" ] && ok "auth_mode = jwt" || bad "auth_mode = $mode (MUST be jwt)"
  [ "$store" = "r2" ] && ok "storage_backend = r2" || skip "storage_backend = $store (expected r2)"
else
  bad "/api/health/ready returned $code — build/migration/DB problem; check Railway logs. Body: $BODY"
fi

# ── 5. Unauthenticated request is rejected ───────────────────────────────
hdr "5. Unauthenticated /api/users/me → 401"
req GET /api/users/me ""; code=$CODE
[ "$code" = "401" ] && ok "unauthenticated -> 401" || bad "unauthenticated -> $code (expected 401)"

# ── 8. CORS preflight from the web origin ────────────────────────────────
hdr "8. CORS (browser origin allowed)"
if [ -n "${WEB_ORIGIN:-}" ]; then
  acao=$(curl -sS -D- -o /dev/null --max-time 30 -X OPTIONS \
    -H "Origin: $WEB_ORIGIN" -H "Access-Control-Request-Method: GET" \
    -H "Access-Control-Request-Headers: authorization" \
    "$API_BASE/api/users/me" 2>/dev/null | tr -d '\r' | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}')
  if [ "$acao" = "$WEB_ORIGIN" ] || [ "$acao" = "*" ]; then ok "CORS allows $WEB_ORIGIN (ACAO=$acao)"
  else bad "CORS did not allow $WEB_ORIGIN (ACAO='$acao'); check ALLOWED_ORIGINS / ALLOWED_ORIGIN_REGEX"; fi
else
  skip "set WEB_ORIGIN to check CORS"
fi

# ── 6/7. Authenticated identity + provisioning ───────────────────────────
hdr "6-7. Authenticated identity provisions in Postgres"
UID_A=""
if [ -n "${TOKEN_A:-}" ]; then
  req GET /api/users/me "$TOKEN_A"; code=$CODE
  if [ "$code" = "200" ]; then
    UID_A=$(printf '%s' "$BODY" | jget 'd.get("id")')
    ok "authenticated /me 200 (user id $UID_A)"
    req GET /api/users/me "$TOKEN_A"; code2=$CODE
    UID_A2=$(printf '%s' "$BODY" | jget 'd.get("id")')
    [ "$UID_A" = "$UID_A2" ] && ok "same token resolves to the same user (stable id)" || bad "id changed between calls ($UID_A vs $UID_A2)"
  else bad "authenticated /me -> $code (token expired? grab a fresh one). Body: $BODY"; fi
else
  skip "set TOKEN_A to run authenticated checks (steps 6,7,9,10,11,13)"
fi

# ── 10/11/9/12. Upload → store in R2 → retrieve → object not public ──────
REPORT_ID=""
if [ -n "$UID_A" ]; then
  hdr "10. Authenticated synthetic PDF upload"
  python3 - <<'PY'
# Minimal, valid, text-bearing PDF (pure stdlib) — a synthetic report.
lines = ["EQUIFAX Credit Report", "Report Date: January 1, 2024",
         "Creditor: SYNTHETIC BANK", "Account Number: XXXX-0000",
         "SMOKE TEST — NOT A REAL REPORT"]
objs = []
objs.append(b"<</Type/Catalog/Pages 2 0 R>>")
objs.append(b"<</Type/Pages/Kids[3 0 R]/Count 1>>")
objs.append(b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>")
objs.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")
text = b"BT /F1 12 Tf 50 740 Td 14 TL "
for ln in lines:
    esc = ln.replace("(", "\\(").replace(")", "\\)").encode()
    text += b"(" + esc + b") Tj T* "
text += b"ET"
objs.append(b"<</Length %d>>\nstream\n%s\nendstream" % (len(text), text))
out = b"%PDF-1.4\n"
offsets = []
for i, body in enumerate(objs, 1):
    offsets.append(len(out))
    out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
xref = len(out)
out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
for off in offsets:
    out += b"%010d 00000 n \n" % off
out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
open("/tmp/cs_synthetic.pdf", "wb").write(out)
PY
  # Upload is asynchronous: 202 + a report id, then the background worker
  # reads the document. We poll the status endpoint rather than expect the
  # upload request itself to have done the work.
  req POST /api/reports/upload "$TOKEN_A" -F "file=@/tmp/cs_synthetic.pdf;type=application/pdf" -F "bureau=auto_detect"; code=$CODE
  if [ "$code" = "202" ]; then
    REPORT_ID=$(printf '%s' "$BODY" | jget 'd.get("report_id")')
    stage=$(printf '%s' "$BODY" | jget 'd.get("processing_stage")')
    ok "upload 202 accepted (report_id $REPORT_ID, stage=$stage)"
  else bad "upload -> $code (expected 202). Body: $BODY"; fi

  hdr "10b. Background extraction finishes"
  if [ -n "$REPORT_ID" ]; then
    processing=true
    for _ in $(seq 1 60); do
      req GET "/api/reports/$REPORT_ID/status" "$TOKEN_A" >/dev/null
      processing=$(printf '%s' "$BODY" | jget 'str(d.get("processing")).lower()')
      stage=$(printf '%s' "$BODY" | jget 'd.get("processing_stage")')
      [ "$processing" = "false" ] && break
      sleep 2
    done
    if [ "$processing" = "false" ]; then
      req GET "/api/reports/$REPORT_ID" "$TOKEN_A" >/dev/null
      acct=$(printf '%s' "$BODY" | jget 'len(d.get("accounts") or [])')
      ok "processing finished (stage=$stage, accounts=$acct)"
      # The worker must never leave provider internals in a consumer response.
      if printf '%s' "$BODY" | grep -Eqi 'insufficient_quota|credit_balance_exhausted|AIProviderError|openai'; then
        bad "report response leaked provider internals"
      else ok "report response carries no provider internals"; fi
    else
      bad "still processing after 120s (stage=$stage) — is EXTRACTION_WORKER_ENABLED set?"
    fi
  else
    skip "no report id — upload failed"
  fi

  hdr "11 & 9 & 12. Retrieve own file / R2 connectivity / object not public"
  if [ -n "$REPORT_ID" ]; then
    # The endpoint verifies ownership then 307-redirects to a presigned R2 URL.
    loc=$(curl -sS -D- -o /dev/null --max-time 45 -H "Authorization: Bearer $TOKEN_A" \
          "$API_BASE/api/reports/$REPORT_ID/file" 2>/dev/null | tr -d '\r' | awk -F': ' 'tolower($1)=="location"{print $2}')
    if [ -n "$loc" ]; then
      ok "file endpoint issued a presigned R2 URL (R2 wired up)"
      # Fetch via the presigned URL (no auth header) → proves R2 connectivity + retrieval.
      pcode=$(curl -sS -o /tmp/cs_pdf -w '%{http_code}' --max-time 60 "$loc" 2>/dev/null || echo 000)
      if [ "$pcode" = "200" ] && head -c4 /tmp/cs_pdf | grep -q '%PDF'; then ok "retrieved the PDF via presigned URL (starts with %PDF)"
      else bad "presigned fetch -> $pcode / not a PDF"; fi
      # Same object WITHOUT the signature query string → must NOT return the
      # object. The security property is "the bytes are not retrievable without
      # authorization", not any particular error code — S3/R2 answers an
      # unsigned request with 400 (InvalidRequest), 403 (AccessDenied), or 404
      # depending on config, and all are fine. We follow redirects (-L) so a
      # redirect can't smuggle out a public copy, then FAIL only if the object
      # actually came back (a PDF body, the exact object bytes, or any 2xx).
      bare="${loc%%\?*}"
      bcode=$(curl -sS -L -o /tmp/cs_unsigned -w '%{http_code}' --max-time 45 "$bare" 2>/dev/null || echo 000)
      if head -c4 /tmp/cs_unsigned 2>/dev/null | grep -q '%PDF'; then
        bad "unsigned URL returned a PDF (HTTP $bcode) — the object is PUBLIC"
      elif cmp -s /tmp/cs_unsigned /tmp/cs_pdf; then
        bad "unsigned URL returned the exact object bytes (HTTP $bcode) — the object is PUBLIC"
      elif [ "${bcode:0:1}" = "2" ]; then
        bad "unsigned URL returned success HTTP $bcode without a signature — treat as PUBLIC/leaky; inspect the response"
      else
        ok "unsigned object URL is NOT public (rejected HTTP $bcode, no object returned)"
      fi
    else
      # Local storage (no R2) streams bytes instead of redirecting.
      req GET "/api/reports/$REPORT_ID/file" "$TOKEN_A"; code=$CODE
      if [ "$code" = "200" ] && head -c4 /tmp/cs_body | grep -q '%PDF'; then
        ok "retrieved the PDF (streamed)"; skip "no R2 redirect — backend is on local storage, not r2"
      else bad "file retrieval -> $code"; fi
    fi
  else
    skip "no report id — upload failed"
  fi
fi

# ── 13. Cross-user isolation ─────────────────────────────────────────────
hdr "13. User isolation (second account cannot reach A's data)"
if [ -n "$REPORT_ID" ] && [ -n "${TOKEN_B:-}" ]; then
  # Make sure B is provisioned, then confirm B cannot see A's report/file.
  req GET /api/users/me "$TOKEN_B" >/dev/null
  req GET "/api/reports/$REPORT_ID" "$TOKEN_B"; code=$CODE
  [ "$code" = "404" ] && ok "B GET A's report -> 404 (isolated)" || bad "B GET A's report -> $code (expected 404!)"
  req GET "/api/reports/$REPORT_ID/file" "$TOKEN_B"; code=$CODE
  [ "$code" = "404" ] && ok "B GET A's file -> 404 (isolated)" || bad "B GET A's file -> $code (expected 404!)"
else
  skip "set TOKEN_B (a second account's token) to check isolation"
fi

# ── Summary ──────────────────────────────────────────────────────────────
hdr "Summary: $PASS passed, $FAIL failed, $SKIP skipped"
[ "$FAIL" -eq 0 ] || exit 1
