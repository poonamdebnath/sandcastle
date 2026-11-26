# Sandcastle

Sandcastle is a Python issue-to-PR worker for a configured GitHub repository. It
discovers eligible issues, creates isolated Git worktrees, runs an implementation
agent and verification, opens PRs, follows review and CI, and cleans up after
merge. The controlled repository owns its issue policy, commands, templates,
agent profiles, GitHub workflows, and persistent state.

The Python runner lives in `sandcastle/runner`. Configuration and templates
belong to each target repository; this product repo contains no target
repository's identity or credentials.

## Install and run

Requires Python 3.11 or newer, Git, GitHub CLI, and the tools named by the
target repository's verification gates and agent profiles.

```sh
python -m pip install git+https://github.com/poonamdebnath/sandcastle.git
python -m sandcastle.runner.main --repo-root /path/to/target-repo report
```

The target checkout needs `sandcastle/config/repo.json`,
`issue-policy.json`, `gates.json`, `agent-profiles.json`,
`controller-backend.json`, and the templates named by its policy under
`sandcastle/templates`. Configure every repository identity, check command,
label policy, model profile, and host requirement for the target. Do not enable
unattended execution until those settings and the runner host have been
validated.

The `--repo-root` option must come before the command. If omitted, the runner
uses the current directory. `SANDCASTLE_REPO_ROOT` also selects the target
checkout for subprocesses and direct module invocations. Runtime state uses
`SANDCASTLE_STATE_ROOT` when set, otherwise the target Git common directory.

Run the tests with:

```sh
python -m unittest discover -s sandcastle/tests -q
```
