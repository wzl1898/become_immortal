# Project Instructions

## Workflow

- In planning-oriented discussions, clarify the problem first. If a plan is needed, ask the user whether they want a plan before writing one. After the user confirms, provide the plan and wait for them to switch into implementation mode.
- After completing each fix or feature, commit and push without waiting for another reminder from the user.
- Use port `8888` for the main app. Start and verify the user-facing service on this port unless there is a specific reason not to.
- After code changes, make sure the service can run. Confirm that `http://127.0.0.1:8888` starts without errors and responds before handing back.
- Service health checks must use a timeout of 10 seconds or less, for example `curl --max-time 8 ...`.
- Do not edit backend files while the user's `--reload` service is running on `8888`. File edits can trigger a hot restart, and open SSE or livereload connections may leave uvicorn stuck at `Waiting for connections to close`; the process can keep occupying port `8888` while no longer responding. For validation, start a temporary no-reload instance on another port such as `8901` or `8902` instead of disturbing the user's running process.

## Common Failure

- If the frontend opens into a blank new game or saves appear to be missing, assume the `8888` service may be stuck or unresponsive before assuming data loss.
- Check the listener with `lsof -nP -iTCP:8888 -sTCP:LISTEN`.
- Probe health with a bounded request such as `no_proxy=127.0.0.1,localhost curl --max-time 8 http://127.0.0.1:8888/api/saves`.
- Confirm save data directly in `backend/data/saves.db` with `sqlite3` or Python if needed.
- If the service is stuck, restart it; the save data should reappear once the backend can read the database again.

## Start Command

```bash
cd backend && ../.venv/bin/python -m uvicorn main:app --reload --port 8888 --timeout-graceful-shutdown 3
```

- On this machine the default proxy may use the `socks5h://` scheme, which `httpx` does not accept. Before starting the service, change proxy environment variables to `socks5://`, for example:

```bash
export ALL_PROXY=socks5://127.0.0.1:7897
```

- Apply the same `socks5://` scheme to `HTTPS_PROXY` and `HTTP_PROXY` if they are set.
- When probing local services with `curl`, bypass the proxy:

```bash
no_proxy=127.0.0.1,localhost curl --max-time 8 http://127.0.0.1:8888/api/saves
```
