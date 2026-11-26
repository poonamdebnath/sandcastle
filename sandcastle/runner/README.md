# Sandcastle Runner

This directory holds the Sandcastle runner implementation.

Related docs:

- [`sandcastle/docs/workflow-architecture.md`](./../docs/workflow-architecture.md):
  end-to-end workflow diagrams, component ownership, persisted-state recovery, and extension
  guidance
- [`sandcastle/docs/issue-lifecycle-contract.md`](./../docs/issue-lifecycle-contract.md):
  authoritative current pickup and successful-completion rules
- [`sandcastle/docs/state-machine.md`](./../docs/state-machine.md): authoritative lifecycle
  states and transitions
- [`sandcastle/docs/operator-runbook.md`](./../docs/operator-runbook.md): operator-facing
  workflow and safety rules
- [`sandcastle/docs/github-actions-backend.md`](./../docs/github-actions-backend.md):
  unattended backend architecture
- [`sandcastle/docs/self-hosted-runner-setup.md`](./../docs/self-hosted-runner-setup.md):
  self-hosted runner onboarding, credentials, validation, and troubleshooting
- [`sandcastle/docs/issue-241-junior-setup-guide.md`](./../docs/issue-241-junior-setup-guide.md):
  numbered copy-paste setup path for a first-time `#241` AVD rollout

Current scope:

- automatically pick the next eligible issue when no issue is active
- automatically advance one active issue through the next implemented phase
- load repo and issue policy config
- list issues from GitHub
- evaluate issue eligibility
- read local state for one issue
- claim an eligible issue
- create local issue state
- create a fresh issue branch and worktree
- save issue context to local artifacts
- execute a configurable implementation harness inside the issue worktree
- run configured verification gates in the issue worktree
- wait for PR CI and capture failed CI logs as findings
- write implementation phase artifacts
- write internal review phase artifacts
- collect normalized findings from verification, CI, and PR discussion
- write revise-loop artifacts
- sync local state from the current PR lifecycle
- verify default-branch health after merge before closing the primary issue
- create or update one tracked post-merge incident record when `main` turns unhealthy
- clean up merged issue worktrees and local and remote branches safely
- suspend new code-mutating work and Sandcastle auto-merges while `main` is unhealthy, while still allowing monitoring and reporting wakeups
- auto-clean failed startup issue branches, worktrees, and runtime state when no PR or branch commit exists
- treat already-absent merged branches/worktrees as idempotent cleanup success and remove orphaned managed worktree directories when Git has already deregistered them
- resolve runtime state from one shared Git-common state store across worktrees
- manage a configurable active issue set with a WIP cap and a human-review backlog cap
- separate human-waiting PRs from active implementation work so backlog pickup can continue
- rank active issues by urgency so broken PRs and review-fix loops outrank fresh backlog pickup
- project detailed lifecycle steps into top-level operator states: `active`, `waiting`, `blocked`, and `done`
- keep passive waiting issues out of the active coding-slot count while still waking them back into active follow-up on CI failures, review comments, stale-branch sync needs, and merge-conflict handling
- guard active issue advancement in explicit parallel mode with per-issue worker leases that expire and refresh automatically
- emit explicit stop categories and scheduler-capacity snapshots so a paused loop is not confused with a scheduler limit
- provide a GitHub event normalization helper for Sandcastle controller wakeups
- provide backend description and validation helpers for unattended GitHub-triggered Sandcastle execution
- execute bounded Sandcastle controller runs from the GitHub Actions self-hosted backend when wakeups are accepted
- resolve Sandcastle runtime state from a durable configured root when `SANDCASTLE_STATE_ROOT` is supplied
- support explicit scheduler modes, with serial active-slot scheduling as the default and parallel backlog fill as an opt-in mode
- apply label-aware backlog ordering so hold/high/low policy labels can skip or reorder pickup ahead of raw oldest-first selection
- automatically mark clean draft PRs ready and merge clean PRs when repository merge policy allows it
- default the development-agent path to a bundled `codex exec` command that invokes Matt's `tdd` skill
- keep agent role model selection centralized in `sandcastle/config/agent-profiles.json` so coding, review, and conflict-resolution agents can use different models and reasoning policies
- still allow an external development-agent override through `SANDCASTLE_DEV_AGENT_COMMAND_JSON`
- default the review-agent path to a bundled `codex exec` command that invokes the repo-local Matt `code-review` skill against a deterministic merge-base fixed point
- still allow an external review-agent override through `SANDCASTLE_REVIEW_AGENT_COMMAND_JSON`
- feed normalized review-fix findings and revision plans back into the development agent on the same issue branch
- repeat verification and review after each development-agent fix pass until the PR is clean or blocks safely
- fail a zero-exit development pass when it produced no meaningful code changes
- own commit and push progression for development-agent changes instead of requiring the agent to commit
- enforce a configurable review-fix retry cap and block early when the same findings repeat unchanged across implementation passes
- detect PR merge-conflict states, update the same issue branch from fresh main, and invoke the bundled conflict-resolution fallback or an explicit override when Git cannot merge cleanly
- force a fresh verification and review pass after a conflict-resolution commit before the PR can move forward again
- persist a queue-wide `operator-report.json` and `morning-handover.json` under the shared Sandcastle state root so unattended runs leave a concise health summary behind

Current commands:

- `python -m sandcastle.runner.main auto-step`
- `python -m sandcastle.runner.main process-next`
- `python -m sandcastle.runner.main process-issue <issue-number>`
- `python -m sandcastle.runner.main run-loop`
- `python -m sandcastle.runner.main discover`
- `python -m sandcastle.runner.main report`
- `python -m sandcastle.runner.main morning-handover`
- `python -m sandcastle.runner.main run-issue <issue-number>`
- `python -m sandcastle.runner.main resume <issue-number>`
- `python -m sandcastle.runner.main status <issue-number>`
- `python -m sandcastle.runner.main load-context <issue-number>`
- `python -m sandcastle.runner.main verify <issue-number>`
- `python -m sandcastle.runner.main wait-ci <issue-number>`
- `python -m sandcastle.runner.main implement <issue-number>`
- `python -m sandcastle.runner.main review <issue-number>`
- `python -m sandcastle.runner.main collect-findings <issue-number>`
- `python -m sandcastle.runner.main revise <issue-number>`
- `python -m sandcastle.runner.main sync-pr <issue-number>`
- `python -m sandcastle.runner.main cleanup <issue-number>`

Recommended operator-facing orchestration commands:

- `process-next` advances one issue until it reaches a stable waiting, blocked, or done boundary.
- `process-issue <issue-number>` forces attention onto a specific issue while still obeying dependency, safety, and capacity rules.
- `run-loop` reuses the same orchestration engine as `process-next` and keeps polling once Sandcastle settles into waiting.
- `report` emits the full queue-wide operational snapshot and refreshes the persisted operator report files.
- `morning-handover` emits the concise overnight handover view from the same persisted queue state.

The lower-level phase commands, including `auto-step`, still remain callable for debugging and targeted recovery work.

## Unattended Execution

The supported unattended execution path in this repository is the GitHub Actions
self-hosted-runner backend, not an always-open local terminal.

Current implementation facts:

- `.github/workflows/sandcastle-controller-dispatch.yml` normalizes wakeup-worthy GitHub events
- `.github/workflows/sandcastle-controller.yml` runs a bounded `run-loop` on the configured
  self-hosted runner
- `sandcastle/runner/github_backend.py` validates host tools, state-root availability, agent
  command readiness, and configured role-model compatibility before unattended execution starts
- `python -m sandcastle.runner.github_backend prepare-host` creates the durable state root,
  verifies `gh` auth, writes repository variables, and reports whether the current identity can
  administer self-hosted runners
- the controller workflow injects `GH_TOKEN`, `GITHUB_TOKEN`, and `SANDCASTLE_STATE_ROOT` into
  the job
- development, review, and conflict-resolution each have bundled Codex fallbacks, with optional
  host overrides through the corresponding `SANDCASTLE_*_AGENT_COMMAND_JSON` environment
  variables

The current runner does not yet:

- post review comments itself without an external review-agent command that performs that publication

## Agent Profiles

Sandcastle resolves agent runtime settings from [`sandcastle/config/agent-profiles.json`](./../config/agent-profiles.json).

Current default roles:

- `development-agent` -> `coding-default` -> `gpt-5.3-codex` with `xhigh`
- `review-agent` -> `review-default` -> `gpt-5.4-1` with `xhigh`
- `conflict-resolution-agent` -> `conflict-default` -> `gpt-5.4-1` with `medium`

The bundled Codex development, review, and conflict-resolution fallbacks now apply both
`--model` and `--config model_reasoning_effort=...` from that file. External review and
conflict-resolution commands receive the same resolved values through
`SANDCASTLE_AGENT_MODEL` and `SANDCASTLE_AGENT_REASONING_EFFORT`.

Backend validation now probes the configured Codex role profiles directly, so an unsupported model or reasoning setting fails readiness before unattended execution starts instead of silently looking ready.

To add another role later, add a new profile under `profiles`, bind a role under `roles`, and have the new workflow stage call `load_agent_profile("<role-name>", repo_root)` rather than hardcoding model values in orchestration logic.
