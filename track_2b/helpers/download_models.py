"""Bake only inference files for a pinned public model into the helper image."""
import json
from pathlib import Path
import sys

from huggingface_hub import snapshot_download


def download(role):
    models = json.loads(Path(__file__).with_name('models.json').read_text())
    names = [role, 'kev_base'] if role == 'kev' else [role]
    resolved = {}
    for name in names:
        model = models[name]
        resolved[name] = snapshot_download(
            model['repo'], revision=model['revision'], token=False,
            allow_patterns=['*.json', '*.safetensors', '*.bin', '*.pt', '*.txt', '*.model',
                            '*.jinja', '*.tiktoken', 'merges.txt', 'vocab.*', 'LICENSE*', 'README.md'],
            ignore_patterns=['onnx/**', 'openvino/**', 'imgs/**', 'assets/**', '*.onnx'])
    Path('/opt/models/paths.json').write_text(json.dumps(resolved))
    print(f'Public {role} weights cached; runtime requires no model download or API key.')


if __name__ == '__main__':
    download(sys.argv[1])
