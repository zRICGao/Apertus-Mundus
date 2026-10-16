"""Validate inline JavaScript using Node's parser."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

node = shutil.which('node')
if not node:
    raise SystemExit('JavaScript validation requires Node.js.')
scripts = [Path(__file__).with_name(name).read_text(encoding='utf-8')
           for name in ('frontend_api.js', 'frontend_ui.js', 'frontend_sync.js', 'frontend_npc_review.js')]
for name in ('index.html', 'phone.html', 'connect.html'):
    html = Path(__file__).with_name(name).read_text(encoding='utf-8')
    scripts.extend(re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', html, re.S | re.I))
for source in scripts:
    with tempfile.TemporaryDirectory(prefix='apertus_js_') as directory:
        path = Path(directory) / 'inline.js'
        path.write_text(source, encoding='utf-8')
        subprocess.run([node, '--check', str(path)], check=True)
print('Inline JavaScript syntax passed.')
