# Submission validation - 16 October 2026

The source, report and offline application flow were checked for the commit containing
this record. The submission remains under `track_2b/`; the root Makefile forwards
`make run` and `make package` to it.

## Verified

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
- Public data totals 2,848 bytes, below the 100 MB limit. The tracked-file audit finds
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
