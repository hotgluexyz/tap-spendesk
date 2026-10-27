"""Company-scoped Spendesk reads with shared, renewable authentication."""

import time
from typing import ClassVar

import requests
from hotglue_singer_sdk.streams import Stream
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class SpendeskClient:
    """One session per tap; retry only reads, search, and token acquisition."""

    def __init__(self, config):
        self.config = config
        self.base_url = config["api_url"].rstrip("/")
        self.session = requests.Session()
        retry = Retry(
            total=5,
            backoff_factor=1,
            backoff_jitter=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods={"GET", "POST"},
            respect_retry_after_header=True,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.token = None
        self.expires_at = 0

    def authenticate(self):
        response = self.session.post(
            self.base_url + "/v1/auth/token",
            auth=(self.config["client_id"], self.config["client_secret"]),
            json={"grant_type": "client_credentials"},
            timeout=60,
        )
        response.raise_for_status()
        token = response.json()
        self.token = token["access_token"]
        self.expires_at = time.monotonic() + token["expires_in"] - 60

    def request(self, method, path, **kwargs):
        if time.monotonic() >= self.expires_at:
            self.authenticate()
        for attempt in range(2):
            response = self.session.request(
                method,
                self.base_url + path,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-Company-Id": self.config["company_id"],
                    "Accept": "application/json",
                },
                timeout=60,
                **kwargs,
            )
            if response.status_code == 401 and attempt == 0:
                self.authenticate()
                continue
            response.raise_for_status()
            return response.json()


class SpendeskStream(Stream):
    """Full-table replay is intentional: cached reads cannot support lossless bookmarks."""

    primary_keys: ClassVar[list[str]] = ["id"]
    replication_key = None
    path = None
    date_param = None

    @property
    def client(self):
        return self._tap.client

    def get_records(self, context):
        params = {"page": 1, "pageSize": 30}
        if self.date_param and self.config.get("start_date"):
            params[self.date_param] = self.config["start_date"]
        yield from self.read_pages(self.path, params)

    def read_pages(self, path, params=None):
        params = dict(params or {"page": 1, "pageSize": 30})
        seen_cursors = set()
        while True:
            body = self.client.request("GET", path, params=params)
            yield from body["data"]
            pagination = body.get("meta", {}).get("pagination")
            if not pagination:
                return
            # GBP wallet loads use cursors: never advance page in this mode.
            if "hasNextPage" in pagination:
                if not pagination["hasNextPage"]:
                    return
                cursor = pagination.get("nextCursor")
                if not cursor or cursor in seen_cursors:
                    raise RuntimeError("Spendesk returned a missing or repeated cursor")
                seen_cursors.add(cursor)
                params.pop("page", None)
                params["cursor"] = cursor
            elif pagination["page"] * pagination["pageSize"] < pagination["total"]:
                params["page"] = pagination["page"] + 1
            else:
                return
