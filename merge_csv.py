#!/usr/bin/env python3
"""
Merge two or more CSV files into a single CSV, taking the union of all columns.

Input files may have different columns. The output header is the union of every
input file's columns, kept in order of first appearance (so a file's column order
is preserved, with new columns from later files appended). Any column a given row
doesn't have is left blank.

Memory-light: rows are streamed one at a time rather than loading whole files,
so it handles large inputs without holding everything in memory.

Examples:
    python merge_csv.py a.csv b.csv
    python merge_csv.py a.csv b.csv c.csv -o combined.csv
    python merge_csv.py reports/*.csv -o all.csv
"""

import argparse
import csv
import os
import sys
from datetime import datetime


def parse_args():
    parser = argparse.ArgumentParser(
        description="Merge 2+ CSV files (union of columns) into one CSV."
    )
    parser.add_argument(
        "files", nargs="+", metavar="CSV",
        help="Input CSV files to merge (two or more).",
    )
    parser.add_argument(
        "-o", "--output", default=None, metavar="PATH",
        help="Output CSV path (default: merged_YYYYMMDD_HHMMSS.csv).",
    )
    return parser.parse_args()


def read_header(path):
    """Return the list of column names for a CSV file ([] if it has no header)."""
    # utf-8-sig transparently strips a BOM if the file was written by Excel.
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            return row            # the first row is the header
    return []                     # empty file


def collect_columns(files):
    """Union of all columns across files, in order of first appearance."""
    columns, seen = [], set()
    for path in files:
        for col in read_header(path):
            if col not in seen:
                seen.add(col)
                columns.append(col)
    return columns


def merge(files, columns, output_path):
    """Stream every row from each file into the merged output. Returns row count."""
    total = 0
    with open(output_path, "w", newline="", encoding="utf-8") as out:
        # restval="" blanks columns a row lacks; extrasaction="ignore" drops any
        # stray extra fields rather than crashing.
        writer = csv.DictWriter(out, fieldnames=columns, restval="",
                                extrasaction="ignore")
        writer.writeheader()
        for path in files:
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    print(f"  note: '{path}' has no header/rows; skipping.")
                    continue
                count = 0
                for row in reader:
                    writer.writerow(row)
                    count += 1
                total += count
                print(f"  {path}: {count} row(s)")
    return total


def main():
    args = parse_args()

    # All inputs must exist before we start writing anything.
    missing = [p for p in args.files if not os.path.isfile(p)]
    if missing:
        print("ERROR: these input file(s) were not found:")
        for p in missing:
            print(f"  - {p}")
        return 1

    output_path = args.output or f"merged_{datetime.now():%Y%m%d_%H%M%S}.csv"

    # Don't let the output clobber one of the inputs (we open it for writing
    # before reading the inputs, which would truncate it first).
    out_abs = os.path.abspath(output_path)
    if out_abs in {os.path.abspath(p) for p in args.files}:
        print(f"ERROR: output '{output_path}' is also an input file. "
              "Choose a different --output path.")
        return 1

    if len(args.files) < 2:
        print("NOTE: only one input file given; the output is effectively a copy.")

    print(f"Merging {len(args.files)} file(s)...")

    try:
        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        columns = collect_columns(args.files)
        if not columns:
            print("ERROR: no columns found across the input files (all empty?).")
            return 1

        total = merge(args.files, columns, output_path)
    except OSError as e:
        print(f"ERROR: problem reading/writing files: {e}")
        return 1
    except csv.Error as e:
        print(f"ERROR: CSV parsing failed: {e}")
        return 1

    print(f"Done. {total} row(s), {len(columns)} column(s).")
    print(f"Merged CSV written to: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
