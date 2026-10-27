"""Regenerate bundled Singer schemas from Spendesk's public OpenAPI definition.

Run with the tap's Python. Schemas are nullable to accommodate partial accounting
data. Union variants are merged so Singer/Parquet retain every variant's fields.
Review generated changes against a real response before publishing.
"""

import json
from pathlib import Path

import requests

URL = "https://developer.spendesk.com/openapi/spendesk-public-api.json"


def normalize(node, spec):
    if "$ref" in node:
        ref = spec
        for key in node["$ref"].split("/")[1:]:
            ref = ref[key]
        node = {**ref, **{k: v for k, v in node.items() if k != "$ref"}}
    variants = [
        normalize(v, spec) for key in ("allOf", "anyOf", "oneOf") for v in node.get(key, [])
    ]
    types = node.get("type", [])
    types = [types] if isinstance(types, str) else list(types)
    if not types and "enum" in node:
        primitive_types = {str: "string", int: "integer", float: "number", bool: "boolean"}
        types = [primitive_types[type(value)] for value in node["enum"] if value is not None]
    props = {k: normalize(v, spec) for k, v in node.get("properties", {}).items()}
    for variant in variants:
        types.extend(variant.get("type", []))
        for key, value in variant.get("properties", {}).items():
            props[key] = normalize({"anyOf": [props[key], value]}, spec) if key in props else value
    if props:
        types.append("object")
    result = {"type": sorted(set(types + ["null"]))}
    if props:
        result.update(properties=props, additionalProperties=True)
    if "items" in node:
        result["items"] = normalize(node["items"], spec)
    for variant in variants:
        if "items" in variant:
            result["items"] = variant["items"]
    # Dates remain strings; monetary numeric values are not rescaled.
    return result


def main():
    response = requests.get(URL, timeout=60)
    response.raise_for_status()
    spec = response.json()
    components = spec["components"]["schemas"]

    def item(path):
        body = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        return normalize(body, spec)["properties"]["data"]["items"]

    schemas = {
        name: normalize(components[component], spec)
        for name, component in {
            "payables": "payable",
            "settlements": "settlement",
            "suppliers": "supplier",
            "users": "user",
            "bank_fees": "bankFee",
            "wallet_loads": "walletLoad",
        }.items()
    }
    schemas["accounts"] = item("/v1/experimental/accounts")
    schemas["cost_centers"] = item("/v1/cost-centers")
    schemas["analytical_fields"] = item("/v1/analytical-fields")
    schemas["analytical_fields"]["properties"]["values"] = {
        "type": ["array", "null"],
        "items": item("/v1/analytical-fields/{fieldId}/values"),
    }
    schemas["payables"]["properties"].update(
        {
            "export_ready": {"type": "boolean"},
            "search_state": {"type": "string"},
            "search_version": {"type": "number"},
            "attachments_status": {"type": "string"},
            "attachments": {"type": "array", "items": item("/v1/payables/{payableId}/attachments")},
        }
    )
    destination = Path(__file__).resolve().parents[1] / "tap_spendesk" / "schemas"
    destination.mkdir(exist_ok=True)
    for name, schema in schemas.items():
        schema["type"] = "object"
        (destination / f"{name}.json").write_text(json.dumps(schema, indent=2) + "\n")


if __name__ == "__main__":
    main()
