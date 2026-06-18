# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A minimal static "license server" for a MetaTrader Expert Advisor (EA) named
`SemiAutoGridRecovery`. It is published via GitHub Pages and exists only to serve
`license.json` over HTTPS so the EA can fetch it and validate which trading
accounts are licensed and when their licenses expire. There is no backend,
build step, or application code — the entire repo is two static files plus a
deploy workflow.

## Files

- `license.json` — the source of truth. Consumed programmatically by the EA.
  Editing this file *is* the primary maintenance task (renewing an expiry,
  adding/removing a licensed account, bumping `version`/`updated`).
- `index.html` — a human-facing landing page that links to `license.json`.
  It exists so the GitHub Pages site has a root document; it is not consumed by
  the EA.

## `license.json` schema and conventions

```json
{
  "ea": "SemiAutoGridRecovery",        // EA name this license set applies to
  "version": "5.20",                    // EA version string
  "updated": "2026-02-12T20:00:00Z",    // ISO 8601 UTC; bump on every edit
  "licenses": [
    {
      "account": 509355,                // broker account number (integer)
      "expiry": "2026.12.31",           // date in YYYY.MM.DD (dots, NOT dashes)
      "note": "Demo - PorPank"          // free-text label
    }
  ]
}
```

Conventions that matter because the EA parses this file:
- `expiry` uses dot-separated `YYYY.MM.DD` (MetaTrader date convention), not
  ISO `YYYY-MM-DD`. Keep this format.
- `account` is a numeric account ID, not a string.
- Update the top-level `updated` timestamp whenever you change anything, so the
  EA / consumers can tell the file is fresh.
- Keep the file valid JSON — a syntax error breaks license checking for every
  account at once.

## Deployment

`.github/workflows/pages.yml` deploys the whole repo root to GitHub Pages on
every push to `master`. There is nothing to build; the artifact is the
directory as-is. To publish a license change, merge it to `master` and the
workflow makes it live at the Pages URL within a minute or two.

Note: the deploy trigger is `master`. Feature branches (including the one this
session develops on) are *not* deployed until merged to `master`.

## Working in this repo

There are no tests, linters, or package managers. Validation = confirming
`license.json` is well-formed JSON and the `expiry`/`account` formats above are
respected. There are no commands to build or run; open `index.html` locally or
hit the Pages URL to view it.
