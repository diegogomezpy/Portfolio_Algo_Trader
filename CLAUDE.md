# sharpe-engine — start here

The full project anchor is **[docs/CLAUDE.md](docs/CLAUDE.md)** (this root file exists because
Claude Code only auto-loads the repo root). Read it, then docs/ in order.

Non-negotiables:
- **No credentials in source or YAML** — env vars only (`.env.paper` / `.env.live`, git-ignored).
  Claude never handles raw key values.
- **Paper trading** until Phase 7 go-live; paper/live follows `ALPACA_BASE_URL`
  (`engine.config.is_paper_env`).
- Tunables live in `config/settings.yaml` — never hardcode a parameter that belongs there
  (guarded by `tests/unit/test_settings_coverage.py`).
- Run `pytest tests/unit tests/integration` before any deploy; deploys go through git
  (`git reset --hard origin/main` on the VM).
- On the VM, restarting `sharpe-eod` cancels ALL open orders — check the blotter first;
  `sharpe-dashboard` restarts are safe.
