# Security Policy

## Supported version

The `main` branch is the supported public version.

## Reporting a vulnerability

Please do **not** open a public GitHub Issue for security-sensitive problems.

Use one of these paths instead:

- GitHub Security Advisories / private vulnerability reporting, if enabled for the repository
- Direct contact through the repository owner profile on GitHub

When reporting a vulnerability, include:

- the affected script or generated asset type
- the impact
- a minimal reproduction
- whether the issue requires explicit unsafe flags or happens with default settings

## What counts as security-sensitive here

Examples include:

- credentials written to disk unexpectedly
- generated files exposing secrets or sensitive prompts by default
- unsafe network behavior in Elasticsearch or Kibana apply paths
- command execution paths that can be abused with untrusted input

## OTLP HTTP bridge security boundaries

The generated `otlphttpbridge.py` has these security characteristics:

- **Bind address**: defaults to `127.0.0.1:14319` (loopback only). Changing to `0.0.0.0` exposes the bridge to the network.
- **No TLS**: the bridge accepts plain HTTP. Use a reverse proxy for TLS termination if exposed beyond localhost.
- **No authentication**: any process that can reach the bind address can submit OTLP payloads.
- **Body size limit**: rejects payloads larger than 50 MB to prevent memory exhaustion (DoS).
- **Sensitive field redaction**: strips `gen_ai.prompt`, `gen_ai.completion`, `gen_ai.tool.call.arguments`, and `gen_ai.tool.call.result` before writing to Elasticsearch (defence-in-depth alongside the ingest pipeline).
- **ES credentials**: read from environment variables (`ELASTICSEARCH_USERNAME` / `ELASTICSEARCH_PASSWORD`), not embedded in the script.

## Handling expectations

Please give reasonable time for triage and a fix before public disclosure.
