import pandas as pd

from bom.teamcenter_xml_parser import parse_plmxml, write_sample_plmxml

NODES = pd.DataFrame([
    {"node_id": "ROOT", "part_number": "ROOT", "description": "Root <Prod>",
     "level_hint": 0, "category": "PRODUCT"},
    {"node_id": "A", "part_number": "A", "description": "Sub & Assy",
     "level_hint": 1, "category": "MODULE"},
    {"node_id": "B", "part_number": "B", "description": "Widget",
     "level_hint": 1, "category": "PART"},
])
EDGES = pd.DataFrame([
    {"parent_id": "ROOT", "child_id": "A", "qty_per": 2, "seq": 10},
    {"parent_id": "ROOT", "child_id": "B", "qty_per": 4, "seq": 20},
])


def test_roundtrip(tmp_path):
    xml = write_sample_plmxml(EDGES, NODES, tmp_path / "sample.xml",
                              root="ROOT")
    tables = parse_plmxml(xml)

    assert set(tables["parts"].part_id) == {"ROOT", "A", "B"}
    occ = tables["occurrences"]
    assert len(occ) == 2
    a_row = occ[occ.child_part_id == "A"].iloc[0]
    assert a_row.parent_part_id == "ROOT"
    assert a_row.quantity == 2.0
    # every part carries at least one revision row
    assert set(tables["revisions"].part_id) == {"ROOT", "A", "B"}


def test_special_characters_escaped(tmp_path):
    xml = write_sample_plmxml(EDGES, NODES, tmp_path / "esc.xml", root="ROOT")
    parts = parse_plmxml(xml)["parts"]
    assert (parts.set_index("part_id").loc["ROOT", "name"] == "Root <Prod>")
