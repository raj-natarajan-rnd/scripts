#!/usr/bin/env python3
"""
list_aws_resources.py

List AWS resources using the AWS Resource Explorer (v2) Search API,
after running prechecks to confirm the service is ready to query.

Prechecks performed:
  1. An index exists in the target Region and is ACTIVE.
  2. The index type is reported. A LOCAL index searches only its own Region;
     an AGGREGATOR index searches every Region that replicates into it.
  3. A view is available (explicit --view-arn, the Region default view, or the
     single existing view). Search() cannot run without a view.

Usage:
  python list_aws_resources.py
  python list_aws_resources.py --region us-east-1 --profile myprofile
  python list_aws_resources.py --query "service:ec2 region:us-east-1"
  python list_aws_resources.py --query "resourcetype:ec2:instance" --limit 50
  python list_aws_resources.py --json > resources.json

Common query strings:
  "*"                          all resources in scope
  "service:s3"                 all S3 resources
  "resourcetype:ec2:instance"  only EC2 instances
  "region:eu-west-1"           resources in a Region (needs an AGGREGATOR index)
  "tag.key:aws-apn-id"         resources that carry a specific tag key
  "tag:Environment=prod"       resources with a specific tag key=value

IAM permissions required:
  resource-explorer-2:GetIndex
  resource-explorer-2:ListIndexes
  resource-explorer-2:GetDefaultView
  resource-explorer-2:ListViews
  resource-explorer-2:Search
"""

import argparse
import json
import sys
from datetime import datetime

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    NoRegionError,
)

SEARCH_PAGE_SIZE = 1000  # maximum allowed per Search() call


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def build_client(region=None, profile=None):
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("resource-explorer-2")


# --------------------------------------------------------------------------- #
# Prechecks
# --------------------------------------------------------------------------- #
def get_local_index(client):
    """Return details of the index in the current Region, or None if absent."""
    try:
        resp = client.get_index()
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise
    return {
        "arn": resp.get("Arn"),
        "type": resp.get("Type"),                       # LOCAL or AGGREGATOR
        "state": resp.get("State"),                     # CREATING/ACTIVE/UPDATING/...
        "replicating_from": resp.get("ReplicatingFrom", []),
    }


def list_all_indexes(client):
    """Return every index in the account across all Regions."""
    indexes, kwargs = [], {}
    while True:
        resp = client.list_indexes(**kwargs)
        indexes.extend(resp.get("Indexes", []))
        token = resp.get("NextToken")
        if not token:
            return indexes
        kwargs["NextToken"] = token


def resolve_view(client, explicit_view=None):
    """
    Decide which view Search() should use.
    Order: explicit --view-arn  ->  Region default view  ->  sole existing view.
    Returns (view_arn, message); view_arn is None if none can be resolved.
    """
    if explicit_view:
        return explicit_view, f"Using supplied view: {explicit_view}"

    try:
        default = client.get_default_view().get("ViewArn")
    except ClientError:
        default = None
    if default:
        return default, f"Using Region default view: {default}"

    views, kwargs = [], {}
    while True:
        resp = client.list_views(**kwargs)
        views.extend(resp.get("Views", []))
        token = resp.get("NextToken")
        if not token:
            break
        kwargs["NextToken"] = token

    if len(views) == 1:
        return views[0], f"No default set; using the only view found: {views[0]}"
    if not views:
        return None, "No view exists in this Region. Create one before searching."
    listed = "\n  ".join(views)
    return None, (
        "Multiple views exist but no default is set. "
        "Re-run with --view-arn set to one of:\n  " + listed
    )


def run_prechecks(client, explicit_view=None):
    """
    Confirm Resource Explorer is ready. Returns (ok, view_arn).
    Prints a readable report along the way.
    """
    region = client.meta.region_name
    print(f"== Resource Explorer prechecks (Region: {region}) ==\n")

    index = get_local_index(client)
    if index is None:
        print(f"[FAIL] No Resource Explorer index in {region}.")
        others = list_all_indexes(client)
        if others:
            print("       Indexes do exist in other Regions:")
            for idx in others:
                print(f"         - {idx['Region']:<15} {idx['Type']}")
            print("       Re-run with --region pointing at one of those,")
            print("       or create an index/aggregator in this Region.")
        else:
            print("       No indexes exist anywhere in this account.")
            print("       Turn on Resource Explorer first (console or the")
            print("       create-index API), ideally with an AGGREGATOR index.")
        return False, None

    print(f"[ OK ] Index found: {index['arn']}")
    if index["type"] == "AGGREGATOR":
        print(f"       Type : {index['type']}  (search spans all replicating Regions)")
    else:
        print(f"       Type : {index['type']}  (LOCAL: search is limited to this Region only)")
    print(f"       State: {index['state']}")

    if index["state"] != "ACTIVE":
        print(f"[FAIL] Index is not ACTIVE yet (state={index['state']}). "
              "Wait for it to finish before searching.")
        return False, None

    view_arn, message = resolve_view(client, explicit_view)
    if view_arn is None:
        print(f"[FAIL] {message}")
        return False, None

    print(f"[ OK ] {message}\n")
    return True, view_arn


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def search_resources(client, view_arn, query="*", limit=None):
    """Paginate Search() and return a list of resource dicts."""
    resources = []
    kwargs = {
        "QueryString": query,
        "ViewArn": view_arn,
        "MaxResults": SEARCH_PAGE_SIZE,
    }
    while True:
        resp = client.search(**kwargs)
        resources.extend(resp.get("Resources", []))
        if limit and len(resources) >= limit:
            return resources[:limit]
        token = resp.get("NextToken")
        if not token:
            return resources
        kwargs["NextToken"] = token


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def print_report(resources):
    if not resources:
        print("No resources matched the query.")
        return

    by_service, by_region = {}, {}
    for r in resources:
        svc = r.get("Service", "?")
        reg = r.get("Region", "?")
        by_service[svc] = by_service.get(svc, 0) + 1
        by_region[reg] = by_region.get(reg, 0) + 1

    print(f"Found {len(resources)} resource(s).\n")

    print("By service:")
    for svc, n in sorted(by_service.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {svc:<22} {n}")

    print("\nBy region:")
    for reg, n in sorted(by_region.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {reg:<22} {n}")

    print("\nResources:")
    for r in resources:
        print(f"  [{r.get('Region', '?'):<14}] "
              f"{r.get('ResourceType', '?'):<28} {r.get('Arn')}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="List AWS resources via Resource Explorer, with prechecks."
    )
    parser.add_argument("--region", help="AWS Region to query (defaults to your config).")
    parser.add_argument("--profile", help="AWS named profile to use.")
    parser.add_argument("--query", default="*", help='Search string (default "*").')
    parser.add_argument("--view-arn", dest="view_arn", help="Explicit view ARN to search.")
    parser.add_argument("--limit", type=int, help="Stop after N resources.")
    parser.add_argument("--json", action="store_true",
                        help="Emit raw JSON instead of a formatted report.")
    args = parser.parse_args(argv)

    try:
        client = build_client(region=args.region, profile=args.profile)
    except (BotoCoreError, ClientError) as exc:
        print(f"Could not create client: {exc}", file=sys.stderr)
        return 2

    try:
        ok, view_arn = run_prechecks(client, explicit_view=args.view_arn)
        if not ok:
            return 1

        resources = search_resources(
            client, view_arn=view_arn, query=args.query, limit=args.limit
        )
    except NoCredentialsError:
        print("No AWS credentials found. Configure them and retry.", file=sys.stderr)
        return 2
    except NoRegionError:
        print("No Region set. Pass --region or configure a default.", file=sys.stderr)
        return 2
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("AccessDeniedException", "UnauthorizedException"):
            print(f"Access denied ({code}): {exc}. Check the IAM permissions "
                  "listed in the file header.", file=sys.stderr)
        else:
            print(f"API error ({code}): {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(resources, sys.stdout, default=_json_default, indent=2)
        sys.stdout.write("\n")
    else:
        print_report(resources)
    return 0


if __name__ == "__main__":
    sys.exit(main())
