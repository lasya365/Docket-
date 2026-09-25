# Docket frontend

`index.html` is the whole frontend (RFC-0003 section 12): one file, vanilla JS and CSS,
no build step, no framework, no npm. The server serves it at `GET /`.

- Routes: `#/changes`, `#/changes/{key}`, `#/ledger`.
- Talks only to the section 11 API, with relative same-origin URLs.
- Every string from the API is set with `textContent`; `innerHTML` is never used.
- Built for 1280 px, the demo screen.

`prototype-reference.html` is the Stage 1 prototype, kept for reference. Its screens and
visual language are unchanged; only the data source moved to `fetch`.
## Dev harness

Open `index.html?mock=1` to run against a bundled `DEMO` object instead of the API — shapes
copied from `backend/tests/fixtures/build_fixtures.py` (held, cleared, model-bump, unmatched),
including the no-code case where four of six chain links are missing. No network is used.

    python -m http.server 8777    # from this directory, then open
    http://localhost:8777/index.html?mock=1
