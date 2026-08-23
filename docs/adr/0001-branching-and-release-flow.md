# ADR-0001: Branching and release flow

## Status

Proposed

## Context

alt-ani-cli-win depends on third-party streaming hosts (mp4upload, Lycoris Cafe, streamtape,
dood, streamwish/filemoon family, CDA, sibnet, VK, plus whatever yt-dlp falls back to) that
change their embed markup, tokens, and anti-bot behavior without notice. CI runs the unit test
suite against synthetic HTML/JS fixtures, so it verifies the parsers still handle known shapes —
it cannot confirm that a stream actually resolves and plays against the live host. Some changes
(new extractors, player-sort tweaks, FSM navigation changes) only surface real problems after
being used normally for a period of days to weeks.

`main` has so far received every merged PR directly, which means an extractor or player-flow
change that looks correct in CI but breaks against a live host lands on the branch users would
reasonably expect to be stable. There is currently no branch dedicated to soaking in changes
before they are considered release-worthy, and no separation between "this passed CI" and
"this has been used enough to trust."

## Decision

Introduce a two-tier branch model plus short-lived working branches.

### `main`

- Stable/release branch.
- Receives only release PRs and hotfix PRs — no feature PRs merge directly into it.
- Should be a protected branch (see Branch protection recommendation below).
- No direct pushes.
- Merges only after CI is green.

### `develop`

- Integration / soak-test branch.
- Used for day-to-day manual testing and real playback verification.
- Feature/fix/refactor PRs target `develop`.
- May contain changes that are not yet release-ready.
- Is the source branch for future `release/*` branches.

### `feature/*`, `fix/*`, `refactor/*`, `docs/*`

- Short-lived working branches, one topic per PR.
- Branch from `develop` by default, unless the change is release documentation or an ADR
  intended specifically for `main`.
- Merge into `develop`.

### `release/*`

- Cut from `develop` after a soak period.
- Used only for stabilization, version bump, changelog/release notes, and documentation —
  no new functionality.
- PR target is `main`.

### `hotfix/*`

- Cut from `main` for urgent fixes to the stable version.
- PR target is `main`.
- After merging, the fix must also be brought into `develop` (merge or cherry-pick), so the
  next release doesn't regress it.

### PR policy

- Standing, freely-mergeable `develop -> main` PRs are not maintained outside a release window.
- A `develop -> main` diff preview, if needed, must be opened as a draft PR explicitly marked
  "do not merge."
- Promotion to `main` happens through a dedicated `release/x.y.z` branch, not directly from a
  live `develop`.
- PRs should stay small and single-purpose.
- Experimental host/player changes land on `develop` first and are promoted only after manual
  verification against the live host.

### Branch protection recommendation

This ADR records the recommendation; it does not implement it (no branch protection was
configured as part of this change).

For `main`:

- Require pull request before merging.
- Require CI/status checks to pass.
- Block force pushes.
- Block branch deletion.
- Require conversation resolution before merging.
- Optionally restrict who can push directly.

For `develop`:

- Require CI before merge.
- No force-pushing once the branch flow is stable.
- Less restrictive than `main`, but not unmanaged.

## Consequences

Benefits:

- `main` stays stable and safe to build releases from.
- Experimental extractor/player changes get real-world soak time on `develop` before promotion.
- Release becomes a deliberate step (`release/*` branch) instead of an implicit side effect of
  merging to `main`.
- Hotfixes have a clear, fast path that doesn't require going through `develop` first.
- Lower risk of an untested change landing directly on the branch users treat as stable.

Costs:

- More branches and PRs to track.
- Requires discipline to keep to the flow, particularly branching from `develop` by default.
- Hotfixes must be remembered to be backported to `develop`.
- Release now requires an explicit `release/*` step rather than merging straight to `main`.
