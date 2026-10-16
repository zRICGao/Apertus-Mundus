# Apertus Mundus - Track 2b

A persistent narrative roleplaying workbench built with Apertus. A game master can
explore candidate outcomes, expand a selected branch, and commit changes to a shared
world with teams, maps, character identities, scoped knowledge and versioned saves.

Team: Apertus Mundus.

## Quick start

Requirements: Docker Engine 28+, Docker Compose 2.24+ and GNU Make.

```sh
cp track_2b/.env.docker.example track_2b/.env.docker
# Set LLM_NAME, LLM_BASE_URL and LLM_API_KEY for your Apertus endpoint.
make run
```

Open http://localhost:8000/index.html. Initial builds download approximately 12.72 GB
of supporting model weights plus dependencies. Plan for 32 GB Docker RAM and at
least 40 GB free disk as a starting point; these are not measured minimums.
`make stop` preserves the saved world. `make smoke` checks isolated mocked inference;
`make smoke-live` exercises the configured real services.

The application stays in `track_2b/`, following the
[official template](https://github.com/HackApertus/project-template/blob/main/track_2b/README.md).
The root Makefile forwards commands to that directory.

## Architecture and scope

The target architecture is self-managed on-premise. `make prepare-model` and
`make run-local` provide a local Apertus v1.5 8B GPU deployment, subject to model
access approval and sufficient NVIDIA hardware. The default `make run` connects
to your Apertus endpoint. Local supporting models provide embeddings, reranking
and typed decisions; their roles are documented in the report. Build preparation
requires network access. Fully disconnected operation has not been validated.

The UI and bundled fictional demo are currently primarily Chinese. The READMEs
and technical report are English. Public data is an original CC0 demo and stays
below 100 MB; credentials, production saves and private campaigns are excluded.

## Documentation and submission status

- [Application README](track_2b/README.md)
- [Deployment guide](track_2b/docs/DOCKER.md)
- [Technical report source](track_2b/technical_report.md)
- [Technical report PDF](track_2b/Apertus_Mundus_Report.pdf)

Compose configuration, root command forwarding and the offline application smoke
flow pass. Full concurrent real-model deployment still requires target-hardware
validation. The required demo video has not yet been recorded.

Source code: [Apache-2.0](LICENSE). Model weights retain their upstream licences.
