"""
Generates a realistic sample sheet for testing the pipeline end to end.

Most rows are clean, but a handful are deliberately messy (duplicate ids,
junk amounts, unparseable dates, unknown statuses, blank rows, missing
emails) so that running a sync visibly exercises every rule in
clean_data(). Seeded so the output is reproducible.

Usage:
    python scripts/generate_sample.py            # writes 150 clean rows + mess
    python scripts/generate_sample.py 300        # custom clean-row count
"""

import csv
import random
import sys
from datetime import date, timedelta

SEED = 42
VALID_STATUSES = ["active", "inactive", "pending", "completed", "refunded"]

FIRST_NAMES = [
    "Alice", "Bob", "Carol", "David", "Eva", "Frank", "Grace", "Henry",
    "Isla", "Jack", "Karen", "Liam", "Mia", "Noah", "Olivia", "Peter",
    "Quinn", "Rachel", "Sam", "Tina", "Uma", "Victor", "Wendy", "Xavier",
    "Yara", "Zach", "Aisha", "Diego", "Mei", "Omar", "Priya", "Sven",
]
LAST_NAMES = [
    "Johnson", "Martinez", "Singh", "Chen", "Brown", "Wright", "Lee",
    "Davis", "Patel", "Wilson", "White", "Garcia", "Robinson", "Khan",
    "Park", "Adams", "Murphy", "Stone", "Turner", "Foster", "Reddy",
    "Hugo", "Cross", "Bell", "Ali", "Knight", "Nguyen", "Silva",
]


def make_clean_rows(n, start_id=1001):
    """Builds n well-formed rows with believable names, amounts, and dates."""
    rng = random.Random(SEED)
    base = date(2026, 1, 1)
    rows = []
    for i in range(n):
        rid = start_id + i
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        name = f"{first} {last}"
        email = f"{first.lower()}.{last.lower()}@example.com"
        amount = round(rng.uniform(10, 2500), 2)
        order_date = (base + timedelta(days=rng.randint(0, 150))).isoformat()
        status = rng.choice(VALID_STATUSES)
        rows.append([rid, name, email, amount, order_date, status])
    return rows


def add_messy_rows(rows):
    """Sprinkles in the bad data that clean_data() is supposed to handle."""
    rng = random.Random(SEED + 1)

    # duplicate ~8 existing ids (these should collapse to one each)
    for r in rng.sample(rows, 8):
        dup = list(r)
        dup[1] = dup[1] + " (dup)"  # tweak name so it's obviously the copy
        rows.append(dup)

    next_id = max(r[0] for r in rows) + 1

    # 4 rows with junk in the amount column -> should become 0.0
    for _ in range(4):
        rows.append([next_id, "Junk Amount", "junk@example.com",
                     "not_a_number", "2026-03-15", "active"])
        next_id += 1

    # 4 rows with an unparseable date -> should become NULL
    for _ in range(4):
        rows.append([next_id, "Bad Date", "baddate@example.com",
                     199.99, "31/02/not-a-date", "completed"])
        next_id += 1

    # 6 rows with an unknown status -> should be filtered out entirely
    for bad in ["deleted", "unknown", "archived", "void", "n/a", "shipped"]:
        rows.append([next_id, "Bad Status", "badstatus@example.com",
                     250.00, "2026-04-01", bad])
        next_id += 1

    # 3 rows with a missing email -> should be KEPT (only blank rows go)
    for _ in range(3):
        rows.append([next_id, "No Email", "", 88.00, "2026-04-10", "pending"])
        next_id += 1

    # 2 fully blank rows -> should be dropped
    rows.append(["", "", "", "", "", ""])
    rows.append(["", "", "", "", "", ""])

    return rows


def main():
    clean_count = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    rows = make_clean_rows(clean_count)
    rows = add_messy_rows(rows)

    out_path = "sample_sheet_large.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "customer_name", "email", "amount",
                         "order_date", "status"])
        writer.writerows(rows)

    print(f"wrote {len(rows)} data rows to {out_path} "
          f"({clean_count} clean + messy test rows)")


if __name__ == "__main__":
    main()
