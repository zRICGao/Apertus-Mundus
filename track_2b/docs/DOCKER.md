# Docker deployment and submission

`make run` starts **Apertus Mundus, BGE-M3, MXBAI Rerank Base v2 and Kev-4B**.
Apertus uses the OpenAI-compatible endpoint supplied by the judges. The three helper
models load weights from their images; no SiliconFlow, Mixedbread, TypeSafe or
Hugging Face API keys are required for those services.

## Start the application

Requirements: Docker Engine 28+, Docker Compose 2.24+ and GNU Make. The host does
not need Python. Inference uses the CPU by default. Allow 32 GB of Docker RAM and
40 GB of free disk as starting capacity estimates. Minimum hardware requirements
and end-to-end latency still need measurement on the evaluation machine.

Run from `track_2b/`:

```sh
cp .env.docker.example .env.docker
# Fill in the supplied LLM_NAME, LLM_BASE_URL and LLM_API_KEY in .env.docker.
make run
```

In PowerShell, use `Copy-Item .env.docker.example .env.docker`. You may also provide
`LLM_NAME`, `LLM_BASE_URL` and `LLM_API_KEY` through the process environment.
An Apertus endpoint without authentication may use an explicitly empty `LLM_API_KEY`.

Open <http://localhost:8000/index.html>. The initial build downloads dependencies
and public model weights; later builds reuse Docker layers. Startup waits for all
three helper models. `make stop` stops containers and preserves saves; `make logs`
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
| app | Apertus specified by `LLM_NAME` | Narrative, summaries, expansion, Wiki and decision fallback |

Kev is an independent open-source model, distinct from the Jev weights hosted by
TypeSafe. The default `DECISION_PROVIDER=kev` falls back directly to the main
Apertus model on uncertainty or service errors, without requiring a second Apertus
8B endpoint. Existing `apertus`, `apertus-8b` and `jev` decision modes remain supported.

With sufficient NVIDIA VRAM, `make run-gpu` builds CUDA images for all three helper
services while still connecting to remote Apertus. `make run-local` additionally
runs Apertus locally. Shared VRAM must hold all models and inference caches; the
current 8 GB GPU has not passed validation for this configuration. The existing
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
libraries. A 15-20 GB image estimate is approximate; measure the actual build and
avoid counting shared layers twice.

```sh
python helpers/model_inventory.py
docker image ls
```

Submit the source repository URL. Docker downloads weights during the build;
weights belong outside Git and `data/`. Public data contains only the original
fictional demo and stays well below 100 MB.

## Data and settings

The named `app-data` volume stores the database, campaigns, exported saves, settings
and reports. On the first empty startup, the app loads the Mist Harbor Lighthouse
demo. Restarting preserves the existing world. Exported saves can be loaded again.

Configuration precedence is process environment > persisted settings > project
`.env` > defaults. Within one layer, `LLM_*` takes precedence over the corresponding
`APERTUS_*`; an empty key is valid. Explicit Compose values override UI settings
on restart, so update `.env.docker` to change deployed connections. Persisted
settings live at `/app/runtime/settings.env`.

The initial demo builds a vector index through local BGE-M3. After changing the
embedding service for existing sources, use `POST /api/rag/reindex` rather than
mixing vectors produced by different services.

## Optional local Apertus

```sh
# Accept the Apertus v1.5 Hugging Face access conditions and set HF_TOKEN locally.
make prepare-model
make run-local
```

This adds an official Apertus v1.5 8B GPU container; helpers still use the CPU.
It requires NVIDIA Container Toolkit, matching drivers and sufficient VRAM.
Defaults are a 16K context, one concurrent request and thinking disabled. The
current 8 GB GPU has not completed validation; 8 GB operation is not guaranteed.

`HF_TOKEN` is passed only to the download container. The server reads the
`model-cache` volume. Available settings include `LOCAL_LLM_IMAGE`, `LOCAL_LLM_NAME`,
`LOCAL_MAX_MODEL_LEN` and `LOCAL_GPU_MEMORY_UTILIZATION`. Model revisions and the
official amd64 image digest are pinned. Fully disconnected operation still needs
verification of browser assets and network egress.

## Validation

```sh
make smoke       # Temporary world and mocked inference; no real models.
make smoke-live  # Temporary world and real services; may consume Apertus quota.
```

Both modes use an isolated world. Live mode first checks embedding, reranking and
Kev inference, then exercises summaries, selected-branch review, expansion, commit,
repeat commit, export/load and persistence in a new process. `/api/health` checks
only the application and database, without invoking models.

## Build the Track 2b submission

On a development machine with Python, run `make package`. This creates
`dist/submission-<UTC timestamp>/` with a root forwarding Makefile and
`track_2b/{src,data,docs,Makefile,technical_report.md}`. Run `make run` from either
the generated root or its `track_2b/` directory; place `.env.docker` in `track_2b/`.
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
