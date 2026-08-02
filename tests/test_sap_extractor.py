import pandas as pd

from bom.sap_bom_extractor import MockSAPGateway, SAPBOMExtractor, flatten_tree


def make_csvs(tmp_path):
    edges = pd.DataFrame([
        {"parent_id": "ROOT", "child_id": "A", "qty_per": 2, "seq": 10},
        {"parent_id": "A", "child_id": "B", "qty_per": 3, "seq": 10},
    ])
    nodes = pd.DataFrame([
        {"node_id": "ROOT", "description": "Product"},
        {"node_id": "A", "description": "Assy"},
        {"node_id": "B", "description": "Part"},
    ])
    e, n = tmp_path / "edges.csv", tmp_path / "nodes.csv"
    edges.to_csv(e, index=False)
    nodes.to_csv(n, index=False)
    return e, n


def test_recursive_extraction(tmp_path):
    e, n = make_csvs(tmp_path)
    extractor = SAPBOMExtractor(MockSAPGateway(e, n))
    tree = extractor.extract_tree("ROOT", "1000")

    assert tree["material"] == "ROOT"
    assert tree["components"][0]["material"] == "A"
    assert tree["components"][0]["qty_per"] == 2.0
    assert tree["components"][0]["components"][0]["material"] == "B"

    flat = flatten_tree(tree)
    assert len(flat) == 2
    assert extractor.calls == 3          # ROOT, A, B (B is a leaf)
