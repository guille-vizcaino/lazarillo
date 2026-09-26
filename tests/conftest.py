from pathlib import Path

import duckdb
import pytest

from lazarillo.config import load_config


@pytest.fixture
def cfg(tmp_path: Path):
    con = duckdb.connect(str(tmp_path / "wh.duckdb"))
    con.execute("CREATE SCHEMA a; CREATE SCHEMA b")
    con.execute("CREATE TABLE a.t AS SELECT * FROM (VALUES (1, 'x', 'ana@example.com'), (2, 'y', NULL), (3, 'z', NULL)) v(id, val, email)")
    con.execute("CREATE TABLE b.t AS SELECT * FROM (VALUES (1, 'x', 10), (2, 'CHANGED', 20), (4, 'w', 40)) v(id, val, extra)")
    con.close()
    (tmp_path / "lazarillo.yml").write_text("warehouse:\n  path: wh.duckdb\nguardrails:\n  max_rows: 2\n  pii_columns: [email]\n")
    return load_config(tmp_path / "lazarillo.yml")
