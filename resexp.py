#!/usr/bin/env python3
"""
Inventory all AWS resources in a region using the Resource Groups Tagging API
and write the result to a timestamped CSV file.

Output is one row per resource. Every distinct tag key found across the whole
dump becomes its own column, prefixed with "tag~" (e.g. tag~project_id,
tag~department); each resource's value lands in the matching column, and tag
columns a resource doesn't have are left blank.

Read-only. Requires only the IAM action: tag:GetResources
(granted by ResourceGroupsTaggingAPIReadOnlyAccess, ReadOnlyAccess, or ViewOnlyAccess).

Prereqs:
    pip install boto3
    # credentials for the target account/partition configured (e.g. a GovCloud profile)

Examples:
    python resexp_inventory.py                       # full inventory (all pages)
    python resexp_inventory.py --max-pages 2          # small test run (~200 resources)
    python resexp_inventory.py --region us-gov-west-1
    python resexp_inventory.py --tag-key Environment --tag-value Prod
    python resexp_inventory.py --tag-key Owner --output-dir ./reports
"""

import argparse
import csv
import os
import sys
from datetime import datetime

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    EndpointConnectionError,
)

DEFAULT_REGION = "us-east-1"
FILE_PREFIX = "resexp"

# Fixed (non-tag) columns, in order. Tag columns (tag~<key>) are discovered at
# runtime and appended after these.
BASE_COLUMNS = [
    "account_id",
    "partition",
    "region",
    "service",
    "resource_type",
    "resource_id",
    "resource_arn",
]
TAG_PREFIX = "tag~"


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def parse_args():
    parser = argparse.ArgumentParser(
        description="Inventory AWS resources via the Resource Groups Tagging API."
    )
    parser.add_argument(
        "--region", default=DEFAULT_REGION,
        help=f"AWS region to inventory (default: {DEFAULT_REGION}).",
    )
    parser.add_argument(
        "--tag-key", default=None,
        help="Optional tag key to filter on.",
    )
    parser.add_argument(
        "--tag-value", default=None,
        help="Optional tag value to filter on (use together with --tag-key).",
    )
    parser.add_argument(
        "--output-dir", default=".",
        help="Directory to write the CSV into (default: current directory).",
    )
    parser.add_argument(
        "--max-pages", type=int, default=0, metavar="N",
        help="Stop after N pages of ~100 resources each (default: 0 = all pages). "
             "Use e.g. --max-pages 2 for a small test run in production.",
    )
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# AWS interaction
# --------------------------------------------------------------------------- #
def build_tag_filters(tag_key, tag_value):
    """Translate the optional tag key/value into the API's TagFilters format."""
    if not tag_key:
        if tag_value:
            print("WARNING: --tag-value given without --tag-key; ignoring the filter.")
        return []
    tag_filter = {"Key": tag_key}
    if tag_value:
        tag_filter["Values"] = [tag_value]
    return [tag_filter]


def fetch_resources(client, tag_filters, max_pages=0):
    """Page through GetResources and return the raw resource mappings.

    max_pages > 0 stops after that many pages (~100 resources each) so a test
    run doesn't pull the whole account; 0 fetches every page.
    """
    resources = []
    paginator = client.get_paginator("get_resources")
    page_kwargs = {"ResourcesPerPage": 100}
    if tag_filters:
        page_kwargs["TagFilters"] = tag_filters
    for page_num, page in enumerate(paginator.paginate(**page_kwargs), start=1):
        resources.extend(page.get("ResourceTagMappingList", []))
        if max_pages > 0 and page_num >= max_pages:
            break
    return resources


# --------------------------------------------------------------------------- #
# Transformation
# --------------------------------------------------------------------------- #
def parse_arn(arn):
    """
    Break an ARN into its components.

    Handles these shapes:
        arn:partition:service:region:account:resource-id
        arn:partition:service:region:account:resource-type/resource-id
        arn:partition:service:region:account:resource-type:resource-id
    """
    fields = arn.split(":", 5)
    fields += [""] * (6 - len(fields))           # pad so unpacking is always safe
    _, partition, service, region, account_id, resource = fields

    # Split on whichever delimiter (":" or "/") appears first, since ARN
    # resource sections use either one and the id itself may contain the other
    # (e.g. "log-group:/aws/lambda/fn" -> type "log-group", id "/aws/lambda/fn").
    delimiters = [i for i in (resource.find(":"), resource.find("/")) if i != -1]
    if delimiters:
        cut = min(delimiters)
        resource_type, resource_id = resource[:cut], resource[cut + 1:]
    else:
        resource_type, resource_id = "", resource

    return {
        "partition": partition,
        "service": service,
        "region": region,
        "account_id": account_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
    }


def collect_tag_keys(resources):
    """Return the sorted set of every distinct tag key across all resources."""
    keys = set()
    for item in resources:
        for tag in item.get("Tags", []):
            key = tag.get("Key", "")
            if key:
                keys.add(key)
    return sorted(keys)


def build_rows(resources):
    """Build the inventory as one row per resource with tags pivoted to columns.

    Each distinct tag key becomes a "tag~<key>" column. Returns
    (fieldnames, rows); rows omit tag columns the resource doesn't have, and the
    CSV writer fills those blanks.
    """
    tag_keys = collect_tag_keys(resources)
    fieldnames = BASE_COLUMNS + [TAG_PREFIX + k for k in tag_keys]

    rows = []
    for item in resources:
        arn = item.get("ResourceARN", "")
        parts = parse_arn(arn)
        row = {
            "account_id": parts["account_id"],
            "partition": parts["partition"],
            "region": parts["region"],
            "service": parts["service"],
            "resource_type": parts["resource_type"],
            "resource_id": parts["resource_id"],
            "resource_arn": arn,
        }
        for tag in item.get("Tags", []):
            key = tag.get("Key", "")
            if key:
                row[TAG_PREFIX + key] = tag.get("Value", "")
        rows.append(row)

    return fieldnames, rows


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def make_output_path(output_dir):
    """Build <output_dir>/resexp_YYYYMMDD_HHMMSS.csv."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(output_dir, f"{FILE_PREFIX}_{timestamp}.csv")


def write_csv(rows, fieldnames, filepath):
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        # restval="" fills in tag columns a given resource doesn't have.
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def main():
    args = parse_args()

    try:
        client = boto3.client("resourcegroupstaggingapi", region_name=args.region)
    except Exception as e:
        print(f"ERROR: could not create the tagging API client: {e}")
        return 1

    tag_filters = build_tag_filters(args.tag_key, args.tag_value)
    if tag_filters:
        shown_value = tag_filters[0].get("Values", ["<any value>"])[0]
        print(f"Filtering on tag  {tag_filters[0]['Key']} = {shown_value}")

    if args.max_pages > 0:
        print(f"LIMITED RUN: fetching at most {args.max_pages} page(s) "
              f"(~{args.max_pages * 100} resources) -- this is NOT a full inventory.")

    print(f"Inventorying resources in {args.region} ...")

    try:
        resources = fetch_resources(client, tag_filters, args.max_pages)
    except NoCredentialsError:
        print("ERROR: no AWS credentials found. Configure your profile/keys and retry.")
        return 1
    except EndpointConnectionError:
        print(f"ERROR: could not reach the endpoint for region '{args.region}'. "
              "Check the region name and your network.")
        return 1
    except ClientError as e:
        err = e.response.get("Error", {})
        code = err.get("Code", "Unknown")
        if code in ("AccessDeniedException", "UnauthorizedException"):
            print(f"ERROR: access denied ({code}). The identity needs tag:GetResources.")
        else:
            print(f"ERROR: API call failed [{code}]: {err.get('Message', '')}")
        return 1
    except BotoCoreError as e:
        print(f"ERROR: AWS SDK error: {e}")
        return 1

    if not resources:
        print("No resources matched the filter." if tag_filters
              else "No resources returned for this region/account.")
        return 0

    fieldnames, rows = build_rows(resources)
    tag_column_count = len(fieldnames) - len(BASE_COLUMNS)

    try:
        os.makedirs(args.output_dir, exist_ok=True)
        output_path = make_output_path(args.output_dir)
        write_csv(rows, fieldnames, output_path)
    except OSError as e:
        print(f"ERROR: could not write the CSV file: {e}")
        return 1

    print(f"Done. {len(rows)} resource(s), {tag_column_count} tag column(s).")
    print(f"Inventory written to: {output_path}")
    if args.max_pages > 0:
        print(f"REMINDER: this was a LIMITED run ({args.max_pages} page(s)); "
              "rerun without --max-pages for the complete inventory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
