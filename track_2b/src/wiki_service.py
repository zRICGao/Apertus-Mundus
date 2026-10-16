"""Bounded automatic Wiki organization after a confirmed world change."""
import os
import threading
from database import connect_db
from world import refresh_wiki, synthesize_wiki
from logger import get_logger

_lock=threading.Lock()
_log=get_logger('wiki')


def maintain_wiki(db_file):
    if os.getenv('WIKI_AUTO_SYNTHESIS','true').lower()!='true' or not _lock.acquire(blocking=False):
        return
    try:
        from wiki_compiler import run_one
        run_one(db_file)
        return True
    finally:
        _lock.release()
