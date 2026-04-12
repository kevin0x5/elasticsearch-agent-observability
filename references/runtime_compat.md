# Runtime Compatibility

## Primary shape

The canonical version of this repo is the `SKILL.md + scripts + references` shape.

That keeps the core logic in one place:

- discovery rules
- Collector rendering
- Elasticsearch asset rendering
- report generation

## Runtime targets

- **CodeBuddy**: direct fit for `SKILL.md`
- **Claude Code**: direct fit for `SKILL.md`
- **OpenClaw**: adapt with a thin wrapper that points to the same scripts

## Rule

Do not fork the core logic per runtime.
Adapt trigger and installation entrypoints, but keep the discovery and rendering logic shared.
