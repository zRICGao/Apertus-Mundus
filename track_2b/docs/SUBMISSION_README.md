# Apertus Mundus — Track 2b: Own Project

Team: Apertus Mundus. Demo video: not yet recorded.

A persistent narrative roleplaying workbench built with Apertus. The GM can generate
candidate outcomes, expand a selected candidate, and commit its changes to one shared
world with teams, maps, character identities, scoped knowledge and versioned saves.

## Run

Install Docker Engine 28+, Compose 2.24+, GNU Make and NVIDIA Container Toolkit
with a compatible NVIDIA GPU and sufficient VRAM for Apertus v1.5 8B.
**Hugging Face access must already be granted** to the downloading account for
[Apertus v1.5 8B](https://huggingface.co/swiss-ai/Apertus-v1.5-8B). Accept the model's
access conditions on that page, then run `hf auth login` on the host with a read
token from the same account; skip login if already authenticated. Browser login
alone is insufficient, and the token must never be committed to GitHub.
Then run from the repository root or `track_2b/`:

```sh
make run
```

This automatically prepares Apertus v1.5 8B weights, starts its GPU server and
three local CPU helpers, then starts the app after model health checks pass.
No `.env.docker` or inference API key is required. Downloads reuse the host Hugging
Face login; browser login alone is insufficient. Open http://localhost:8000/index.html
once startup completes. Initial downloads include Apertus weights plus about 12.72 GB
of helper weights and dependencies. The helpers' 32 GB RAM / 40 GB disk allowances
exclude Apertus and its serving image; full-stack hardware minima remain unmeasured.

Full instructions, optional external endpoint mode, tests, storage and limitations:
[Docker guide](docs/DOCKER.md). Architecture and evaluation: [technical report](technical_report.md).

## Target architecture and validation

The default is self-managed on-premise deployment with local Apertus v1.5 8B.
`make run-local` is an alias for `make run`; `make prepare-model` optionally downloads
weights in advance. `make run-remote` supports an external Apertus service configured
with `LLM_NAME`, `LLM_BASE_URL` and `LLM_API_KEY`. Build and initial preparation require
internet access. The frontend still requests Google Fonts, CDN icons and optional
online background music. This is an on-premise deployment; a fully disconnected,
air-gapped runtime has not been validated.

The interface, bundled demo and submission documentation are in English. Docker Compose configuration and the
isolated offline application smoke test are checked for this layout; full concurrent
model inference on the target hardware still needs validation. Run `make smoke`
for isolated mocked inference or `make smoke-live` for real configured services.

The PDF report is [Apertus_Mundus_Report.pdf](Apertus_Mundus_Report.pdf).
The demo video remains an outstanding submission deliverable.

The latest [submission validation](docs/SUBMISSION_VALIDATION.md) records the checks
and remaining gaps. Run the included summary-boundary regressions from the repository
root with `python -m unittest discover -s track_2b/tests -v`.
Run the English frontend regressions with `node --test track_2b/tests/test_english_frontend.cjs`.

The `data/` folder contains the original CC0 lighthouse demo and the
[English Limmat example and blank starter](data/scenarios/README.md). The Limmat
adaptation retains the characters and roles from the user-supplied story.
Private saves and credentials are excluded. Model licences are retained with the downloaded
weights. The source layout follows the
[official Track 2b template](https://github.com/HackApertus/project-template/tree/main/track_2b).
