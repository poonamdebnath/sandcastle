# Sandcastle runner

This package contains the issue-to-PR controller. Install it in the environment that controls a target Git checkout. The target owns `sandcastle/config`, `sandcastle/templates`, its GitHub workflows, and its persistent state.

The main entry point is `python -m sandcastle.runner.main --repo-root /path/to/target <command>`. The installed `sandcastle-worker` command is equivalent. Common commands are:

- `report` and `morning-handover`: inspect the queue.
- `discover`: list eligible issues.
- `process-next` or `process-issue <number>`: advance work to a stable boundary.
- `run-loop`: poll and advance work until a configured stop.
- `status <number>`, `resume <number>`, and `cleanup <number>`: inspect or recover one issue.

Use `--help` for the full command list. The target's `config/repo.json`, `issue-policy.json`, `gates.json`, `agent-profiles.json`, and `controller-backend.json` define its behavior. Agent command overrides use the `SANDCASTLE_*_AGENT_COMMAND_JSON` environment variables. Set `SANDCASTLE_STATE_ROOT` to a durable path for unattended runs.

Sandcastle can create branches, push commits, open and merge PRs, close issues, and delete merged work branches when the target policy permits. Validate the target configuration and host before enabling unattended execution.
