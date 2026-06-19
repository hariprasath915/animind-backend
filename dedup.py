import os
import sys
from collections import defaultdict
from auth_utils import get_supabase
from datetime import datetime

def deduplicate():
    print("Fetching all contents...")
    try:
        supabase = get_supabase()
    except Exception as e:
        print("Failed to init supabase:", e)
        return

    # Fetch all rows. We might need pagination if there are > 1000 rows.
    # Supabase Python client limit is 1000 by default.
    all_rows = []
    offset = 0
    limit = 1000
    while True:
        res = supabase.table("contents").select("id, user_id, updated_at, created_at, body").range(offset, offset + limit - 1).execute()
        if not res.data:
            break
        all_rows.extend(res.data)
        if len(res.data) < limit:
            break
        offset += limit

    print(f"Total rows fetched: {len(all_rows)}")

    # Group by (user_id, anim_id)
    groups = defaultdict(list)
    for row in all_rows:
        user_id = row.get("user_id")
        body = row.get("body") or {}
        anim_id = body.get("anim_id")
        if user_id and anim_id:
            groups[(user_id, anim_id)].append(row)

    to_delete = []
    for (user_id, anim_id), rows in groups.items():
        if len(rows) > 1:
            print(f"Found {len(rows)} duplicates for user {user_id}, anim_id {anim_id}")
            # Sort by updated_at desc, then created_at desc
            def get_time(r):
                t = r.get("updated_at") or r.get("created_at") or ""
                # Replace 'Z' with '+00:00' to parse ISO if needed, but string comparison works for ISO8601
                return t
            
            rows.sort(key=get_time, reverse=True)
            
            # Keep the first one, delete the rest
            for r in rows[1:]:
                to_delete.append(r["id"])

    if not to_delete:
        print("No duplicates found. Safe to add unique constraint.")
        return

    print(f"Deleting {len(to_delete)} duplicate rows...")
    # Delete in batches to avoid URI too long
    batch_size = 100
    for i in range(0, len(to_delete), batch_size):
        batch = to_delete[i:i+batch_size]
        res = supabase.table("contents").delete().in_("id", batch).execute()
        print(f"Deleted batch of {len(batch)}.")

    print("Deduplication complete.")

if __name__ == "__main__":
    deduplicate()
