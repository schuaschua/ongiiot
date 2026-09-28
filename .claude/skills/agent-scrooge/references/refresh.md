---
name: refresh
description: Regenerate the token ledger and report what changed
code: RF
added: 2026-09-27
type: prompt
---

# Refresh the ledger

The outcome is six current files in the token usage folder (org config `token_usage_folder`, default `{project-root}/docs/costing/token_usage/`), and {user_name} knowing what moved since the last look.

Run `uv run scripts/token_report.py {project-root}` (`--help` explains every option; `--since YYYY-MM-DD` limits the count). It prints JSON with the files written, the call count and the total cost units. Read `token_usage_summary_final.csv` before and after when {user_name} asks what changed, and name the items that grew the most. When `token-budgets.json` is set, the output has a `budgets` list, and the final summary has `actual_usd`, `budget_usd` and `variance_usd` columns. Name any phase or epic over its budget, or past 80% of it.

If the script reports no log folder, say so plainly. Don't go looking through the logs by hand: they hold whole conversations, and the script is the only permitted way in.

New stories or bugs without a row in `{project-root}/_bmad/memory/agent-scrooge/stories.json` still show up under their Jira key, just without a story number or title. Offer to add them (create the file if it's missing): `{"stories": {"PROJ-18": ["2.3", "Choose a product"]}}`.
