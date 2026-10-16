"""Headless Docker entrypoint. Imports the app only after validating configuration."""
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def initialize_demo(app):
    from campaigns import load_save
    from database import atomic

    marker = Path(app.DB_FILE).parent / '.demo-initialized'
    if marker.exists():
        return
    with app.safe_db() as conn:
        empty = not conn.execute('SELECT 1 FROM nodes LIMIT 1').fetchone()
        empty = empty and not conn.execute('SELECT 1 FROM characters LIMIT 1').fetchone()
    # Existing worlds are authoritative, including a database supplied by an operator.
    if empty:
        target = Path(app.CAMPAIGNS_DIR) / 'demo'
        if not target.exists():
            demo_root = ROOT if (ROOT / 'data' / 'demo').exists() else ROOT.parent
            shutil.copytree(demo_root / 'data' / 'demo', target)
        load_save(app.DB_FILE, app.BASE_DIR, 'campaigns/demo', campaigns_dir=app.CAMPAIGNS_DIR)
        with app.safe_db() as conn, atomic(conn):
            conn.execute("UPDATE system_state SET value='1' WHERE key='player_current_scene_id'")
        # Only the bundled public demo is indexed automatically, never an existing world.
        from rag import rag_reindex
        try:
            rag_reindex()
        except Exception:
            print('Demo vector indexing unavailable; lexical retrieval remains active.', flush=True)
    marker.touch()


def main():
    from runtime_config import load_environment, validate_container_settings
    from providers import Settings
    load_environment(ROOT)
    try:
        validate_container_settings(Settings.from_env())
    except ValueError as exc:
        print(f'Configuration error: {exc}', file=sys.stderr)
        return 2
    import main as app
    initialize_demo(app)
    import uvicorn
    print('Apertus Mundus: open http://localhost:<published port>/index.html', flush=True)
    uvicorn.run(app.app, host='0.0.0.0', port=8000, proxy_headers=False, workers=1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

