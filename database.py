
import sqlite3, os, secrets
from datetime import datetime, timezone, timedelta

DB = os.getenv("DATABASE_FILE", "tigerbot.db")

def conn():
    c = sqlite3.connect(DB, timeout=20, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      telegram_id INTEGER PRIMARY KEY,
      username TEXT DEFAULT '',
      first_name TEXT DEFAULT '',
      created_at TEXT NOT NULL,
      last_seen TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS plans(
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      days INTEGER NOT NULL,
      price REAL NOT NULL,
      active INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS keys(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      key TEXT UNIQUE NOT NULL,
      plan_days INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'unused',
      telegram_id INTEGER,
      activated_at TEXT,
      expires_at TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      order_id TEXT UNIQUE NOT NULL,
      telegram_id INTEGER NOT NULL,
      plan_days INTEGER NOT NULL,
      amount REAL NOT NULL,
      status TEXT NOT NULL DEFAULT 'Pending',
      txn_id TEXT DEFAULT '',
      utr TEXT DEFAULT '',
      payment_url TEXT DEFAULT '',
      created_at TEXT NOT NULL,
      paid_at TEXT
    );
    """)
    defaults = [(1,"1 Day",1,float(os.getenv("PRICE_1D","20"))),
                (7,"7 Days",7,float(os.getenv("PRICE_7D","99"))),
                (30,"30 Days",30,float(os.getenv("PRICE_30D","249")))]
    for days,name,d,price in [(x[0],x[1],x[2],x[3]) for x in defaults]:
        c.execute("INSERT OR IGNORE INTO plans(id,name,days,price) VALUES(?,?,?,?)",
                  (days,name,d,price))
    c.commit(); c.close()

def now():
    return datetime.now(timezone.utc)

def upsert_user(tg):
    c=conn(); t=now().isoformat()
    c.execute("""INSERT INTO users(telegram_id,username,first_name,created_at,last_seen)
                 VALUES(?,?,?,?,?)
                 ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,
                 first_name=excluded.first_name,last_seen=excluded.last_seen""",
              (tg.id,tg.username or "",tg.first_name or "",t,t))
    c.commit(); c.close()

def active_key(telegram_id):
    c=conn()
    r=c.execute("""SELECT * FROM keys WHERE telegram_id=? AND status='active'
                   ORDER BY expires_at DESC LIMIT 1""",(telegram_id,)).fetchone()
    c.close()
    if not r: return None
    if datetime.fromisoformat(r["expires_at"]) <= now():
        c=conn(); c.execute("UPDATE keys SET status='expired' WHERE id=?",(r["id"],)); c.commit(); c.close()
        return None
    return dict(r)

def create_key(days):
    key="TG-"+secrets.token_hex(8).upper()
    c=conn(); c.execute("""INSERT INTO keys(key,plan_days,status,created_at)
                           VALUES(?,?,?,?)""",(key,days,"unused",now().isoformat()))
    c.commit(); c.close()
    return key

def activate_key(key, telegram_id):
    c=conn(); r=c.execute("SELECT * FROM keys WHERE key=?",(key.strip(),)).fetchone()
    if not r: c.close(); return False,"❌ Invalid key."
    if r["status"]=="disabled": c.close(); return False,"🚫 This key has been disabled."
    if r["status"]=="active" and r["telegram_id"] != telegram_id:
        c.close(); return False,"🚫 This key is already bound to another user."
    if r["status"]=="active":
        c.close(); return True,"✅ Your key is already active."
    expires=now()+timedelta(days=r["plan_days"])
    c.execute("""UPDATE keys SET status='active',telegram_id=?,activated_at=?,expires_at=? WHERE id=?""",
              (telegram_id,now().isoformat(),expires.isoformat(),r["id"]))
    c.commit(); c.close()
    return True,f"✅ Key activated!\n\nPlan: {r['plan_days']} days\nExpires: {expires.strftime('%d %b %Y %H:%M UTC')}"

def disable_key(key):
    c=conn(); cur=c.execute("UPDATE keys SET status='disabled' WHERE key=? AND status!='disabled'",(key.strip(),))
    c.commit(); changed=cur.rowcount; c.close(); return changed>0

def all_users():
    c=conn(); rows=c.execute("SELECT telegram_id FROM users").fetchall(); c.close()
    return [r["telegram_id"] for r in rows]

def keys_page(limit=30):
    c=conn(); rows=c.execute("SELECT * FROM keys ORDER BY id DESC LIMIT ?",(limit,)).fetchall(); c.close()
    return [dict(r) for r in rows]

def get_plan(days):
    c=conn(); r=c.execute("SELECT * FROM plans WHERE days=? AND active=1",(days,)).fetchone(); c.close()
    return dict(r) if r else None

def save_order(order_id,tg_id,days,amount):
    c=conn(); c.execute("""INSERT INTO orders(order_id,telegram_id,plan_days,amount,created_at)
                           VALUES(?,?,?,?,?)""",(order_id,tg_id,days,amount,now().isoformat()))
    c.commit(); c.close()

def update_order(order_id, **kw):
    if not kw:return
    c=conn(); sets=", ".join(f"{k}=?" for k in kw); vals=list(kw.values())+[order_id]
    c.execute(f"UPDATE orders SET {sets} WHERE order_id=?",vals); c.commit(); c.close()

def get_order(order_id):
    c=conn(); r=c.execute("SELECT * FROM orders WHERE order_id=?",(order_id,)).fetchone(); c.close()
    return dict(r) if r else None

init_db()
