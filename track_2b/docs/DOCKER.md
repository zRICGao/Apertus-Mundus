# Docker deployment and submission

`make run` automatically prepares **Apertus v1.5 8B**, starts its local GPU server,
then starts **Apertus Mundus, BGE-M3, MXBAI Rerank Base v2 and Kev-4B**. No inference
API key, `.env.docker` file or manual endpoint configuration is required. A host
Hugging Face login is needed for the initial gated weight download.

## Start the application

Requirements: Docker Engine 28+, Docker Compose 2.24+, GNU Make, NVIDIA Container
Toolkit and compatible NVIDIA drivers / GPU with sufficient VRAM for Apertus
v1.5 8B. Only the helpers use the CPU by default. The helpers' 32 GB Docker RAM
and 40 GB disk estimates exclude Apertus weights and its serving image. Allow
additional RAM, disk and GPU capacity for the generator; full-stack minimum
requirements and end-to-end latency still need measurement on the evaluation machine.

Before the first run, accept the access conditions at
[the Apertus model page](https://huggingface.co/swiss-ai/Apertus-v1.5-8B) and run
`hf auth login` on the host with a read token from the same account. **That account
must already have been granted access to the Apertus model on Hugging Face.**
If already logged in with the Hugging Face CLI, skip this step. Browser login
alone does not provide download credentials to Docker. The application itself
does not require host Python.

Run from the repository root or `track_2b/`:

```sh
make run
```

The one-shot download container reads the host login directory through a read-only
mount. It resolves `HOST_HF_HOME`, then `HF_HOME`, then `$XDG_CACHE_HOME/huggingface`,
then `$HOME/.cache/huggingface` (Windows: `%USERPROFILE%/.cache/huggingface`). If your
login uses a custom `HF_TOKEN_PATH`, set `HOST_HF_HOME` to its parent directory when
the file is named `token`, or export `HF_TOKEN`. An exported `HF_TOKEN` overrides
the file. This is a download credential, not an inference API key. It is provided
only to the preparation container, never to the app or inference server, and is
not copied into the model-cache volume or source repository.

Preparation downloads the complete pinned snapshot into `model-cache`. Only a
successful full download writes the readiness marker, so an interrupted first
download is retried on the next `make run`. Later starts use the completed cache
without a Hub request or token. The vLLM server loads the same revision offline
from a read-only mount. Compose waits for preparation to finish successfully,
then for the generator and all three helpers to become healthy, before starting
the app. `make run-local` is an alias; a separate `make prepare-model` is optional.

Open <http://localhost:8000/index.html> once startup completes. The initial build
and preparation download dependencies and model weights; later runs reuse the
images and model cache. `make stop` stops containers and preserves saves; `make logs`
shows logs. Set `APP_PORT=8001` before `make run` if the default port is occupied.

`.env.docker` is excluded from Git and the image build context. Helper services are
available only inside the Docker network; the host publishes `127.0.0.1:<APP_PORT>`.
This mode provides a local workbench. Phone pairing uses the desktop launch mode.

## Models running locally

| Service | Pinned model | Purpose |
| --- | --- | --- |
| embedding | `BAAI/bge-m3` | 1024-dimensional dense embeddings |
| reranker | `mixedbread-ai/mxbai-rerank-base-v2` | Rerank candidate passages |
| kev | `jaredpalmer/kev-4b` + `Qwen/Qwen3.5-4B-Base` | System One structured decisions |
| llm | `swiss-ai/Apertus-v1.5-8B` | Local narrative, summaries, expansion, Wiki and decision fallback |

Kev is an independent open-source model, distinct from the Jev weights hosted by
TypeSafe. The default `DECISION_PROVIDER=kev` falls back directly to the main
Apertus model on uncertainty or service errors, without requiring a second Apertus
8B endpoint. Existing `apertus`, `apertus-8b` and `jev` decision modes remain supported.

With sufficient NVIDIA VRAM, `make run-gpu` also builds CUDA images for all three
helper services, alongside local Apertus. Shared VRAM must hold all models and
inference caches; the current 8 GB GPU has not passed validation for this configuration. The existing
configuration fields `JEV_URL` and `JEV_MODEL` also configure Kev.

Local Base v2 differs from the previously hosted `mxbai-rerank-v3.1-listwise` model;
hosted benchmark results do not establish local model quality. Kev was primarily
validated upstream in English; decisions on other campaign languages need separate
evaluation. English demo results also require their own model-quality measurements.

The helper services do not need real keys. The `local` value only satisfies the
OpenAI client's non-empty key requirement. Model revisions are recorded in
`helpers/models.json`; Kev source is pinned to commit
`1f04cfa28e6a708485b28a9585010b9e436b776e`. Model loading is offline at runtime;
build preparation requires network access.

## Weights and submission size

The pinned public Hub file metadata checked on 16 October 2026 totals
**12,716,349,435 bytes**, approximately **12.72 GB / 11.84 GiB**: BGE-M3 2.27 GB,
Base v2 0.99 GB, Kev adapter and heads 0.135 GB, and Qwen base 9.32 GB. Downloads
also include tokenizers and configuration; images include PyTorch and other
libraries. A 15-20 GB helper image estimate is approximate; measure the actual build
and avoid counting shared layers twice. These totals exclude the local Apertus
weights, its official vLLM image and runtime caches.

```sh
python helpers/model_inventory.py
docker image ls
```

Submit the source repository URL. Docker downloads helper weights during the build
and Apertus during automatic preparation;
weights belong outside Git and `data/`. Bundled data includes the original
fictional lighthouse demo, an English Limmat adventure and a blank starter,
and stays well below 100 MB.

## Data and settings

The named `app-data` volume stores the database, campaigns, exported saves, settings
and reports. On the first empty startup, the app loads the Mist Harbor Lighthouse
demo. Restarting preserves the existing world. Exported saves can be loaded again.
The [Limmat example and blank campaign](../data/scenarios/README.md) are installed
as selectable folders on startup without replacing an existing campaign folder
or the active world. Existing modified examples are preserved.

Configuration precedence is process environment > persisted settings > project
`.env` > defaults. Within one layer, `LLM_*` takes precedence over the corresponding
`APERTUS_*`; an empty key is valid. Explicit Compose values override UI settings
on restart. Local mode fixes the generator to `http://llm:8000/v1`; use `LOCAL_LLM_*`
overrides for that service. Remote mode takes its endpoint from `.env.docker` or
the process environment. Persisted settings live at `/app/runtime/settings.env`.

The initial demo builds a vector index through local BGE-M3. After changing the
embedding service for existing sources, use `POST /api/rag/reindex` rather than
mixing vectors produced by different services.

## Optional external Apertus endpoint

```sh
cp track_2b/.env.docker.example track_2b/.env.docker
# Fill in LLM_NAME, LLM_BASE_URL and LLM_API_KEY for your external service.
make run-remote
```

Run these commands from the repository root. In PowerShell, use `Copy-Item` in
place of `cp`. You may instead export the three `LLM_*` values. An unauthenticated
endpoint may use an explicitly empty `LLM_API_KEY`. This mode starts the app and
CPU helpers without a local Apertus server, NVIDIA runtime or Hugging Face login.
`make run-remote-gpu` enables GPU helpers while retaining the external generator.

For default local mode, available overrides include `LOCAL_LLM_IMAGE`,
`LOCAL_LLM_NAME`, `LOCAL_MODEL_REVISION`, `LOCAL_MAX_MODEL_LEN` and
`LOCAL_GPU_MEMORY_UTILIZATION`. Defaults are a 16K context, one concurrent request
and thinking disabled. The model revision and official amd64 image digest are
pinned. The current 8 GB GPU has not completed validation; 8 GB operation is not
guaranteed. The frontend still requests Google Fonts, CDN icons and optional
online background music. This is an on-premise deployment; fully disconnected
operation still needs local browser assets and verification of network egress.

## Validation

```sh
make smoke       # Temporary world and mocked inference; no real models.
make smoke-live  # Temporary world and real services; remote mode may consume quota.
```

Both modes use an isolated world. Live mode first checks embedding, reranking and
Kev inference, then exercises summaries, selected-branch review, expansion, commit,
repeat commit, export/load and persistence in a new process. `/api/health` checks
only the application and database, without invoking models.

## Build the Track 2b submission

On a development machine with Python, run `make package`. This creates
`dist/submission-<UTC timestamp>/` with a root forwarding Makefile and
`track_2b/{src,data,docs,Makefile,technical_report.md}`. Run `make run` from either
the generated root or its `track_2b/` directory. No configuration file is required
for default local mode; optional `.env.docker` overrides belong in `track_2b/`.
Packaging excludes private campaigns, databases, credentials and model weights,
preserves existing output directories and generates a SHA-256 manifest.

Before final competition submission, provide team-member names, current test
results and a demonstration video link of at most two minutes. Keep the report
within six body pages and use the official template for the public repository.

Project references: [competition template](https://github.com/HackApertus/project-template),
[BGE-M3](https://huggingface.co/BAAI/bge-m3),
[MXBAI Base v2](https://huggingface.co/mixedbread-ai/mxbai-rerank-base-v2),
[Kev](https://huggingface.co/jaredpalmer/kev-4b),
[Kev source](https://github.com/jaredpalmer/kev),
[Apertus](https://huggingface.co/swiss-ai/Apertus-v1.5-8B).
