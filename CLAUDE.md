# ev-journal — rules for agents

## Privacy: the code is public, the data is private

- Never put places, coordinates, place names, station names, routes, GPS
  tracks, receipts, prices paid, serials, IP addresses, tailnet names or links
  to private repos into code, tests, commits, PRs, issues or comments.
- Tests and fixtures use made-up places within 1° of 0° N 0° E (open sea).
  `tests/test_privacy_guard.py` fails CI on any coordinate outside that area.
- Settings that carry data (places seed, tokens, passwords) come from the
  environment or from files it points to. Never from the repo.
- Results of runs against real data go into PRs only as counts and
  percentages.

## How to work

- Each change starts from a GitHub issue. Make the smallest change that meets
  its "Done when". Write extra ideas in the PR or a new issue; do not build them.
- Work in a branch, open a PR, get a review, squash-merge when CI is green.
- Tests use made-up data. Run `pytest` and `ruff check . && ruff format --check .`.
- The design and its reasons live in `docs/design.md`. Keep it in step with
  the contracts (measurements, topics, API).
- Plain English with short sentences.
