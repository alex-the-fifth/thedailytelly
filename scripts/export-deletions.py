"""Export minimal deletion IDs for the operator's protected backup-deletion ledger."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from app import platform_store as store

with store.registry() as conn:
    print(json.dumps([dict(row) for row in conn.execute('SELECT * FROM deletion_records ORDER BY deleted_at')]))
