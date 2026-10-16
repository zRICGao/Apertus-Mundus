"""Download one pinned Apertus snapshot, reusing the host's Hugging Face login."""
import hashlib
import os
from pathlib import Path
import sys


class PreparationError(RuntimeError):
    pass


def prepare_model(download, environ=None):
    env = os.environ if environ is None else environ
    model = env['MODEL_ID']
    revision = env['MODEL_REVISION']
    home = Path(env['HF_HOME'])
    cache = home / 'hub'
    key = hashlib.sha256(f'{model}@{revision}'.encode()).hexdigest()
    marker = home / '.apertus-ready' / key

    # Only trust snapshots for which our full download completed. A partially
    # downloaded Hub snapshot can exist and still be missing weight shards.
    if marker.is_file():
        try:
            snapshot = download(repo_id=model, revision=revision,
                                cache_dir=str(cache), local_files_only=True)
            if Path(snapshot).is_dir():
                print('Apertus weights already prepared; using the local cache.', flush=True)
                return
        except OSError:
            pass
        marker.unlink(missing_ok=True)

    token = (env.get('HF_TOKEN') or '').strip()
    if not token:
        token_file = Path(env.get('HOST_HF_TOKEN_FILE', '/host-huggingface/token'))
        try:
            token = token_file.read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            pass
        except OSError:
            raise PreparationError('Cannot read the host Hugging Face login. Check HOST_HF_HOME permissions.') from None
    if not token:
        raise PreparationError(
            'No Hugging Face download credentials. Accept the Apertus model access '
            'conditions and run hf auth login on the host, or export HF_TOKEN. '
            'Browser login alone does not log in Docker. No inference API key is needed.')

    print(f'Preparing {model} at revision {revision}; the first download may take a while.', flush=True)
    snapshot = download(repo_id=model, revision=revision, cache_dir=str(cache), token=token)
    if not Path(snapshot).is_dir():
        raise PreparationError('The downloaded Apertus snapshot is unavailable; retry make run.')
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(revision + '\n', encoding='utf-8')
    print('Apertus weights prepared. Starting the local inference server.', flush=True)


def main():
    from huggingface_hub import snapshot_download
    try:
        prepare_model(snapshot_download)
    except PreparationError as exc:
        print(f'Model preparation failed: {exc}', file=sys.stderr)
        return 2
    except Exception as exc:
        # Upstream exception bodies may include request headers or credentials.
        print(f'Model preparation failed ({type(exc).__name__}). Check model access, '
              'hf auth login / HF_TOKEN, network connectivity and free disk space; '
              'then retry make run.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
