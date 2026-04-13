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

## Version compatibility

- **Elasticsearch**: 9.0+ (Basic license is enough)
- **Kibana**: 9.0+
- **otelcol-contrib**: 0.87.0+ (required for `spanmetrics` connector and `mapping.allowed_modes` in the Elasticsearch exporter)
- **Python**: 3.10+
- **OpenTelemetry SDK (Python)**: 1.24+ (for log export support)
