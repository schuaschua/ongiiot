# Project rules

## Jira is updated with every story

Stories live in Jira project `OG` on https://example.atlassian.net. The story-to-key map is the `## Jira` table in `_bmad-output/planning-artifacts/epics.md`.

For every story, without being asked:

- **Start (planning begins):** move the issue to In Progress and comment with the plan file path and branch.
- **Built:** comment with the commit(s), verification results, and anything deferred or decided in review.
- **Merged:** comment with the PR and merge commit, then move the issue to Done. Note any manual follow-up (for example a deploy still to run).
- **Scope changes:** when a story's acceptance criteria change or work is deferred to another story, update or comment on the affected issues.
- **New work:** create a Jira issue under the right epic before building anything that has no key.
- Keep the `## Jira` table in `epics.md` in step with Jira. Never create a duplicate issue for a story that already has a key.
