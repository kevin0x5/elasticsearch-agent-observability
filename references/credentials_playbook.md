# Credentials Playbook

Short version: **don't embed cluster credentials in the generated bundle** unless you have a reason to, and if you must, mark the file as secret material.

## Default (recommended) path

The Collector config and all env files ship with **env placeholders**:

```yaml
elasticsearch:
  endpoints: [${env:ELASTICSEARCH_URL}]
  user: ${env:ELASTICSEARCH_USERNAME}
  password: ${env:ELASTICSEARCH_PASSWORD}
```

Operators export those variables before starting the Collector (or load them from a systemd drop-in, Kubernetes secret, vault agent, etc). The generated YAML can live in the repo; the secret never does.

Bootstrap command for this path:

```bash
python scripts/bootstrap_observability.py \
  --workspace <workspace> \
  --es-url <url> \
  --es-user elastic \
  --es-password '<password>'
  # no --embed-es-credentials; YAML stays clean
```

## When credentials end up on disk anyway

Two realistic reasons this happens:

1. **You passed `--embed-es-credentials` explicitly** because the runtime cannot read env vars (older container orchestrators, a background daemon launched without an env file, etc).
2. **An AI agent installed the skill and took a shortcut** to get traffic flowing end-to-end — pasted the password into the generated YAML so a detached process would work without shell env inheritance.

Both are legitimate short-term moves. Neither is where you want to stay.

## Integration clean-up checklist

When a bootstrap run produced a bundle with inline credentials, before calling it "production":

- [ ] Rotate the password — the bundle probably sat in a git working tree, a screenshare, or a chat log somewhere.
- [ ] Switch to a **dedicated low-privilege account** or **API key**, scoped to just what the Collector needs:
  - `monitor_cluster`
  - `write` on `<prefix>-*`
  - `manage_ilm` if the operator wants ILM updates from this identity
- [ ] Remove the embedded `user` / `password` from the generated YAML and re-render with env placeholders.
- [ ] Add the bundle directory to the workspace `.gitignore`. Generated artifacts with any history of secrets should not be committed.
- [ ] Record rotation date + new credential owner in an ops runbook.

The API key shape (Basic-licence-friendly) — this is the **bootstrap admin** key, not the runtime Collector key; see the least-privilege matrix below for the runtime shape:

```json
POST /_security/api_key
{
  "name": "agent-obsv-bootstrap",
  "role_descriptors": {
    "agent_obsv_bootstrap": {
      "cluster": ["monitor", "manage_ilm", "manage_index_templates", "manage_pipeline"],
      "index": [
        { "names": ["<prefix>-*"], "privileges": ["manage", "write", "create_index", "auto_configure"] }
      ]
    }
  }
}
```

Pass the returned `encoded` value as `ELASTICSEARCH_API_KEY` and swap the Basic-auth block in the Collector for an API-key block.

## Kibana credential note

`--apply-kibana-assets` uses the same ES Basic Auth by default. For production prefer `--kibana-api-key`; the generated wiring already accepts it.

## Least-privilege matrix

Different scripts need different privileges. Do not reuse the bootstrap key for the runtime Collector, and do not reuse either for `uninstall`.

| Script | Cluster privileges | Index privileges on `<prefix>-*` | Why |
|---|---|---|---|
| `bootstrap_observability.py --apply-es-assets` | `monitor`, `manage_ilm`, `manage_index_templates`, `manage_pipeline` | `manage`, `create_index`, `auto_configure` | Creates ILM policy, ingest pipeline, component + index templates, and bootstraps the data stream |
| `bootstrap_observability.py --apply-kibana-assets` | Kibana role with `all` on Saved Objects Management (or API key with the equivalent) | — | Writes data view, searches, Lens, and dashboard |
| `apply_elasticsearch_assets.py` (standalone) | same as bootstrap apply | same as bootstrap apply | Same surface; split out for CI reuse |
| **Collector / bridge at runtime** | `monitor` | `write`, `create_index`, `auto_configure` | **Steady-state identity. Never give this key `manage_ilm` / template perms.** This is what should live on production hosts. |
| `alert_and_diagnose.py` | `monitor` | `read` | Only runs aggregations + writes alert events (optional) |
| `alert_and_diagnose.py --write-to-es` | `monitor` | `read`, `write` | Adds alert docs to the events data stream |
| `generate_report.py` | `monitor` | `read` | Read-only aggregations |
| `validate_state.py` | `monitor` | `read` | Compares rendered assets to live state |
| `verify_pipeline.py` | `monitor` | `read` | OTLP canary + ES poll |
| `status.py` | `monitor` | `read` | GETs templates/policy/data stream |
| `doctor.py` | `monitor` | `read` | Same read-only surface as status + verify |
| `uninstall.py --confirm` | `monitor`, `manage_ilm`, `manage_index_templates`, `manage_pipeline` | `manage`, `delete_index` | Dangerous; keep a separate short-lived API key for this. Rotate immediately after. |

Rule of thumb: **three keys, not one.**

1. A short-lived **admin key** for bootstrap and uninstall. Create, use, delete.
2. A long-lived **ingest key** with `write`-only on `<prefix>-*` for the Collector and bridge.
3. A long-lived **read key** for alerting / reporting / dashboards / diagnostics.

Example ingest-only API key:

```json
POST /_security/api_key
{
  "name": "agent-obsv-collector",
  "role_descriptors": {
    "agent_obsv_ingest": {
      "cluster": ["monitor"],
      "index": [
        { "names": ["<prefix>-*"], "privileges": ["write", "create_index", "auto_configure"] }
      ]
    }
  }
}
```

The previous `manage_ilm` grant (shown below) is only appropriate for the bootstrap identity, not the runtime Collector.

## Rules

1. **Never** commit a YAML that contains inline credentials. If it exists on disk, treat it like a private SSH key.
2. **Never** let a Collector run with the superuser account in steady state. The bootstrap moment is the only excusable exception.
3. **Always** rotate after an agent-driven install. You don't know what history that password is now in.
4. If the skill's output lands in a repo, a `.gitignore` entry for `generated/` is the minimum viable hygiene.
