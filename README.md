# tap-spendesk

Hotglue Singer SDK tap for the Spendesk → Rillet accounting integration.
Extracts native Spendesk records; it does not create Rillet objects, convert
minor-unit amounts, download receipt binaries, or write export status back.

## Streams

| Stream | Data | Scope |
| --- | --- | --- |
| `payables` | Search → detail, nested receipt URLs | Invoices, cards; credit notes, refunds, claims, mileage, per diem |
| `settlements` | Created settlements, allocations, payment source and currencies | Invoice / reimbursement payments and wallet feed |
| `suppliers` | All suppliers including archived, VAT number, bank details, creditor account | Vendor matching / creation |
| `accounts` | Expense, tax, bank, supplier, employee and reverse-charge accounts, including archived | Coding / wallet mappings |
| `cost_centers` | Including archived | Coding mappings |
| `analytical_fields` | Including archived, with nested values | Coding mappings |
| `users` | Active, archived, pending | Employee counterparties (V2) |
| `bank_fees` | Fees and settlement links | Fees / wallet feed (V2) |
| `wallet_loads` | Loads with page or GBP cursor pagination | Top-ups / wallet feed (V2) |

Every stream uses native `id` as its primary key. Select only streams needed for
the job using the Singer catalog; V2 streams are available, not a V2 export implementation.
The company is configured explicitly: no organisation-wide companies endpoint or
wallet-summary permission is required.

## Configuration

Requires Python 3.10. Install into a dedicated venv:

```bash
uv venv --python 3.10 --seed .venv
uv pip install --python .venv/bin/python -e .
```

| Setting | Required | Default | Meaning |
| --- | --- | --- | --- |
| `client_id` | yes | — | Public API credentials ID (sensitive) |
| `client_secret` | yes | — | Public API credentials secret (sensitive) |
| `company_id` | yes | — | Spendesk company, sent as `X-Company-Id` |
| `api_url` | no | `https://public-api.spendesk.com` | Host only, without `/v1`; demo: `https://public-api.demo.spendesk.com` |
| `start_date` | no | all history | ISO datetime; inclusive payable business date, settlement cleared date, fee/load date. Does not filter referentials. |
| `include_attachments` | no | `true` | Fetch short-lived receipt links alongside each eligible payable |
| `include_unprepared` | no | `false` | Inspection only: include non-ready payables, flagged `export_ready=false` |

Store credentials, catalogs, output and state in git-ignored `.secrets/`:

```json
{
  "client_id": "YOUR_CLIENT_ID",
  "client_secret": "YOUR_CLIENT_SECRET",
  "company_id": "YOUR_COMPANY_ID",
  "api_url": "https://public-api.demo.spendesk.com",
  "start_date": "2023-01-01T00:00:00Z"
}
```

These are Public API credentials using Basic authentication to `/v1/auth/token`,
not partner OAuth/PKCE credentials. The tap shares one in-memory token across
streams, renews before expiry, and retries one 401 after reauthentication. Partner
OAuth linking/Hotglue refresh is not implemented in this first version.

Required scopes depend on selected streams: `payable:read`,
`experimental:payable-search:read`, `payable-attachment:read`, `settlement:read`,
`supplier:read`, `experimental:chart-of-accounts:read`, `cost-center:read`,
`analytical-field:read`, `user:read`, `bank-fee:read`, `wallet-load:read`.

## Discovery and sync

```bash
.venv/bin/tap-spendesk --config .secrets/config.json --discover > .secrets/catalog.json
# Select desired streams/fields in the catalog before syncing.
.venv/bin/tap-spendesk --config .secrets/config.json --catalog .secrets/catalog.json > .secrets/data.singer
```

Discovery is offline and uses bundled schemas generated from the public OpenAPI
specification. Full nested schemas retain line amounts, expense/VAT accounts,
counterparties, analytical values, allocation IDs, versions, currency conversion,
amortisation and Spendesk deep links. Dates and amounts are left in source format.

### Safety and replay

- Search uses `sort=desc`, no status/date filter, and follows every cursor. Every
  detail must match `company_id`. `start_date` is applied to detail `payableDate`;
  older results do not terminate pagination because updates/preparation can be late.
- Default payable output requires detail `bookkeepingStatus=prepared` or
  `failedExport`, and detail version ≥ search version. Search must agree
  (`state=toExport`) unless detail has a strictly newer version. This permits fresh
  prepared detail to supersede cached search. Stale/unprepared/already-exported
  records are deferred. Inspecting with
  `include_unprepared=true` must never be used to bypass `export_ready` in an ETL.
- Search `referencesToRetry` and repeated/missing pagination cursors fail the run
  rather than silently accepting incomplete extraction.
- All streams intentionally use `FULL_TABLE`, with no advancing bookmark. Each
  run replays the configured date range so cached results and parked failures are
  retried. Downstream confirmed-response ID mappings must provide idempotency;
  extraction state is not confirmation of a successful Rillet write.
- Reads/search/token acquisition retry 429 and transient 5xx responses up to five
  retries, honoring `Retry-After` or exponential backoff with jitter.
- Receipt 403/404 or an empty list does not block the payable. Inspect
  `attachments_status` (`available`, `missing`, `unavailable`, `not_requested`).
  Other attachment errors fail the run after retries. Download URLs during export:
  they expire in about one hour and are sensitive. Do not log Singer records.
- Current Spendesk docs describe search/settlement caching up to 24 hours and
  detail up to 20 minutes. A 10–15 minute polling schedule alone cannot guarantee
  15-minute source visibility. Webhook-driven fresh reads / provider cache
  guarantees and downstream retry/parking belong to the integration phase.

## Development

```bash
uv pip install --python .venv/bin/python pytest requests-mock ruff
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
# Refresh schemas from the vendor spec, then inspect/test the diff:
.venv/bin/python tools/update_schemas.py
```

Docs: https://developer.spendesk.com/ — generated schemas intentionally merge
union variants and allow nullable partial accounting data for Singer/Parquet.

MIT licensed.
