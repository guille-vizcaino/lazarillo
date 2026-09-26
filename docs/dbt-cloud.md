# dbt Cloud

If production runs in dbt Cloud, Lazarillo can read the manifest of your production job
instead of a local `target/manifest.json`. The data map then describes what production
actually runs, not what happens to be compiled on your laptop.

```yaml
dbt:
  project_dir: transform/shop         # optional; verify still builds locally from here
  cloud:
    account_id: 12345
    job_id: 67890                     # the job that builds production
    host: cloud.getdbt.com            # or emea.dbt.com, au.dbt.com, ACCOUNT_PREFIX.us1.dbt.com
    token_env: DBT_CLOUD_API_TOKEN    # default
    cache_minutes: 10                 # default
```

```bash
export DBT_CLOUD_API_TOKEN=...        # a service token with read access to job artifacts
lazarillo map
```

The map starts by saying which manifest it used, e.g.
`Manifest: dbt Cloud job 67890, generated at 2026-09-26T06:00:00Z`.

## How it works

- Lazarillo calls the Admin API (`/api/v2/accounts/{account_id}/jobs/{job_id}/artifacts/manifest.json`),
  which returns the manifest of the job's latest successful run. The Discovery API only
  exposes parts of the manifest, so it is not used.
- The token is read from the environment variable named in `token_env`. It never goes in
  `lazarillo.yml`, and it is not sent on to any URL dbt Cloud redirects to.
- Downloads are cached in `.lazarillo/` next to `lazarillo.yml` for `cache_minutes`. Set it to
  `0` to always fetch.
- If dbt Cloud can't be reached or rejects the token, Lazarillo uses the last cached copy,
  then the local `target/manifest.json` if `project_dir` is set. The map says which one it
  used and why, so the agent knows when it is looking at an old manifest.
- `project_dir` is optional when `cloud` is set: `map`, `describe` and `impact` work with the
  manifest alone. `verify` builds in dev, so it still needs the local project.
