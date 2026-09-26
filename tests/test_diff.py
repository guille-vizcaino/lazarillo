from lazarillo.diff import diff
from lazarillo.warehouse import open_warehouse


def test_diff_finds_everything(cfg):
    with open_warehouse(cfg) as wh:
        r = diff(wh, "a.t", "b.t", key=["id"])
    assert (r.left_rows, r.right_rows) == (3, 3)
    assert (r.only_left, r.only_right, r.changed) == (1, 1, 1)
    assert r.columns_only_left == ["email"] and r.columns_only_right == ["extra"]
    assert r.changed_by_column == {"val": 1}
    assert not r.identical
    assert "Schema drift" in r.to_markdown()


def test_identical(cfg):
    with open_warehouse(cfg) as wh:
        r = diff(wh, "a.t", "a.t", key=["id"])
    assert r.identical


def test_where_window(cfg):
    with open_warehouse(cfg) as wh:
        r = diff(wh, "a.t", "b.t", key=["id"], where="id = 1")
    assert (r.only_left, r.only_right, r.changed) == (0, 0, 0)
