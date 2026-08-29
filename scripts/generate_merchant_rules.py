"""
Learn merchant rules from historical transactions.
- Groups store name variants (e.g. "VOI FI", "VOI FI, HELSINKI, FI") into contains rules
- Creates exact rules for unambiguous single-name stores
- Skips low-confidence and generic stores
"""
import os
import sys

# This script lives in scripts/ but imports the app's modules, which sit in the
# repo root — put the root on the path before importing them.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from collections import defaultdict

# Reuse the app's DB connection helper so this script resolves the database by
# absolute path (relative to database.py), never relative to the current working
# directory — running it from any folder hits the same expenses.db.
from database import get_db

NOISE_STORES = {"Other", "Rent", "Missing info", "Monthly fee", "Korko", ""}
CONFIDENCE_THRESHOLD = 70.0

def fetch_store_dominants(cur):
    """Return {store: {category_id, category_name, confidence, total}} for all stores."""
    cur.execute("""
        SELECT
            t.store,
            t.category_id,
            c.name as category_name,
            COUNT(*) as cnt,
            total_counts.total,
            ROUND(100.0 * COUNT(*) / total_counts.total, 1) as confidence
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        JOIN (
            SELECT store, COUNT(*) as total
            FROM transactions
            WHERE store != '' AND store IS NOT NULL
            GROUP BY store
        ) total_counts ON t.store = total_counts.store
        WHERE t.store != '' AND t.store IS NOT NULL
        GROUP BY t.store, c.id
        ORDER BY t.store, cnt DESC
    """)
    store_data = {}
    for r in cur.fetchall():
        store = r["store"]
        if store not in store_data:
            store_data[store] = {
                "category_id": r["category_id"],
                "category_name": r["category_name"],
                "confidence": r["confidence"],
                "total": r["total"],
                "cnt": r["cnt"],
            }
    return store_data

def find_contains_groups(store_data):
    """
    Find groups of stores that are variants of the same merchant.
    Criteria: store A is a case-insensitive substring of store B,
    both have the same dominant category.
    Returns list of (pattern, category_id, category_name, covered_stores).
    """
    eligible = {
        s: d for s, d in store_data.items()
        if s not in NOISE_STORES and d["confidence"] >= CONFIDENCE_THRESHOLD
    }

    stores_by_len = sorted(eligible.keys(), key=len)
    used = set()
    contains_rules = []

    for base in stores_by_len:
        if base in used or len(base) < 4:
            continue
        base_lower = base.lower()
        base_cat = eligible[base]["category_id"]

        # Find all stores that contain this base (case-insensitive) and same category
        group = [base]
        for other in stores_by_len:
            if other == base or other in used:
                continue
            if (base_lower in other.lower()
                    and eligible[other]["category_id"] == base_cat):
                group.append(other)

        # Only make a contains rule if it covers >1 distinct stores
        if len(group) > 1:
            total_txns = sum(eligible[s]["total"] for s in group)
            contains_rules.append({
                "pattern": base,
                "category_id": base_cat,
                "category_name": eligible[base]["category_name"],
                "match_type": "contains",
                "covered": group,
                "total_txns": total_txns,
            })
            used.update(group)

    return contains_rules, used

def find_exact_rules(store_data, covered_by_contains):
    """Exact rules for stores not covered by contains rules."""
    exact_rules = []
    for store, d in store_data.items():
        if store in covered_by_contains:
            continue
        if store in NOISE_STORES:
            continue
        if d["confidence"] < CONFIDENCE_THRESHOLD:
            continue
        exact_rules.append({
            "pattern": store,
            "category_id": d["category_id"],
            "category_name": d["category_name"],
            "match_type": "exact",
            "total_txns": d["total"],
        })
    return exact_rules

def insert_rules(cur, rules):
    inserted = 0
    skipped = 0
    for rule in rules:
        # Skip if pattern already exists
        cur.execute(
            "SELECT id FROM merchant_rules WHERE LOWER(pattern) = LOWER(?)",
            (rule["pattern"],)
        )
        if cur.fetchone():
            skipped += 1
            continue
        cur.execute(
            "INSERT INTO merchant_rules (pattern, category_id, match_type) VALUES (?, ?, ?)",
            (rule["pattern"], rule["category_id"], rule["match_type"])
        )
        inserted += 1
    return inserted, skipped

def main():
    conn = get_db()
    cur = conn.cursor()

    print("Reading transaction history...")
    store_data = fetch_store_dominants(cur)
    print(f"  {len(store_data)} unique stores found")

    print("\nFinding contains-pattern groups...")
    contains_rules, covered = find_contains_groups(store_data)
    print(f"  {len(contains_rules)} contains rules (covering {len(covered)} store variants)")
    for r in sorted(contains_rules, key=lambda x: -x["total_txns"]):
        print(f"    [contains] '{r['pattern']}' → {r['category_name']} ({r['total_txns']} txns, covers: {r['covered']})")

    print("\nFinding exact rules...")
    exact_rules = find_exact_rules(store_data, covered)
    print(f"  {len(exact_rules)} exact rules")

    all_rules = contains_rules + exact_rules
    print(f"\nTotal rules to insert: {len(all_rules)}")

    print("\nClearing existing merchant rules...")
    cur.execute("DELETE FROM merchant_rules")

    print("Inserting rules...")
    inserted, skipped = insert_rules(cur, all_rules)
    conn.commit()

    print(f"\nDone. Inserted: {inserted}, Skipped (duplicates): {skipped}")

    # Summary by category
    cur.execute("""
        SELECT c.name, COUNT(*) as cnt
        FROM merchant_rules mr
        JOIN categories c ON mr.category_id = c.id
        GROUP BY c.name
        ORDER BY cnt DESC
    """)
    print("\nRules by category:")
    for r in cur.fetchall():
        print(f"  {r['name']:<30} {r['cnt']}")

    conn.close()

if __name__ == "__main__":
    main()
