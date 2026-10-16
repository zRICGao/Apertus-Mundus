"""Durable post-commit Wiki compilation; source facts remain in the event transaction."""
import os
import threading
import time
from database import connect_db, atomic
from contextlib import closing

_worker=None

def enqueue(conn,event_id):
    event=conn.execute('SELECT event_uid FROM world_events WHERE id=?',(event_id,)).fetchone()
    conn.execute('INSERT OR IGNORE INTO wiki_jobs(event_uid,event_id) VALUES(?,?)',(event[0],event_id))

def run_one(db_file):
    from wiki_service import maintain_wiki
    with closing(connect_db(db_file)) as conn:
        with atomic(conn):
            job=conn.execute("SELECT * FROM wiki_jobs WHERE status='pending' AND next_attempt<=? ORDER BY event_id LIMIT 1",(time.time(),)).fetchone()
            if not job: return False
            if not conn.execute('SELECT id FROM world_events WHERE event_uid=? AND valid=1',(job['event_uid'],)).fetchone():
                conn.execute("UPDATE wiki_jobs SET status='obsolete' WHERE event_uid=?",(job['event_uid'],))
                return True
            conn.execute("UPDATE wiki_jobs SET status='running',attempts=attempts+1 WHERE event_uid=?",(job['event_uid'],))
        try:
            if not maintain_wiki(db_file): raise RuntimeError('Compiler unavailable or source changed')
        except Exception as exc:
            with atomic(conn):
                conn.execute("UPDATE wiki_jobs SET status='pending',next_attempt=?,last_error=? WHERE event_uid=?",
                             (time.time()+min(300,2**min(job['attempts']+1,8)),type(exc).__name__,job['event_uid']))
        else:
            with atomic(conn): conn.execute("UPDATE wiki_jobs SET status='done',last_error='' WHERE event_uid=?",(job['event_uid'],))
    return True

def start_worker(db_file):
    global _worker
    if os.getenv('WIKI_AUTO_SYNTHESIS','true').lower()!='true' or (_worker and _worker.is_alive()): return
    with closing(connect_db(db_file)) as conn, atomic(conn):
        conn.execute("UPDATE wiki_jobs SET status='pending' WHERE status='running'")
    def work():
        while True:
            try:
                from wiki_compiler import run_one as compile_one
                busy=compile_one(db_file)
                if not busy: busy=run_one(db_file)
            except Exception: busy=False
            time.sleep(1 if busy else 5)
    _worker=threading.Thread(target=work,daemon=True,name='wiki-compiler')
    _worker.start()
