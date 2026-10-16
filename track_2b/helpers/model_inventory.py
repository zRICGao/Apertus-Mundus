"""Public HF weight-size inventory for the exact revisions used by Docker builds."""
import json
from pathlib import Path
import urllib.request


def inventory():
    models = json.loads(Path(__file__).with_name('models.json').read_text())
    rows = []
    for role, spec in models.items():
        url = f'https://huggingface.co/api/models/{spec["repo"]}/revision/{spec["revision"]}?blobs=true'
        with urllib.request.urlopen(url, timeout=60) as response:
            data = json.load(response)
        files = [file for file in data['siblings'] if '/' not in file['rfilename']
                 and file['rfilename'].endswith(('.safetensors', '.bin', '.pt'))]
        size = sum(file.get('size', (file.get('lfs') or {}).get('size', 0)) for file in files)
        rows.append({'role': role, **spec, 'weight_bytes': size, 'gated': data.get('gated', False)})
    return {'models': rows, 'total_weight_bytes': sum(row['weight_bytes'] for row in rows)}


if __name__ == '__main__':
    print(json.dumps(inventory(), indent=2))
