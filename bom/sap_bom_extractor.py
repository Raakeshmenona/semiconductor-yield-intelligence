"""
SAP BOM Extractor  (RFC/BAPI -> JSON BOM tree)
==============================================
Pulls multi-level BOMs from SAP ECC / S/4HANA by repeatedly calling the
single-level explosion function module ``CS_BOM_EXPL_MAT_V2`` (the same RFC
transaction CS12 uses) and stitching the results into a JSON tree.

Two gateways are provided:

  * ``SAPRfcGateway``   — real system, requires the ``pyrfc`` SDK + SAP NW RFC
                          libraries and connection params (ashost/sysnr/client).
  * ``MockSAPGateway``  — emulates the RFC responses from the synthetic CSVs so
                          the whole pipeline runs end-to-end without an SAP box.

Usage:
    # against the mock (local demo)
    python bom/sap_bom_extractor.py --material IX-9000 --plant 1000 --mock \
        --edges data/raw/bom_edges.csv --nodes data/raw/bom_nodes.csv \
        --out data/outputs/sap_bom_tree.json

    # against a real system (reads SAP_* env vars)
    python bom/sap_bom_extractor.py --material IX-9000 --plant 1000
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

try:                                    # optional dependency — real SAP only
    from pyrfc import Connection        # type: ignore
except ImportError:                     # pragma: no cover
    Connection = None


class SAPRfcGateway:
    """Thin wrapper over pyrfc.Connection for the BOM explosion RFC."""

    def __init__(self, conn_params: Optional[Dict[str, str]] = None) -> None:
        if Connection is None:
            raise RuntimeError(
                "pyrfc is not installed. Install the SAP NW RFC SDK and "
                "'pip install pyrfc', or use MockSAPGateway / --mock.")
        conn_params = conn_params or {
            "ashost": os.environ["SAP_ASHOST"],
            "sysnr":  os.environ.get("SAP_SYSNR", "00"),
            "client": os.environ.get("SAP_CLIENT", "100"),
            "user":   os.environ["SAP_USER"],
            "passwd": os.environ["SAP_PASSWD"],
        }
        self.conn = Connection(**conn_params)

    def single_level(self, material: str, plant: str,
                     application: str = "PP01") -> List[dict]:
        """One CS_BOM_EXPL_MAT_V2 call, single level (MEHRS=' ')."""
        result = self.conn.call(
            "CS_BOM_EXPL_MAT_V2",
            CAPID=application, DATUV="99991231",
            MTNRV=material, WERKS=plant,
            MEHRS=" ",                      # single-level; we recurse ourselves
            STPST=0, SVWVO="X", VRSVO="X",
        )
        return [
            {"component": row["IDNRK"].strip(),
             "description": row.get("OJTXP", "").strip(),
             "qty_per": float(row["MNGLG"]),
             "uom": row.get("MEINS", "EA").strip(),
             "item_number": row.get("POSNR", "").strip()}
            for row in result.get("STB", [])
        ]


class MockSAPGateway:
    """Serves single-level BOM calls from the synthetic edge/node CSVs,
    shaped exactly like the STB table of CS_BOM_EXPL_MAT_V2."""

    def __init__(self, edges_csv: Path, nodes_csv: Path) -> None:
        self.edges = pd.read_csv(edges_csv)
        desc = pd.read_csv(nodes_csv).set_index("node_id")["description"]
        self.desc = desc.to_dict()

    def single_level(self, material: str, plant: str,
                     application: str = "PP01") -> List[dict]:
        rows = self.edges[self.edges.parent_id == material]
        return [
            {"component": r.child_id,
             "description": self.desc.get(r.child_id, ""),
             "qty_per": float(r.qty_per),
             "uom": "EA",
             "item_number": f"{int(r.seq):04d}"}
            for r in rows.itertuples(index=False)
        ]


class SAPBOMExtractor:
    """Recursive multi-level extraction via repeated single-level calls."""

    def __init__(self, gateway) -> None:
        self.gateway = gateway
        self.calls = 0

    def extract_tree(self, material: str, plant: str,
                     max_depth: int = 12) -> dict:
        return self._expand(material, plant, level=0, max_depth=max_depth,
                            lineage={material})

    def _expand(self, material: str, plant: str, level: int,
                max_depth: int, lineage: set) -> dict:
        node = {"material": material, "plant": plant, "level": level,
                "components": []}
        if level >= max_depth:
            return node
        self.calls += 1
        for item in self.gateway.single_level(material, plant):
            comp = item["component"]
            if comp in lineage:            # recursive BOM guard (SAP would too)
                continue
            child = self._expand(comp, plant, level + 1, max_depth,
                                 lineage | {comp})
            child.update({"description": item["description"],
                          "qty_per": item["qty_per"], "uom": item["uom"],
                          "item_number": item["item_number"]})
            node["components"].append(child)
        return node


def flatten_tree(tree: dict, parent: Optional[str] = None,
                 rows: Optional[list] = None) -> pd.DataFrame:
    """JSON tree -> normalized edge table (ready for Snowflake RAW load)."""
    if rows is None:
        rows = []
    for child in tree.get("components", []):
        rows.append({"parent_id": tree["material"],
                     "child_id": child["material"],
                     "qty_per": child["qty_per"],
                     "level": child["level"],
                     "description": child.get("description", "")})
        flatten_tree(child, tree["material"], rows)
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--material", required=True)
    ap.add_argument("--plant", default="1000")
    ap.add_argument("--mock", action="store_true",
                    help="use MockSAPGateway backed by the synthetic CSVs")
    ap.add_argument("--edges", type=Path, default=Path("data/raw/bom_edges.csv"))
    ap.add_argument("--nodes", type=Path, default=Path("data/raw/bom_nodes.csv"))
    ap.add_argument("--out", type=Path,
                    default=Path("data/outputs/sap_bom_tree.json"))
    a = ap.parse_args()

    gateway = (MockSAPGateway(a.edges, a.nodes) if a.mock
               else SAPRfcGateway())
    extractor = SAPBOMExtractor(gateway)
    tree = extractor.extract_tree(a.material, a.plant)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(tree, indent=2))
    flat = flatten_tree(tree)
    print(f"Extracted BOM for {a.material}: {len(flat):,} usage rows "
          f"via {extractor.calls:,} RFC calls -> {a.out}")


if __name__ == "__main__":
    main()
