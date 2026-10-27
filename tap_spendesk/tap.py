"""Spendesk accounting extraction for Hotglue."""

from functools import cached_property

from hotglue_singer_sdk import Tap
from hotglue_singer_sdk import typing as th

from tap_spendesk.client import SpendeskClient
from tap_spendesk.streams import (
    AccountsStream,
    AnalyticalFieldsStream,
    BankFeesStream,
    CostCentersStream,
    PayablesStream,
    SettlementsStream,
    SuppliersStream,
    UsersStream,
    WalletLoadsStream,
)


class TapSpendesk(Tap):
    name = "tap-spendesk"
    config_jsonschema = th.PropertiesList(
        th.Property("api_url", th.StringType, default="https://public-api.spendesk.com"),
        th.Property("client_id", th.StringType, required=True),
        th.Property(
            "client_secret", th.StringType, required=True, description="Sensitive credential"
        ),
        th.Property("company_id", th.StringType, required=True),
        th.Property(
            "start_date",
            th.DateTimeType,
            description="Inclusive business-date lower bound; omit for all history",
        ),
        th.Property("include_attachments", th.BooleanType, default=True),
        th.Property(
            "include_unprepared",
            th.BooleanType,
            default=False,
            description="Inspection only: include non-exportable payables",
        ),
    ).to_dict()

    @cached_property
    def client(self):
        return SpendeskClient(self.config)

    def discover_streams(self):
        return [
            stream(tap=self)
            for stream in (
                SuppliersStream,
                AccountsStream,
                CostCentersStream,
                AnalyticalFieldsStream,
                UsersStream,
                PayablesStream,
                SettlementsStream,
                BankFeesStream,
                WalletLoadsStream,
            )
        ]


if __name__ == "__main__":
    TapSpendesk.cli()
