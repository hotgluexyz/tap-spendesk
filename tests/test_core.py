"""Exercise Singer output through the public tap and mocked HTTP boundary."""

import json

import pytest
from jsonschema import validate

from tap_spendesk.tap import TapSpendesk

CONFIG = {"client_id": "test", "client_secret": "test", "company_id": "company-a"}
BASE = "https://public-api.spendesk.com"


def run_tap(requests_mock, capsys, stream, **config):
    requests_mock.post(BASE + "/v1/auth/token", json={"access_token": "test", "expires_in": 3600})
    tap = TapSpendesk(config={**CONFIG, **config})
    catalog = tap.catalog_dict
    for entry in catalog["streams"]:
        entry["metadata"].append(
            {
                "breadcrumb": [],
                "metadata": {"selected": entry["stream"] == stream},
            }
        )
    TapSpendesk(config={**CONFIG, **config}, catalog=catalog).sync_all()
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    schemas = {m["stream"]: m["schema"] for m in messages if m["type"] == "SCHEMA"}
    records = [m["record"] for m in messages if m["type"] == "RECORD"]
    for record in records:
        validate(record, schemas[stream])
    return records


def detail(id, **changes):
    return {
        "id": id,
        "companyId": "company-a",
        "type": "invoicePurchase",
        "bookkeepingStatus": "prepared",
        "version": 4,
        "payableDate": "2026-09-02",
        "amount": 12347,
        "functionalAmount": 14816,
        "currency": "USD",
        "functionalCurrency": "EUR",
        "allocations": [{"type": "settlement", "allocatedAmount": 12347, "settlementId": "paid-a"}],
        "counterparty": {"id": "supplier-a", "accountPayable": None},
        "lineItems": [
            {
                "expenseAccount": {"code": "6100"},
                "vatAccount": None,
                "financial": {"netAmount": 10347, "vatAmount": 2000, "grossAmount": 12347},
            }
        ],
        **changes,
    }


def test_payables_exhaust_cursors_and_defer_unsafe_details(requests_mock, capsys):
    summaries = [
        {"id": id, "state": "toExport", "version": 4}
        for id in ("ready", "wrong-company", "stale", "unprepared", "older", "boundary")
    ]
    requests_mock.post(
        BASE + "/v1/payables/search",
        [
            {"json": {"payables": summaries[:3], "nextCursor": "next", "referencesToRetry": []}},
            {"json": {"payables": summaries[3:], "nextCursor": None, "referencesToRetry": []}},
        ],
    )
    changes = {
        "ready": {},
        "wrong-company": {"companyId": "company-b"},
        "stale": {"version": 3},
        "unprepared": {"bookkeepingStatus": "created"},
        "older": {"payableDate": "2026-08-31"},
        "boundary": {"payableDate": "2026-09-01"},
    }
    for id, override in changes.items():
        requests_mock.get(BASE + f"/v1/payables/{id}", json=detail(id, **override))
    requests_mock.get(
        BASE + "/v1/payables/ready/attachments",
        json={
            "data": [
                {"url": "https://example.com/receipt.pdf", "mimeType": "application/pdf"},
            ]
        },
    )
    requests_mock.get(BASE + "/v1/payables/boundary/attachments", status_code=404)
    records = run_tap(requests_mock, capsys, "payables", start_date="2026-09-01T00:00:00Z")
    assert [r["id"] for r in records] == ["ready", "boundary"]
    assert records[0]["export_ready"] is True
    assert records[0]["amount"] == 12347
    assert records[0]["functionalAmount"] == 14816
    assert records[0]["lineItems"][0]["financial"]["netAmount"] == 10347
    assert records[0]["allocations"][0]["settlementId"] == "paid-a"
    assert records[0]["attachments"][0]["mimeType"] == "application/pdf"
    assert records[1]["attachments"] == []
    assert records[1]["attachments_status"] == "unavailable"


def test_accounts_follow_offsets(requests_mock, capsys):
    requests_mock.get(
        BASE + "/v1/experimental/accounts",
        [
            {"json": {"data": [{"id": "expense", "code": "6100"}], "totalResults": 2}},
            {"json": {"data": [{"id": "wallet", "purpose": "spendesk"}], "totalResults": 2}},
        ],
    )
    records = run_tap(requests_mock, capsys, "accounts")
    assert [r["id"] for r in records] == ["expense", "wallet"]
    assert records[1]["purpose"] == "spendesk"


def test_analytical_fields_include_values_from_every_page(requests_mock, capsys):
    requests_mock.get(BASE + "/v1/analytical-fields", json={"data": [{"id": "department"}]})
    requests_mock.get(
        BASE + "/v1/analytical-fields/department/values",
        [
            {
                "json": {
                    "data": [{"id": "sales", "value": "Sales", "isArchived": False}],
                    "meta": {"pagination": {"page": 1, "pageSize": 1, "total": 2}},
                }
            },
            {
                "json": {
                    "data": [{"id": "legacy", "value": "Legacy", "isArchived": True}],
                    "meta": {"pagination": {"page": 2, "pageSize": 1, "total": 2}},
                }
            },
        ],
    )
    records = run_tap(requests_mock, capsys, "analytical_fields")
    assert records[0]["values"] == [
        {"id": "sales", "value": "Sales", "isArchived": False},
        {"id": "legacy", "value": "Legacy", "isArchived": True},
    ]


def test_inspection_records_cannot_be_mistaken_for_ready(requests_mock, capsys):
    requests_mock.post(
        BASE + "/v1/payables/search",
        json={
            "payables": [{"id": "draft", "state": "toPrepare", "version": 4}],
            "nextCursor": None,
            "referencesToRetry": [],
        },
    )
    requests_mock.get(
        BASE + "/v1/payables/draft", json=detail("draft", bookkeepingStatus="created")
    )
    records = run_tap(
        requests_mock, capsys, "payables", include_unprepared=True, include_attachments=False
    )
    assert len(records) == 1
    assert records[0]["export_ready"] is False
    assert records[0]["search_state"] == "toPrepare"


def test_newer_prepared_detail_supersedes_stale_search(requests_mock, capsys):
    requests_mock.post(
        BASE + "/v1/payables/search",
        json={
            "payables": [
                {"id": "newer", "state": "toPrepare", "version": 3},
                {"id": "contradictory", "state": "toPrepare", "version": 4},
            ],
            "nextCursor": None,
            "referencesToRetry": [],
        },
    )
    for id in ("newer", "contradictory"):
        requests_mock.get(BASE + f"/v1/payables/{id}", json=detail(id))
    records = run_tap(requests_mock, capsys, "payables", include_attachments=False)
    assert [r["id"] for r in records] == ["newer"]
    assert records[0]["export_ready"] is True


def test_partial_search_fails_instead_of_silent_loss(requests_mock, capsys):
    requests_mock.post(
        BASE + "/v1/payables/search",
        json={
            "payables": [],
            "nextCursor": None,
            "referencesToRetry": ["missing"],
        },
    )
    with pytest.raises(RuntimeError, match="referencesToRetry"):
        run_tap(requests_mock, capsys, "payables")


def test_page_pagination_preserves_all_suppliers(requests_mock, capsys):
    requests_mock.get(
        BASE + "/v1/suppliers",
        [
            {
                "json": {
                    "data": [{"id": "a", "vatNo": "GB123"}],
                    "meta": {"pagination": {"page": 1, "pageSize": 1, "total": 2}},
                }
            },
            {
                "json": {
                    "data": [{"id": "b", "iban": "sandbox-iban"}],
                    "meta": {"pagination": {"page": 2, "pageSize": 1, "total": 2}},
                }
            },
        ],
    )
    assert [r["id"] for r in run_tap(requests_mock, capsys, "suppliers")] == ["a", "b"]


def test_gbp_cursor_pagination_does_not_stop_at_page_total(requests_mock, capsys):
    requests_mock.get(
        BASE + "/v1/wallet-loads",
        [
            {
                "json": {
                    "data": [{"id": "a", "amount": 123}],
                    "meta": {
                        "pagination": {
                            "page": 1,
                            "pageSize": 30,
                            "total": 1,
                            "hasNextPage": True,
                            "nextCursor": "after-a",
                        }
                    },
                }
            },
            {
                "json": {
                    "data": [{"id": "b", "amount": 456}],
                    "meta": {"pagination": {"hasNextPage": False, "nextCursor": None}},
                }
            },
        ],
    )
    assert [r["amount"] for r in run_tap(requests_mock, capsys, "wallet_loads")] == [123, 456]


def test_expired_token_is_renewed_once(requests_mock, capsys):
    requests_mock.get(
        BASE + "/v1/suppliers",
        [
            {"status_code": 401},
            {"json": {"data": [{"id": "renewed"}]}},
        ],
    )
    assert [r["id"] for r in run_tap(requests_mock, capsys, "suppliers")] == ["renewed"]
