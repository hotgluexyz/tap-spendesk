"""Raw accounting records, preserving Spendesk minor-unit amounts and native IDs."""

import json
from pathlib import Path

import requests

from tap_spendesk.client import SpendeskStream


def schema(name):
    return json.loads((Path(__file__).parent / "schemas" / f"{name}.json").read_text())


class PayablesStream(SpendeskStream):
    """Search broadly, exhaust cursors, then validate authoritative detail."""

    name = "payables"
    schema = schema(name)

    def get_records(self, context):
        payload = {"sort": "desc", "limit": 100}
        cursor = None
        seen_cursors = set()
        while True:
            if cursor:
                payload["cursor"] = cursor
            body = self.client.request("POST", "/v1/payables/search", json=payload)
            if body.get("referencesToRetry"):
                # Never mark an incomplete extract successful. The next run replays it.
                raise RuntimeError("Spendesk search has referencesToRetry; retry extraction later")
            for summary in body["payables"]:
                record = self.client.request("GET", f"/v1/payables/{summary['id']}")
                if record.get("companyId") != self.config["company_id"]:
                    self.logger.warning("Skipping payable outside configured company")
                    continue
                if (
                    self.config.get("start_date")
                    and record["payableDate"] < self.config["start_date"][:10]
                ):
                    continue
                # A newer detail can supersede stale search; older detail cannot be exported.
                ready = (
                    record["bookkeepingStatus"] in {"prepared", "failedExport"}
                    and record["version"] >= summary["version"]
                    and (summary["state"] == "toExport" or record["version"] > summary["version"])
                )
                if not ready and not self.config["include_unprepared"]:
                    continue
                record["export_ready"] = ready
                record["search_state"] = summary["state"]
                record["search_version"] = summary["version"]
                record["attachments"], record["attachments_status"] = self.read_attachments(
                    record["id"]
                )
                yield record
            cursor = body["nextCursor"]
            if not cursor:
                return
            if cursor in seen_cursors:
                raise RuntimeError("Spendesk search repeated a cursor")
            seen_cursors.add(cursor)

    def read_attachments(self, payable_id):
        """Return receipt links without making a missing receipt fatal."""
        if not self.config["include_attachments"]:
            return [], "not_requested"
        try:
            body = self.client.request("GET", f"/v1/payables/{payable_id}/attachments")
            return body["data"], "available" if body["data"] else "missing"
        except requests.HTTPError as error:
            if error.response.status_code not in {403, 404}:
                raise
            self.logger.warning("Payable receipt unavailable (HTTP %s)", error.response.status_code)
            return [], "unavailable"


class SettlementsStream(SpendeskStream):
    name = "settlements"
    path = "/v1/settlements"
    date_param = "clearedFrom"
    schema = schema(name)

    def get_records(self, context):
        params = {"page": 1, "pageSize": 30, "state": "created"}
        if self.config.get("start_date"):
            params["clearedFrom"] = self.config["start_date"]
        yield from self.read_pages(self.path, params)


class SuppliersStream(SpendeskStream):
    name = "suppliers"
    path = "/v1/suppliers"
    schema = schema(name)


class UsersStream(SpendeskStream):
    name = "users"
    path = "/v1/users"
    schema = schema(name)

    def get_records(self, context):
        for status in ("active", "archived", "pending"):
            yield from self.read_pages(self.path, {"page": 1, "pageSize": 30, "status": status})


class BankFeesStream(SpendeskStream):
    name = "bank_fees"
    path = "/v1/bank-fees"
    date_param = "from"
    schema = schema(name)


class WalletLoadsStream(SpendeskStream):
    name = "wallet_loads"
    path = "/v1/wallet-loads"
    date_param = "from"
    schema = schema(name)


class AccountsStream(SpendeskStream):
    name = "accounts"
    path = "/v1/experimental/accounts"
    schema = schema(name)

    def get_records(self, context):
        offset = 0
        while True:
            body = self.client.request(
                "GET",
                self.path,
                params={
                    "offset": offset,
                    "limit": 100,
                    "includeArchived": "true",
                },
            )
            yield from body["data"]
            offset += len(body["data"])
            if offset >= body["totalResults"]:
                return
            if not body["data"]:
                raise RuntimeError("Spendesk accounts pagination made no progress")


class CostCentersStream(SpendeskStream):
    name = "cost_centers"
    path = "/v1/cost-centers"
    schema = schema(name)

    def get_records(self, context):
        yield from self.read_pages(self.path, {"includeArchived": "true"})


class AnalyticalFieldsStream(SpendeskStream):
    name = "analytical_fields"
    path = "/v1/analytical-fields"
    schema = schema(name)

    def get_records(self, context):
        for field in self.read_pages(
            self.path,
            {
                "page": 1,
                "pageSize": 30,
                "includeArchived": "true",
            },
        ):
            field["values"] = list(
                self.read_pages(
                    f"/v1/analytical-fields/{field['id']}/values",
                    {"page": 1, "pageSize": 30, "includeArchived": "true"},
                )
            )
            yield field
