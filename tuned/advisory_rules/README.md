# tuned/advisory_rules

Upstream's agent rule files (CLAUDE.md, AGENTS.md, .claude/ skills and similar), moved here with `git mv` so that coding
agents do not load them by default. They are kept exactly as upstream wrote them, at the same relative path they had, so
a reader can still consult them and a merge from upstream can still carry upstream edits through the rename.

They are reference material, not instructions for this fork: the owner's own rules are authoritative. When one of them
informs work, name it as upstream's. Do not move them back. If a merge from upstream brings an agent rule file back to a default
path, tuned-common's `tools/upstream-sync.sh merge` moves it here in its own commit; `git mv` by hand otherwise.
A file with real local changes belongs in `tuned/local_modified_rules/` instead.
