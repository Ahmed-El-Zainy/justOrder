"""Fetch the source dataset from Kaggle.

Idempotent: if the CSV is already on disk the download is skipped, so a reviewer
who obtained the data another way is not forced through a Kaggle login.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

DATASET = "sohier/large-purchases-by-the-state-of-ca"
DATA_DIR = Path(__file__).resolve().parent.parent / "kaggle_data"
CSV_NAME = "PURCHASE ORDER DATA EXTRACT 2012-2015_0.csv"

# Measured from the published file. Used only to spot a truncated download.
EXPECTED_MIN_BYTES = 150_000_000


def csv_path() -> Path:
    return DATA_DIR / CSV_NAME


def already_present() -> bool:
    path = csv_path()
    return path.is_file() and path.stat().st_size >= EXPECTED_MIN_BYTES


def download(force: bool = False) -> Path:
    path = csv_path()

    if already_present() and not force:
        size_mb = path.stat().st_size / 1_000_000
        print(f"[download] already present: {path.name} ({size_mb:.0f} MB) — skipping")
        return path

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[download] fetching {DATASET} into {DATA_DIR}")

    try:
        subprocess.run(
            ["kaggle", "datasets", "download", "-d", DATASET, "-p", str(DATA_DIR)],
            check=True,
        )
    except FileNotFoundError:
        sys.exit(
            "[download] the `kaggle` CLI is not installed.\n"
            "           pip install kaggle, then put credentials at ~/.kaggle/kaggle.json"
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(f"[download] kaggle CLI failed ({exc.returncode}). Check ~/.kaggle/kaggle.json")

    for archive in DATA_DIR.glob("*.zip"):
        print(f"[download] extracting {archive.name}")
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(DATA_DIR)
        archive.unlink()

    if not path.is_file():
        sys.exit(f"[download] expected {CSV_NAME} after extraction, but it is missing")

    size_mb = path.stat().st_size / 1_000_000
    print(f"[download] ready: {path.name} ({size_mb:.0f} MB)")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Download the CA large-purchases dataset")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args()
    download(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
