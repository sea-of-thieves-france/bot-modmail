"""
One-time migration: rename MongoDB collection 'logs' → 'tickets'
============================================================
Run this script ONCE and BEFORE deploying the updated bot code that uses
the 'tickets' collection.  After the migration the old 'logs' collection
will no longer exist and the bot will read/write 'tickets' instead.

Usage
-----
    python scripts/migrate_logs_to_tickets.py

    # or supply the URI directly to skip the prompt:
    MONGODB_URI="mongodb+srv://..." python scripts/migrate_logs_to_tickets.py

Requirements
------------
    pip install motor  (already a bot dependency)

Steps performed
---------------
1. Connect to the 'modmail_bot' database.
2. Check whether 'logs' exists and 'tickets' does not (safe default).
3. Use MongoDB's *renameCollection* admin command (instant, no data copy).
4. Print a confirmation with the document count.
"""

import asyncio
import os
import sys

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    print("ERROR: 'motor' is not installed.  Run:  pip install motor")
    sys.exit(1)


async def main() -> None:
    uri = os.environ.get("MONGODB_URI") or os.environ.get("CONNECTION_URI")
    if not uri:
        uri = input("Enter your MongoDB connection URI: ").strip()
    if not uri:
        print("No URI provided.  Aborting.")
        sys.exit(1)

    client = AsyncIOMotorClient(uri)
    db = client.modmail_bot

    # ------------------------------------------------------------------ checks
    try:
        colls = await db.list_collection_names()
    except Exception as exc:
        print(f"ERROR: Could not connect to database: {exc}")
        client.close()
        sys.exit(1)

    if "logs" not in colls:
        if "tickets" in colls:
            print("'tickets' collection already exists and 'logs' is gone — nothing to migrate.")
        else:
            print("Neither 'logs' nor 'tickets' found.  Nothing to migrate.")
        client.close()
        return

    if "tickets" in colls:
        print()
        print("WARNING: A 'tickets' collection already exists alongside 'logs'.")
        print("         Choosing to proceed will DROP the existing 'tickets' collection")
        print("         and replace it with the contents of 'logs'.")
        print()
        logs_count = await db.logs.count_documents({})
        tickets_count = await db.tickets.count_documents({})
        print(f"  logs    documents : {logs_count}")
        print(f"  tickets documents : {tickets_count}")
        print()
        confirm = input("Type 'yes' to replace 'tickets' with 'logs', or anything else to abort: ").strip()
        if confirm.lower() != "yes":
            print("Aborted.  No changes made.")
            client.close()
            return
        drop_target = True
    else:
        drop_target = False

    # ---------------------------------------------------------------- migrate
    logs_count = await db.logs.count_documents({})
    print(f"\nRenaming 'logs' → 'tickets'  ({logs_count} documents) …")

    try:
        await client.admin.command(
            {
                "renameCollection": "modmail_bot.logs",
                "to": "modmail_bot.tickets",
                "dropTarget": drop_target,
            }
        )
    except Exception as exc:
        print(f"\nERROR during renameCollection: {exc}")
        print(
            "If you are using a shared/free-tier MongoDB Atlas cluster, "
            "renameCollection may be disabled.  In that case you must use the "
            "Atlas web UI or mongosh to rename the collection manually:\n"
            "  db.logs.renameCollection('tickets')"
        )
        client.close()
        sys.exit(1)

    tickets_count = await db.tickets.count_documents({})
    print(f"\n✅  Migration complete!  'tickets' now contains {tickets_count} documents.")
    print("    You can now deploy and start the updated bot.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
