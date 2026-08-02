import pandas as pd
import pytest

from bom.bom_graph_builder import BOMCycleError, BOMGraph

# tiny reference BOM:
#   ROOT -(2)-> A -(3)-> C        C also used directly by ROOT (qty 5)
#   ROOT -(1)-> B
EDGES = pd.DataFrame([
    {"parent_id": "ROOT", "child_id": "A", "qty_per": 2, "seq": 10},
    {"parent_id": "ROOT", "child_id": "B", "qty_per": 1, "seq": 20},
    {"parent_id": "A",    "child_id": "C", "qty_per": 3, "seq": 10},
    {"parent_id": "ROOT", "child_id": "C", "qty_per": 5, "seq": 30},
])


def make_graph():
    return BOMGraph(EDGES)


def test_explosion_levels_and_extended_qty():
    flat = make_graph().explode("ROOT")
    # C appears twice: via A (2*3=6) and directly (5)
    c_rows = flat[flat.component_id == "C"]
    assert len(c_rows) == 2
    assert sorted(c_rows.extended_qty) == [5.0, 6.0]
    assert flat[flat.component_id == "A"].level.iloc[0] == 1
    assert set(flat.level) == {1, 2}


def test_total_quantity_rollup():
    assert make_graph().total_quantity("ROOT", "C") == 11.0


def test_where_used_paths():
    used = make_graph().where_used("C")
    assert set(used.top_level) == {"ROOT"}
    assert len(used) == 2            # two independent usage paths


def test_cycle_detection():
    bad = pd.concat([EDGES, pd.DataFrame([
        {"parent_id": "C", "child_id": "ROOT", "qty_per": 1, "seq": 10}])])
    with pytest.raises(BOMCycleError):
        BOMGraph(bad)


def test_unknown_root_raises():
    with pytest.raises(KeyError):
        make_graph().explode("NOPE")
