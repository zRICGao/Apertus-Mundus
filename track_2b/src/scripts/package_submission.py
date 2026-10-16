"""Build a clean Track 2b submission tree from explicit public source categories."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

SOURCE = Path(__file__).resolve().parents[1]
PROJECT = SOURCE.parent if SOURCE.name == 'src' and (SOURCE.parent / 'compose.yaml').exists() else SOURCE


def copy(source, target):
    if source.is_symlink():
        raise ValueError(f'Symlinks are not packaged: {source.name}')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def package(destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError('Output already exists; choose a new --output directory. Existing files are never removed.')
    if destination == PROJECT or PROJECT.is_relative_to(destination):
        raise ValueError('Output must not contain the source project.')
    track = destination / 'track_2b'
    src = track / 'src'
    # Runtime modules and frontend assets only. No broad recursive project copy.
    for pattern in ('*.py', '*.html', '*.css', '*.js'):
        for file in sorted(SOURCE.glob(pattern)):
            copy(file, src / file.name)
    copy(SOURCE / 'requirements.txt', src / 'requirements.txt')
    for name in ('DESIGN.md', 'PRODUCT.md'):
        copy(SOURCE / name, src / name)
    for file in sorted((SOURCE / 'frontend_vendor').iterdir()):
        if file.suffix in {'.js', '.md'}:
            copy(file, src / 'frontend_vendor' / file.name)
    for file in sorted((SOURCE / 'scripts').glob('*.py')):
        copy(file, src / 'scripts' / file.name)
    for file in sorted((PROJECT / 'data' / 'demo').rglob('*')):
        if file.is_file():
            copy(file, track / 'data' / 'demo' / file.relative_to(PROJECT / 'data' / 'demo'))
    if sum(file.stat().st_size for file in (track / 'data').rglob('*') if file.is_file()) > 100_000_000:
        raise ValueError('Submission data exceeds 100 MB.')
    for name in ('compose.yaml', 'compose.local.yaml', 'compose.helpers-gpu.yaml', '.env.docker.example'):
        copy(PROJECT / name, track / name)
    for file in sorted((PROJECT / 'helpers').iterdir()):
        if file.suffix in {'.py', '.json', '.txt', '.md'} or file.name == 'Dockerfile':
            copy(file, track / 'helpers' / file.name)
    dockerfile = (PROJECT / 'Dockerfile').read_text(encoding='utf-8')
    if SOURCE == PROJECT:
        dockerfile = (dockerfile.replace('COPY requirements.txt', 'COPY src/requirements.txt')
                      .replace('COPY *.py *.html *.css *.js', 'COPY src/*.py src/*.html src/*.css src/*.js')
                      .replace('COPY frontend_vendor/', 'COPY src/frontend_vendor/')
                      .replace('COPY scripts/', 'COPY src/scripts/'))
    (track / 'Dockerfile').write_text(dockerfile, encoding='utf-8')
    (track / '.dockerignore').write_text(
        '*\n!Dockerfile\n!src/\n!src/*.py\n!src/*.html\n!src/*.css\n!src/*.js\n!src/requirements.txt\n'
        '!src/frontend_vendor/\n!src/frontend_vendor/**\n!src/scripts/\n!src/scripts/*.py\n'
        '!data/\n!data/demo/\n!data/demo/**\n!helpers/\n!helpers/**\n**/__pycache__\n**/*.pyc\n', encoding='utf-8')
    makefile = (PROJECT / 'Makefile').read_text(encoding='utf-8')
    if SOURCE == PROJECT:
        makefile = makefile.replace('$(PYTHON) scripts/package_submission.py', '$(PYTHON) src/scripts/package_submission.py')
    (track / 'Makefile').write_text(makefile, encoding='utf-8')
    copy(PROJECT / 'docs' / 'SUBMISSION_README.md', track / 'README.md')
    copy(PROJECT / 'technical_report.md', track / 'technical_report.md')
    for report in sorted(PROJECT.glob('*_Report.pdf')):
        copy(report, track / report.name)
    license_file = PROJECT / 'LICENSE'
    if not license_file.exists():
        license_file = PROJECT.parent / 'LICENSE'
    if license_file.exists():
        copy(license_file, destination / 'LICENSE')
    copy(PROJECT / 'docs' / 'DOCKER.md', track / 'docs' / 'DOCKER.md')
    for name in ('report_metrics.json', 'build_technical_report.py', 'SUBMISSION_VALIDATION.md'):
        source = PROJECT / 'docs' / name
        if source.is_file():
            copy(source, track / 'docs' / name)
    # Keep packaging inputs available for a reproducible re-export.
    copy(PROJECT / 'docs' / 'SUBMISSION_README.md', track / 'docs' / 'SUBMISSION_README.md')
    for pattern in ('test_*.py', 'test_*.cjs'):
        for file in sorted((PROJECT / 'tests').glob(pattern)):
            copy(file, track / 'tests' / file.name)
    (destination / 'README.md').write_text(
        '# Apertus Mundus — Track 2b\n\n'
        'Submission layout follows https://github.com/HackApertus/project-template .\n'
        'Application root: `track_2b/`. Configure `track_2b/.env.docker` using its example, '
        'then run `make run` from either this directory or `track_2b/`. '
        'See [run instructions](track_2b/docs/DOCKER.md) and [technical report](track_2b/technical_report.md).\n',
        encoding='utf-8')
    (destination / 'Makefile').write_text(
        '.PHONY: run run-gpu run-local prepare-model stop logs smoke smoke-live package\n'
        'run run-gpu run-local prepare-model stop logs smoke smoke-live package:\n\t$(MAKE) -C track_2b $@\n', encoding='utf-8')
    (destination / '.gitignore').write_text(
        '.env\n.env.docker\n*.db\n*.db-wal\n*.db-shm\n__pycache__/\n*.pyc\ndist/\n', encoding='utf-8')
    manifest = {}
    for file in sorted(destination.rglob('*')):
        if file.is_file():
            manifest[file.relative_to(destination).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=PROJECT / 'dist' / ('submission-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')))
    args = parser.parse_args()
    try:
        print(package(args.output))
    except ValueError as exc:
        parser.exit(2, f'{exc}\n')
