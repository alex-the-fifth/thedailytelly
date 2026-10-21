"""Import a legacy backup; dry-run by default. Never changes the source database."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'server'))
from app import platform_store as store
from app.migration import import_backup

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True, help='Consistent SQLite backup, not the running database')
parser.add_argument('--email', required=True, help='Existing verified destination account')
parser.add_argument('--apply', action='store_true', help='Import into the empty destination library')
args = parser.parse_args()
store.initialize()
try:
    print(json.dumps(import_backup(args.source,args.email,args.apply),indent=2))
except (ValueError, OSError) as error:
    parser.exit(1, str(error)+'\n')
