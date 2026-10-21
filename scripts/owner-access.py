"""Issue a private, one-time owner link while public email sign-in is disabled."""
import argparse
import os
from pathlib import Path
import secrets
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from app import platform_store as store


def issue_owner_link(email, hours=24):
    email=email.strip().lower()
    if os.environ.get('DAILYTELLY_MAIL_MODE')!='disabled':
        raise ValueError('Owner links require DAILYTELLY_MAIL_MODE=disabled; use email sign-in otherwise')
    if email not in store.owner_emails():
        raise ValueError('The email must already be in the configured owner allowlist')
    token=secrets.token_urlsafe(48)
    with store.registry() as conn:
        row=conn.execute('SELECT * FROM accounts WHERE email=?',(email,)).fetchone()
        if row and row['status']=='suspended':
            raise ValueError('Restore the account explicitly before issuing a link')
        if not row:
            conn.execute("INSERT INTO accounts (id,email,created_at,verified_at,status) VALUES (?,?,?,?,'active')",
                         (secrets.token_hex(16),email,store.now(),store.now()))
        else:
            conn.execute("UPDATE accounts SET status='active',verified_at=COALESCE(verified_at,?) WHERE email=?",(store.now(),email))
        account=dict(conn.execute('SELECT * FROM accounts WHERE email=?',(email,)).fetchone())
        conn.execute('DELETE FROM operator_links WHERE account_id=?',(account['id'],))
        conn.execute('INSERT INTO operator_links VALUES (?,?,?)',(store.digest(token),account['id'],store.now()+int(hours*3600)))
    store.initialize_library(account)
    store.audit('operator','owner_link_issued',account['id'])
    return store.base_url()+'/owner-access#'+token


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email',required=True)
    parser.add_argument('--hours',type=int,default=24,choices=range(1,25))
    args=parser.parse_args()
    store.initialize()
    try:print(issue_owner_link(args.email,args.hours))
    except ValueError as error:parser.exit(1,str(error)+'\n')
