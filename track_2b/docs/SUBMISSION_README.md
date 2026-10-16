# Apertus Mundus — Track 2b: Own Project

Team: Apertus Mundus. Demo video: not yet recorded.

A persistent narrative roleplaying workbench built with Apertus. The GM can generate
candidate outcomes, expand a selected candidate, and commit its changes to one shared
world with teams, maps, character identities, scoped knowledge and versioned saves.

## Run

Provide the judges' Apertus service through `LLM_NAME`, `LLM_BASE_URL`, and `LLM_API_KEY`,
or copy `.env.docker.example` to `.env.docker` and fill them in. Run:

```sh
make run
```

This builds and starts the application plus local BGE-M3, MXBAI Rerank Base v2 and
Kev-4B containers. No paid helper inference APIs are required. Initial build downloads
approximately 12.72 GB of public weights, plus dependencies. Allocate sufficient RAM
to Docker (32 GB is the initial CPU deployment recommendation, not a measured minimum).
Open http://localhost:8000/index.html .

Full instructions, optional local Apertus GPU mode, tests, storage and limitations:
[Docker guide](docs/DOCKER.md). Architecture and evaluation: [technical report](technical_report.md).

## Target architecture and validation

The target is self-managed on-premise deployment. `make prepare-model` downloads
the gated Apertus weights after you accept their access conditions; `make run-local`
then serves Apertus v1.5 8B on an NVIDIA GPU. Build and preparation require internet
access. A fully disconnected runtime has not been validated. The default `make run`
uses the supplied Apertus endpoint and local supporting models.

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
