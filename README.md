# Wage Type Catalog Service

Authenticated API that converts a **Payslip Master Data** file (`.xlsx` or `.csv`) into a **Wage Type Catalog** workbook
(`.xlsx`). Uploaded and generated files are held only until the catalog is downloaded once, then deleted.

```
client ──POST /v1/auth/token──────────────► JWT (people)      or  X-API-Key (systems)
       ──POST /v1/catalog/jobs  [file]─────► 201 job: summary, warnings, download_url
       ──GET  …/jobs/{id}/download─────────► catalog.xlsx  ──► input + output deleted
```

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
copy .env.example .env      # set WTC_JWT_SECRET:  python -c "import secrets; print(secrets.token_urlsafe(48))"
.venv\Scripts\python -m scripts.manage_users create admin --role admin      # prompts for a password
.venv\Scripts\python -m uvicorn app.main:create_app --factory --port 8000
```

Interactive docs: <http://localhost:8000/docs>. Click **Authorize** and log in.

```bash
TOKEN=$(curl -s -X POST localhost:8000/v1/auth/token -d "username=admin&password=…" | jq -r .access_token)
JOB=$(curl -s -H "Authorization: Bearer $TOKEN" -F "file=@Payslip_Demo_Master_Data.xlsx" localhost:8000/v1/catalog/jobs)
curl -OJ -H "Authorization: Bearer $TOKEN" "$(echo "$JOB" | jq -r .download_url)"
```

## Endpoints

| Method & path | Auth | Purpose |
|---|---|---|
| `POST /v1/auth/token` | – | Log in (OAuth2 password form) → bearer token |
| `GET /v1/auth/me` | any | Who am I, and how did I authenticate |
| `POST /v1/auth/logout` | login | Revoke **all** your tokens |
| `POST /v1/auth/me/password` | login | Change password (revokes your tokens) |
| `POST / GET /v1/auth/api-keys`, `DELETE …/{key_id}` | login | Create (shown once), list, revoke API keys |
| `GET / POST /v1/auth/users`, `POST …/{u}/disable\|enable\|password` | admin login | User administration |
| `POST /v1/catalog/jobs` | any | Upload → process → job with summary and warnings (`Location` header) |
| `GET /v1/catalog/jobs`, `GET …/{id}` | any | Your jobs still waiting to be downloaded |
| `GET /v1/catalog/jobs/{id}/download` | any | **One-time** download; files deleted afterwards |
| `DELETE /v1/catalog/jobs/{id}` | any | Discard a job without downloading |
| `GET /health/live`, `/health/ready` | – | Liveness; readiness (DB, job storage, mapping) |

"any" = bearer token **or** `X-API-Key`. Credential and user management accept a **login token only**, so a leaked API key
cannot create more keys, change passwords or create users.

### Errors

Every error is `application/problem+json` (RFC 9457) with a stable machine-readable `code` and the `request_id`:

```json
{"type": "about:blank", "title": "Unprocessable Content", "status": 422, "code": "invalid_input",
 "detail": "Input validation failed (1 problem(s))", "request_id": "b2761bc5…",
 "errors": ["Pay Line Items CSV row 166: payslip_id 'X' not found in Master Data CSV"]}
```

Send `X-Request-ID` to correlate with your own logs. It is echoed back and stamped on every server log line.

## Input

| Mode | `file` | `master_data` |
|---|---|---|
| Workbook | `.xlsx` with sheets **Master Data** and **Pay Line Items** | – |
| CSV pair | Pay Line Items `.csv` | Master Data `.csv` |
| CSV single | Pay Line Items `.csv` that also has `molga_country_grouping`, `employment_group` | – |

Reference input: `tests/fixtures/Payslip_Demo_Master_Data_updated.xlsx`.

Required columns. **Master Data:** `payslip_id, employee_id, molga_country_grouping, employment_group`. **Pay Line Items:**
`payslip_id, employee_id, section, wage_type, amount, line_category`, plus **`wage_code`** in the current template.
Optional and used if present: `payroll_currency`, `applies_to`. Files in the original template (no `wage_code` column) are
still accepted; their codes come from the mapping. Other sheets, such as an existing *Wage Type Catalog* sheet, are ignored.
CSV delimiters `, ; tab |` are auto-detected. Encoding can be UTF-8 or Windows-1252.

### Validation rules (strict by design: payroll figures are never guessed)

| Rejected (422, row-level message) | Why |
|---|---|
| Line whose `payslip_id` isn't in Master Data, or whose `employee_id` contradicts it | No fuzzy joins |
| Duplicate `payslip_id` in Master Data; duplicate column headers | Ambiguous source |
| Amount like `1234,56`, `1.234,56` or a bare `1,234` | Locale-ambiguous. `1234.56`, `1,234.56`, `-5`, SAP `5.00-` and `(5.00)` are accepted |
| One molga containing several payroll currencies | Amounts in different currencies can't be summed |
| Unknown `section` (not EARNING / DEDUCTION / ER_CONTRIB) | No category to assign |
| One `wage_code` used for two different wage types in a molga | One code must mean one wage type |
| `wage_code` that isn't 1–10 letters, digits or `/ _ . -` | Malformed code |

| Accepted with a warning | |
|---|---|
| `applies_to` other than `CURRENT` (e.g. RETRO) | Included in totals; confirm that is intended |
| Blank `wage_code` cell | Code taken from the mapping by name; failing that, a provisional code |
| One wage type under several codes | Each code is its own catalog row |
| Same wage type under several sections/categories | The most frequent one is used |

## Output

One row per **wage code** per molga, in the reference `Wage_Type_Catalog_Demo.xlsx` layout (verified cell for cell by a test):

| Column | Rule |
|---|---|
| `wage_type_local_lang` | `wage_type` |
| `wage_code` | The line's `wage_code`; see *Wage codes* below |
| `no_of_occurrence` | Number of line items |
| `total_pay` | Sum of `amount` (exact decimal arithmetic) |
| `average_pay` | Unrounded total ÷ occurrences, rounded **half-up** to 2 dp (123.225 → 123.23) |
| `category` | EARNING → Earning, DEDUCTION → Deduction, ER_CONTRIB → Employer Contribution |
| `sub_category` | `line_category` |
| `position` | Rank by wage code within the molga (matches the reference; see note below) |
| `employment_groups` | Distinct groups of the employees concerned, `; `-separated |
| `molga` | `molga_country_grouping` |

When there are warnings, a **Processing Notes** sheet lists them and provisional codes are highlighted in yellow.

### Wage codes

Each line's code is resolved in this order, and the job summary reports the counts in `code_sources`:

1. **`wage_code` column** (current template). This is authoritative and needs no mapping.
2. **`data/wage_codes.csv`** (`molga,wage_type,wage_code`), looked up by wage-type name. Used for files in the original
   template and for blank cells. Rows with molga `*` apply to all country groupings, and a molga-specific row overrides them.
   Matching ignores case and extra spaces, and the file reloads automatically when it changes.
3. **Provisional code** in the sub-category's range (EARNING 1000–1499, IMPUTED_INCOME 1500–1999, US_TAX 4000–4999,
   PRE_TAX 5000–5999, AFTER_TAX 6000–6999, EMPLOYER_PAID_BENEFIT 7000–7999, anything else 9000–9999). It never collides
   with a code used in the file or reserved by the mapping, and it is highlighted and listed in the warnings.

A mapping file that is missing, malformed or self-contradictory makes the service return `503 configuration_error` and fail
readiness rather than silently mis-code.

**Note on `position`.** The template notes describe it as the *display sequence on the payslip*. The payslip line order in the
sample data doesn't follow that sequence (for example, Medicare EE 4050 is printed before Social Security EE 4030 on every
payslip). The reference catalog's positions are exactly the wage-code order, so the service ranks by code.

## Security model

- **Passwords:** Argon2id. **Tokens:** HS256 JWT with `exp/iat/nbf/iss/aud` and a per-user `token_version`, so logout, a password
  change or disabling the user revokes every outstanding token.
- **API keys:** `wtc_<id>_<secret>`. Only a SHA-256 of the secret is stored, expiry is capped (`WTC_API_KEY_MAX_DAYS`), keys can be
  revoked, `last_used_at` is tracked, and a key stops working as soon as its owner is disabled.
- **Login throttling:** keyed on username **and** client IP, so an attacker cannot lock a real user out, plus a per-IP cap
  against password spraying. `Retry-After` is returned. Unknown usernames take the same time and get the same error as bad passwords.
- **Uploads:** authentication, the per-user job quota and capacity are checked **before the body is read**. The body is capped
  even for chunked uploads with no `Content-Length`. Extension, zip structure and uncompressed size are checked (zip-bomb guard),
  XML is parsed via `defusedxml`, and every text cell is written as text (no formula injection).
- **Jobs:** private to the uploader (others get 404), with 144-bit random IDs. `Cache-Control: no-store`, `nosniff` and
  `X-Frame-Options: DENY` are set on every response.
- **Audit log:** a `wtc.audit` logger records logins (success, failure, throttled), key and user changes, and jobs
  (created, rejected, downloaded, interrupted, discarded, expired). It logs identifiers and counts only, never payslip contents.
- **Deployment:** run behind **HTTPS** only. When behind a proxy, set `FORWARDED_ALLOW_IPS` to the proxy's address so throttling
  sees real client IPs.

## Temporary storage lifecycle

| Event | Effect |
|---|---|
| Upload accepted | `WTC_JOB_DIR/<id>/` holds the input(s), `output.xlsx` and `meta.json` |
| Download **completes** | The whole directory is deleted. Download is one-time, and a concurrent second request gets 404 |
| Download **interrupted** (client hung up before the last byte) | Job is kept and can be retried until it expires |
| `Range` / `HEAD` request | Range is ignored (full file sent); HEAD does not consume the job |
| Never downloaded | The sweeper deletes it after `WTC_JOB_TTL_MINUTES` (default 60) |
| Crash mid-upload or mid-download | Orphans are swept after 10 minutes |

"Completed" means the last byte was handed to the server's socket. HTTP gives no client-side delivery receipt.

## Limits (all configurable, see `.env.example`)

10 MB per file · 5 pending jobs per user (429) · 4 concurrent processing jobs; excess requests wait up to 30 s, then get 503
with `Retry-After` · 500,000 rows per sheet · first 25 row errors reported.

## Running in production

### AWS EC2 (manon-prod) — `deploy/`

Runs as its own Compose project (`wage-catalog`) next to manon, without touching it. The API has no host port. Caddy serves
HTTPS on **443** with an automatic Let's Encrypt certificate, validated over TLS-ALPN because port 80 belongs to manon's nginx.
Memory is capped at 512 MB for the API and 128 MB for Caddy.

```bash
# first time, on the server
sudo mkdir -p /opt/wage-catalog && sudo chown ec2-user: /opt/wage-catalog
git clone https://github.com/tarunepiuse/wage-catalog-service.git /opt/wage-catalog
SITE_ADDRESS=13-62-135-9.sslip.io /opt/wage-catalog/deploy/deploy.sh    # creates deploy/.env with a fresh secret
cd /opt/wage-catalog/deploy && docker compose exec api python -m scripts.manage_users create admin --role admin

# every update
/opt/wage-catalog/deploy/deploy.sh
```

`deploy/.env` exists only on the server and holds the JWT secret. Users live in the `wage-catalog_db` volume, so they survive
redeploys. Logs: `docker compose -f /opt/wage-catalog/deploy/compose.yaml logs -f api`. `13-62-135-9.sslip.io` is a test hostname
that resolves to the server IP. For a real domain, point an A record at the server and change `SITE_ADDRESS` in `deploy/.env`.

### Any Docker host

```bash
docker build -t wage-catalog-service:2.0.0 .
docker run -d -p 8000:8000 -e WTC_JWT_SECRET=… -v wtc-db:/srv/var/db wage-catalog-service:2.0.0
docker exec -it <container> python -m scripts.manage_users create admin --role admin
```

The image runs as a non-root user, has a health check, and logs in JSON. Run **one worker per container** and scale with
replicas. Login throttling and job files are per instance, so behind several replicas use sticky sessions (or a shared
`WTC_JOB_DIR` volume) and enforce rate limits at the gateway. SQLite (WAL mode) suits a single instance. With several
instances, move users to a shared database.

User management CLI: `python -m scripts.manage_users {create,set-password,disable,enable,list}`. Pass `--password-stdin` for
scripted use.

## Development

```powershell
.venv\Scripts\python -m pytest            # golden files (current + legacy template), auth, limits, validation, download mechanics
.venv\Scripts\python -m ruff check .
```

| Path | Responsibility |
|---|---|
| `app/main.py` | App factory, lifespan, middleware order, health |
| `app/core/` | Problem+json errors, pure-ASGI middleware (request id, body cap, security headers), logging/audit |
| `app/auth/` | Credential primitives, user and API-key store (SQLite with migrations), dependencies, routes |
| `app/catalog/` | `reader` (parse + structure) → `builder` (rules) → `writer` (xlsx); `service` orchestrates; `router` HTTP |
| `app/jobs/` | Temp job store (claim / release / sweep) and the one-time download response |
