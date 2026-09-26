# Tarima Tickets

A fictional concert ticketing company, set up the way a real project would use Lazarillo:
its own `lazarillo.yml`, dbt project (`tarima/`) and data (`data/`, generated).

```bash
pip install -e "../..[all]"     # from this folder, or ".[all]" from the repo root
python build_demo.py            # source system, Delta and Iceberg landing, warehouse, dbt build
lazarillo map
```

Three problems are planted on purpose (late box office sales, a UTC shift in the Iceberg
migration, and an incremental model that misses refunds). The main
[README](../../README.md#try-the-demo-2-minutes-no-cloud-account) walks through each one.

Everything here, from artists and venues to buyers, is made up.
