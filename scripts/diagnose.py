"""Local aggregate diagnostics. Never prints account emails or viewing data."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from app import platform_store as store, resources

with store.registry() as conn:
    print('Database integrity:',conn.execute('PRAGMA integrity_check').fetchone()[0])
    for table in ('accounts','paired_devices'):
        print(table+':',conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0])
print(resources.snapshot())
