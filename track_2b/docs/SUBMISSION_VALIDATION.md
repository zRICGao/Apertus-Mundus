# Submission validation - 16 October 2026

The source, report and offline application flow were checked for the commit containing
this record. The submission remains under `track_2b/`; the root Makefile forwards
`make run` and `make package` to it.

## Verified

### Default local Apertus startup

- Root `make run` now selects the local Apertus v1.5 8B GPU stack, with CPU helpers.
  A one-shot preparation service reuses host `hf auth login` credentials or `HF_TOKEN`,
  downloads a pinned snapshot and completes before vLLM starts. The app waits for
  the generator and all helpers to become healthy. No inference key or `.env.docker`
  is needed. `make run-remote` retains optional external endpoint configuration.
- Nine deployment tests pass, including real Compose CLI configuration checks for
  local/remote modes and CPU/GPU helpers, Linux/Windows/custom login directories,
  credential isolation, interrupted downloads, revision changes and cached restarts.
  Downloads are mocked in these tests; no model weights are fetched.
- All 31 included Python regressions pass. The exported package validates all 114
  manifest hashes and includes the downloader and deployment tests. Its isolated
  mocked-inference smoke flow passes generation, selected review, expansion,
  idempotent commit, export/load and fresh-process recovery. Windows sandbox
  loopback restrictions blocked TestClient startup; that smoke check passed when
  rerun outside the sandbox. This does not exercise Docker or real models.
- The README, deployment guide, generated submission instructions and report now
  describe local default startup. Changed report pages were rendered and inspected;
  the report remains six body pages plus one reference page.
- Docker Desktop's Linux engine remains unavailable on this machine. A fresh image
  build, gated model download and full local GPU inference have not been validated.
  This change requires compatible NVIDIA Docker support and sufficient GPU memory;
  the existing 8 GB GPU has not passed that validation. Historical 70B quality and
  latency results do not establish local 8B performance.

### Limmat example and blank starter

- Added Limmat — The Forbidden River and Blank Campaign under `data/scenarios/`.
  The Limmat adaptation preserves all 16 named people and their original roles,
  with romanized display names and original-name aliases. It includes 27 scenes,
  19 locations, 22 passages, 26 triggers, eight typed state definitions and nine
  editable knowledge sources. Three authored endings exclude one another.
- Startup installs missing example folders without overwriting existing saves or
  changing the active world. Docker inputs and submission exports include both
  examples. The blank starter resets previous scenario data and leaves one empty
  editable opening scene.
- Twenty-two included Python tests pass (the full 21-test run plus the added
  scoped Campus Watch check), as do the three English frontend tests. All 259
  existing offline regressions pass against the updated isolated runtime copy.
  The generated 112-file manifest validates, and the exported package passes the
  isolated mocked-inference deployment smoke flow including fresh-process recovery.
- Checks cover every authored ending, exact costs, repeat commits, missing keys,
  conditional disclosure, map discovery versus entry, scoped hard navigation,
  capped recovery and cooldown, optional key sources, uncertain and confirmed
  semantic rules, irreversible deadlock, opt-in generation failure rollback,
  import/export, original-name aliases and non-overwriting installation.
- Real-model judgement/prose and interactive two-device behavior were not measured
  by these offline tests. The GM guide identifies the model-dependent rules and
  keeps the fictional timetable separate from real-time engine cooldowns.

### English language review

- Interface text, server messages, active generation prompts, deployment documentation
  and the bundled fictional demo are in English. Legacy identity and private-chat
  markers remain accepted for existing saves. The historical Chinese campaign
  benchmark in the report has not been remeasured with English content.
- Fixed English likelihood weighting, preserved multiword location aliases, corrected
  private-chat preview markers and enabled incremental output at English sentence ends.
- Six included Python regressions and three English frontend regressions pass, as do
  five frontend API tests and all three Vue template compilations. Python and JavaScript
  syntax checks and the isolated deployment smoke flow pass.
- All 259 existing offline regressions pass against an isolated runtime copy after
  updating localized output expectations to English and supplying the existing
  developer fixtures. Source-export validation verifies all 93 manifest hashes and
  confirms required report files, documentation links and tests are present.
- The report source required by the README links and packaging script is present.
  DESIGN.md, PRODUCT.md and the English regressions are included in source exports.
  The imported ZIP archive is excluded from this GitHub update at the user's request.
- Docker's Linux engine is unavailable on this machine. This language review does not
  establish a fresh container build, real-model quality or target-hardware performance.

### Earlier submission checks

- Default, local Apertus and GPU-helper Compose configurations parse successfully.
- Application Docker COPY inputs, Python syntax, frontend JavaScript syntax and
  README links are present and consistent with the submission layout.
- The isolated deployment smoke test passes health and static assets, bundled demo
  loading, provider-failure isolation, selected-branch preparation, expansion,
  idempotent commit, export/load and fresh-process database recovery. Inference is mocked.
- 259 existing offline regressions pass against an exported copy of the submitted
  application. Developer test helpers and fixtures were added only to the temporary
  verification copy. The Windows desktop launcher tests are outside this container
  submission check.
- Three included summary-contract regressions pass: the 100-character prompt target
  and 120-character server tolerance preserve text; 121 characters are rejected;
  legacy full-branch validation retains its original contract. The 120-character
  tolerance is intentional. No generated text is forcibly truncated.
- The frontend API test suite passes. The standalone report has six English body
  pages plus one reference page; changed pages were rendered and visually checked.
- Report metrics sum correctly. The package includes its editable report, PDF,
  generator, aggregate evidence ledger and included boundary tests.
- The lighthouse demo data totals 3,659 bytes, below the 100 MB limit. The source audit finds
  no runtime databases, private campaign directories, credential files or matches
  for the checked credential-token/private-key patterns.

The historical full regression/evaluation suite and private campaign are developer
artifacts. Only the standalone boundary tests and public demo smoke harness are
included in the public package. Their results do not establish model quality.

## Review changes

- Added the missing root `make package` forwarding, including generated exports.
- Preserved the existing 120-character summary tolerance and corrected its error
  message to say 120. Added direct boundary checks independent of provider errors.
- Replaced the placeholder report and included its source, generator and evidence
  ledger in re-exports. Updated the report to explain the intended summary tolerance.

## Outstanding submission and runtime validation

- The required demonstration video URL has not been supplied. The official Track 2b
  requirements specify a demonstration of at most two minutes. Individual team-member
  names also have not been supplied for the template metadata.
- Docker Desktop's Linux engine was not running during this review. Compose parsing
  passed, but no fresh container build or full concurrent real-model run was performed.
- Hosted retrieval measurements do not measure the submitted local Base v2 reranker;
  local helper quality and target-hardware resource needs still require validation.
- The private 48-case campaign benchmark cannot be rerun from the public demo alone.

The official checklist is available at
[HackApertus Track 2b](https://github.com/HackApertus/project-template/blob/main/track_2b/README.md).
