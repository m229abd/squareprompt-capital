# Verification record — 2026-11-04

- Final backend Docker image: 31 tests passed; one upstream Starlette/AnyIO deprecation warning.
- Final frontend Docker image: TypeScript and Vite production build passed.
- Compose configuration, including optional Opik profile: valid.
- Workspace, proxied API, MLflow health endpoint and TensorBoard: HTTP 200.
- Synthetic campaign `f9e4321d0c02`: completed 16 experiments, 41 tree nodes, all twelve category branches, an evaluated merge, and a frozen final winner (`h-signals`).
- Final holdout: 96 bars; winner selected before evaluation. Reported returns are synthetic workflow evidence only.
- Pause held persisted step 23 stable. Resume followed by SIGKILL/restart of the worker recovered the campaign and completed it.
- Inner notebook Git: 38 reachable commits, including two-parent merge ancestry.
- All 103 durable telemetry messages delivered; no pending delivery errors. MLflow model `squareprompt-f9e4321d0c02`, version 1, run `26385828f4634563a0944f6b7a67efcd`.
- Browser: final build reloaded, completed campaign and holdout visible; node inspection and campaign wizard verified. Screenshot: `workspace-preview.png`.
- CPU and memory limits added to research services, evaluator, Temporal and PostgreSQL. The idle core stack measured approximately 0.9 GiB after startup; build-time use and campaign peaks differ.

Not verified with this user's credentials: live LLM campaigns, provider-specific billing/reasoning metadata, and end-to-end Opik delivery. The optional Opik stack was not started. Interval refresh logic is implemented but a real multi-hour refresh schedule was not exercised. Live exchange order placement and a full margin/order-book simulator are outside this release. See the README and architecture contract for supported markets and evaluation assumptions.

## Adaptive compute correction

- 36 backend tests passed after replacing the fixed per-candidate cutoff with hardware-calibrated workload selection.
- Tests cover startup/replay separation, faster/slower hardware selecting different workloads, unchanged scoring windows when model cost changes, candidates completing after their target, and termination of child processes that start separate sessions when the total compute allocation is exhausted.
- Real Docker campaign `d8eee8673ae2` completed with a 2-second target and a 120-second total compute budget. Pilot startup alone took 3.79 seconds, so the minimum useful 84-bar workload was selected and estimated at 3.89 seconds. The baseline and two candidate evaluations finished in 4.59, 4.60 and 4.41 seconds without target-based termination. The full 48-bar holdout completed.
- The historical JSON field `evaluation_seconds` is retained for compatibility and now means target duration. Previously completed campaign results remain unchanged.

## Blue spruce and campaign configuration update

- 44 backend tests pass. Frontend TypeScript and production Docker build pass.
- Browser verified blue spruce styling, adding ARB as another sandbox market, independent Developer/Researcher/Critic selections from the configured provider catalog, target-before-data ordering, preservation of an edited 300-bar history when changing target duration, zero-valued resource inputs, quote review, and frozen-cost display. Draft test choices were discarded without saving role defaults or launching paid LLM calls.
- Sandbox campaign `4a7225e0cfd7` completed with DOGE and AAVE, a full 189-bar development window, unlimited USD/tokens/evaluation time, unlimited critic retries, and a finite two-experiment ceiling. Its reviewed execution snapshot `3bfdf163e02d460482d5edb76503cb22` remained pinned. MLflow registered version 1 and telemetry fully drained.
- Live public BTC quote endpoint returned HTTP 200 with exchange taker fee, signed hourly funding, bid/ask and spread. No live LLM calls or exchange orders were submitted by these checks.
- New campaigns hold the reviewed hourly funding rate constant for costs. Previously completed campaigns retain their original results and conventions.
- Screenshot: `blue-spruce-preview.png`.


## Campaign settings refinement — 2026-11-05

- 50 backend tests passed inside Docker. Coverage includes custom/unreported reasoning efforts reaching provider requests, literal `none`, remembered custom efforts, forced catalog refresh replacing cached metadata, the refresh API flag, and required explicit market selections.
- Final Docker frontend build passed TypeScript and Vite. Compose configuration and whitespace checks passed.
- Browser verified fallback suggestions in all three editable reasoning fields (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`) and typed a custom Developer effort. No provider calls or saved preference changes were made.
- Sandbox picker fetched 347 perpetual market options from Hyperliquid, with no preselected markets. Both modes use this same fetched catalog component.
- Browser measured all six resource inputs at 195 × 35 pixels, with target evaluation time last and Evaluation data below. Promotional dashboard panels, hero/footer slogans and wizard footer copy were removed. Final screenshot: `campaign-settings-preview.png`.
- Final deployed workspace reloaded successfully after checks. The test draft was discarded; no new campaign or exchange order was submitted.
