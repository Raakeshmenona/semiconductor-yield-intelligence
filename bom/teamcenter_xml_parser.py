"""
Teamcenter PLMXML Parser  (XML -> normalized relational tables)
===============================================================
Siemens Teamcenter exports product structures as PLMXML.  This module
stream-parses those exports (``iterparse`` — constant memory even for
100 MB files) into three normalized DataFrames ready for warehouse load:

  parts        part_id, name, category
  revisions    part_id, revision, ecn, effective_date
  occurrences  parent_part_id, child_part_id, quantity, seq  (the BOM edges)

It also ships ``write_sample_plmxml`` which renders a subset of the synthetic
BOM as a valid PLMXML file, so the parser can be demonstrated (and unit
tested) without a Teamcenter licence.

Usage:
    python bom/teamcenter_xml_parser.py --xml data/outputs/sample_plmxml.xml \
        --out data/outputs
"""
from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Optional
from xml.sax.saxutils import escape

import pandas as pd

PLMXML_NS = "http://www.plmxml.org/Schemas/PLMXMLSchema"


def _local(tag: str) -> str:
    """Strip the '{namespace}' prefix ElementTree puts on tag names."""
    return tag.rsplit("}", 1)[-1]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_plmxml(xml_path: Path) -> Dict[str, pd.DataFrame]:
    """Stream-parse a PLMXML product structure into normalized tables."""
    parts, revisions, occurrences = [], [], []
    id_to_part: Dict[str, str] = {}          # element id -> partId
    pending_occurrences = []                  # resolved after full pass

    for _event, elem in ET.iterparse(str(xml_path), events=("end",)):
        tag = _local(elem.tag)

        if tag == "Part":
            part_id = elem.get("partId") or elem.get("id")
            id_to_part[elem.get("id", part_id)] = part_id
            parts.append({"part_id": part_id,
                          "name": elem.get("name", ""),
                          "category": elem.get("subType", "Part")})
            for child in elem:
                if _local(child.tag) == "Revision":
                    revisions.append({
                        "part_id": part_id,
                        "revision": child.get("revision", "A"),
                        "ecn": child.get("changeId", ""),
                        "effective_date": child.get("effectivity", "")})
            elem.clear()                      # constant-memory streaming

        elif tag == "Occurrence":
            pending_occurrences.append({
                "parent_ref": (elem.get("parentRef") or "").lstrip("#"),
                "child_ref": (elem.get("instancedRef") or "").lstrip("#"),
                "quantity": float(elem.get("quantity", 1)),
                "seq": int(elem.get("sequenceNumber", 0))})
            elem.clear()

    for occ in pending_occurrences:
        occurrences.append({
            "parent_part_id": id_to_part.get(occ["parent_ref"],
                                             occ["parent_ref"]),
            "child_part_id": id_to_part.get(occ["child_ref"],
                                            occ["child_ref"]),
            "quantity": occ["quantity"], "seq": occ["seq"]})

    return {"parts": pd.DataFrame(parts).drop_duplicates("part_id"),
            "revisions": pd.DataFrame(revisions),
            "occurrences": pd.DataFrame(occurrences)}


# --------------------------------------------------------------------------- #
# Sample export generator (demo / tests — stands in for a Teamcenter export)
# --------------------------------------------------------------------------- #
def write_sample_plmxml(edges: pd.DataFrame, nodes: pd.DataFrame,
                        out_path: Path, root: str = "IX-9000",
                        max_nodes: int = 300) -> Path:
    """Render the top slice of the synthetic BOM as a valid PLMXML file."""
    keep, frontier = {root}, [root]
    children = edges.groupby("parent_id")
    while frontier and len(keep) < max_nodes:
        parent = frontier.pop(0)
        if parent not in children.groups:
            continue
        for row in children.get_group(parent).itertuples(index=False):
            if len(keep) >= max_nodes:
                break
            if row.child_id not in keep:
                keep.add(row.child_id)
                frontier.append(row.child_id)

    sub_edges = edges[edges.parent_id.isin(keep) & edges.child_id.isin(keep)]
    sub_nodes = nodes[nodes.node_id.isin(keep)]

    lines = [f'<?xml version="1.0" encoding="UTF-8"?>',
             f'<PLMXML xmlns="{PLMXML_NS}" schemaVersion="6" '
             f'author="Teamcenter V13.2" language="en-US">',
             ' <ProductDef id="pd1">',
             '  <InstanceGraph id="ig1" rootRefs="id_' + root + '">']
    for n in sub_nodes.itertuples(index=False):
        lines.append(
            f'   <Part id="id_{n.node_id}" partId="{n.node_id}" '
            f'name="{escape(str(n.description))}" subType="{n.category}">')
        lines.append('    <Revision revision="A" changeId="ECN-INIT" '
                     'effectivity="2025-01-01"/>')
        lines.append('   </Part>')
    for i, e in enumerate(sub_edges.itertuples(index=False), start=1):
        lines.append(
            f'   <Occurrence id="occ{i}" parentRef="#id_{e.parent_id}" '
            f'instancedRef="#id_{e.child_id}" quantity="{e.qty_per}" '
            f'sequenceNumber="{e.seq}"/>')
    lines += ['  </InstanceGraph>', ' </ProductDef>', '</PLMXML>']

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xml", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("data/outputs"))
    a = ap.parse_args()

    tables = parse_plmxml(a.xml)
    a.out.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        path = a.out / f"teamcenter_{name}.csv"
        df.to_csv(path, index=False)
        print(f"  {name:12s} {len(df):>6,} rows -> {path}")


if __name__ == "__main__":
    main()
