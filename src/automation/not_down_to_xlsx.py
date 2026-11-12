"""
Convert not_down.json -> Not down.xlsx.

Usage:
    python3 -m src.automation.not_down_to_xlsx
    python3 -m src.automation.not_down_to_xlsx --json path/to/not_down.json --xlsx "path/to/Not down.xlsx"
"""
import argparse
import json
from pathlib import Path

import pandas as pd

from src.logging import logger


COLUMNS = ["Website", "File Url", "Code", "ID",
           "Tracking Number", "Reason", "Status", "Date"]


def load_entries(json_path: Path) -> list:
    if not json_path.is_file():
        logger.warning(f"{json_path} not found; nothing to convert.")
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        logger.exception(f"Could not read {json_path}.")
        return []


def to_rows(entries: list) -> list:
    return [{
        "Website": e.get("website", ""),
        "File Url": e.get("file_url", ""),
        "Code": e.get("code", ""),
        "ID": e.get("id", ""),
        "Tracking Number": e.get("tracking_number", ""),
        "Reason": e.get("reason", ""),
        "Status": e.get("status", ""),
        "Date": e.get("recorded_at", ""),
    } for e in entries]


def convert(json_path: Path, xlsx_path: Path) -> bool:
    entries = load_entries(json_path)
    rows = to_rows(entries)
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.to_excel(xlsx_path, index=False)
    logger.info(f"Wrote {xlsx_path} ({len(df)} row(s)).")
    return True


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Convert not_down.json to Not down.xlsx")
    parser.add_argument("--json", default=str(here / "not_down.json"),
                        help="Path to not_down.json (default: alongside this script)")
    parser.add_argument("--xlsx", default=str(here / "Not down.xlsx"),
                        help="Path to the output XLSX")
    args = parser.parse_args()
    convert(Path(args.json), Path(args.xlsx))


if __name__ == "__main__":
    main()