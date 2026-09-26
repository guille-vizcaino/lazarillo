# checkride (design notes)

A *checkride* is the practical exam a pilot flies with an examiner before getting a rating.
`lazarillo checkride` will put agents through the same thing on data work.

## Idea

Every task starts from a broken but realistic platform (the Tarima Tickets demo plus a
scenario patch). The agent has to reach a verifiable end state. Each task is scored by
checks the harness can run, never by an LLM judge:

| task | the agent must… | pass when |
|---|---|---|
| `incremental-drift` | find why payouts are overstated and fix `fct_orders` | `verify fct_orders` reports identical |
| `late-arrivals` | explain the source/landing gap | answer names the watermark and the 35 missing box office orders |
| `iceberg-parity` | decide whether the Iceberg table can replace Delta | answer is "no" and names the timestamp shift |
| `safe-refactor` | rename a column in staging without breaking the dashboard | dbt build passes, exposure columns unchanged |

Each task runs twice, **with** the harness (MCP tools) and **without** it (raw SQL
access), for every model under test. The headline number is how much the harness helps.

## Open questions

- Scenario format: a YAML file plus a patch applied to the demo project?
- Isolation: a fresh copy of `demo/data` per run.
- Reporting: Markdown table plus JSON, easy to publish.
