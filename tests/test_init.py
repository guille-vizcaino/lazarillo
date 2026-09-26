import pytest

from lazarillo.config import load_config
from lazarillo.init import init_project


def test_init_finds_dbt_project_and_writes_a_loadable_config(tmp_path):
    (tmp_path / "transform" / "shop").mkdir(parents=True)
    (tmp_path / "transform" / "shop" / "dbt_project.yml").write_text("name: shop\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "dbt_project.yml").write_text("name: ignored\n")

    out = init_project(tmp_path, "data/wh.duckdb", None, force=False)

    cfg = load_config(tmp_path / "lazarillo.yml")
    assert cfg.warehouse == tmp_path / "data" / "wh.duckdb"
    assert cfg.dbt.project_dir == tmp_path / "transform" / "shop"
    assert "dbt project: `transform/shop`" in out and "(not created yet)" in out
    assert '"mcp"' in out


def test_init_without_dbt_leaves_it_commented(tmp_path):
    init_project(tmp_path, "warehouse.duckdb", None, force=False)
    assert load_config(tmp_path / "lazarillo.yml").dbt is None


def test_init_does_not_overwrite_without_force(tmp_path):
    init_project(tmp_path, "a.duckdb", None, force=False)
    with pytest.raises(FileExistsError):
        init_project(tmp_path, "b.duckdb", None, force=False)
    init_project(tmp_path, "b.duckdb", None, force=True)
    assert load_config(tmp_path / "lazarillo.yml").warehouse.name == "b.duckdb"


# --- the warehouse from dbt's profiles.yml ------------------------------------------------

def dbt_project(root, profiles: str, name: str = "shop"):
    project = root / "transform"
    project.mkdir(parents=True)
    (project / "dbt_project.yml").write_text(f"name: {name}\nprofile: {name}\n")
    (project / "profiles.yml").write_text(profiles)
    return project


def test_duckdb_target_is_read_from_the_profile(tmp_path):
    dbt_project(tmp_path, """\
shop:
  target: dev
  outputs:
    dev: {type: duckdb, path: ../data/dev.duckdb, schema: dbt_me}
    prod: {type: duckdb, path: ../data/wh.duckdb, schema: marts}
""")
    out = init_project(tmp_path)

    cfg = load_config(tmp_path / "lazarillo.yml")
    assert cfg.warehouse == tmp_path / "data" / "wh.duckdb"  # prod wins over the default target
    assert (cfg.dbt.prod_target, cfg.dbt.prod_schema) == ("prod", "marts")
    assert (cfg.dbt.dev_target, cfg.dbt.dev_schema) == ("dev", "dbt_me")
    assert "profile `shop`, target `prod`" in out


def test_redshift_password_stays_an_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("RS_HOST", "my-cluster.abc123.eu-west-1.redshift.amazonaws.com")
    monkeypatch.setenv("RS_PASSWORD", "s3cret")
    dbt_project(tmp_path, """\
shop:
  target: prod
  outputs:
    prod:
      type: redshift
      host: "{{ env_var('RS_HOST') }}"
      port: 5439
      dbname: analytics
      user: dbt_prod
      password: "{{ env_var('RS_PASSWORD') }}"
      schema: analytics
""")
    init_project(tmp_path)

    text = (tmp_path / "lazarillo.yml").read_text()
    assert "s3cret" not in text and "password_env: RS_PASSWORD" in text
    rs = load_config(tmp_path / "lazarillo.yml").redshift
    assert (rs.host, rs.database, rs.user, rs.password_env) == (
        "my-cluster.abc123.eu-west-1.redshift.amazonaws.com", "analytics", "dbt_prod", "RS_PASSWORD")


def test_literal_password_is_left_behind(tmp_path):
    dbt_project(tmp_path, """\
shop:
  outputs:
    prod: {type: redshift, host: h.example.com, dbname: a, user: u, password: hunter2, schema: s}
""")
    out = init_project(tmp_path)
    assert "hunter2" not in (tmp_path / "lazarillo.yml").read_text()
    assert "was not copied" in out


def test_redshift_iam_and_serverless(tmp_path):
    dbt_project(tmp_path, """\
shop:
  outputs:
    prod:
      type: redshift
      method: iam
      host: analytics.123456789012.eu-west-1.redshift-serverless.amazonaws.com
      dbname: analytics
      iam_profile: warehouse-ro
      schema: analytics
""")
    init_project(tmp_path)
    rs = load_config(tmp_path / "lazarillo.yml").redshift
    assert (rs.iam, rs.workgroup, rs.profile, rs.cluster_identifier) == (True, "analytics", "warehouse-ro", None)


def test_other_adapters_are_reported(tmp_path):
    dbt_project(tmp_path, "shop:\n  outputs:\n    prod: {type: snowflake, account: x}\n")
    out = init_project(tmp_path)
    assert "snowflake" in out
    assert load_config(tmp_path / "lazarillo.yml").warehouse.name == "warehouse.duckdb"


def test_flags_override_the_profile(tmp_path):
    dbt_project(tmp_path, "shop:\n  outputs:\n    prod: {type: duckdb, path: a.duckdb}\n")
    init_project(tmp_path, {"type": "redshift", "host": "h", "database": "d", "iam": True, "cluster_identifier": "c"})
    assert load_config(tmp_path / "lazarillo.yml").redshift.cluster_identifier == "c"


# --- asking for what is missing -----------------------------------------------------------

class Answers:
    def __init__(self, **answers):
        self.answers, self.asked = answers, []

    def __call__(self, text, default=None, choices=None):
        self.asked.append(text)
        for key, value in self.answers.items():
            if text.lower().startswith(key.replace("_", " ")):
                return value
        return default


def test_asks_for_redshift_with_iam(tmp_path):
    ask = Answers(warehouse="redshift", redshift_host="my-cluster.abc.eu-west-1.redshift.amazonaws.com",
                  database="analytics", sign_in="iam")
    out = init_project(tmp_path, prompt=ask)

    rs = load_config(tmp_path / "lazarillo.yml").redshift
    assert (rs.iam, rs.cluster_identifier, rs.profile) == (True, "my-cluster", None)
    assert not any("password" in q.lower() and "env" not in q.lower() for q in ask.asked)
    assert "IAM (cluster `my-cluster`)" in out


def test_asks_only_what_the_profile_lacks(tmp_path):
    dbt_project(tmp_path, "shop:\n  outputs:\n    prod: {type: redshift, dbname: a, user: u, schema: s}\n")
    ask = Answers(redshift_host="h.example.com", sign_in="password")
    init_project(tmp_path, prompt=ask)
    assert ask.asked[0].startswith("Redshift host") and not any(q == "Database" for q in ask.asked)
    assert load_config(tmp_path / "lazarillo.yml").redshift.host == "h.example.com"


def test_without_prompt_missing_settings_name_the_flags(tmp_path):
    with pytest.raises(ValueError, match="--host, --database"):
        init_project(tmp_path, {"type": "redshift", "iam": True, "cluster_identifier": "c"})


def test_cli_init_asks_in_a_terminal(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from lazarillo import cli

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    out = CliRunner().invoke(cli.main, ["init", str(tmp_path)], input="duckdb\ndata/wh.duckdb\n")
    assert out.exit_code == 0, out.output
    assert load_config(tmp_path / "lazarillo.yml").warehouse == tmp_path / "data" / "wh.duckdb"

    out = CliRunner().invoke(cli.main, ["init", str(tmp_path), "--force", "--no-input", "-w", "redshift"])
    assert out.exit_code == 1 and "--host" in out.output
