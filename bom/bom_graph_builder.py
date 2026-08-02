"""
Multi-Level BOM Explosion Engine (networkx)
===========================================
Loads the parent/child edge list into a directed acyclic graph and performs:

  * full multi-level explosion from any root  (level, path, extended qty)
  * where-used (implosion) for any component
  * cumulative quantity roll-up of a component across every usage path
  * structural validation (cycle detection with the offending path)

KPI target: explode an 8,000-node BOM in < 2 minutes.  In practice this
implementation runs in well under a second — the timing is captured and
returned so dashboards can report it.

Usage:
    python bom/bom_graph_builder.py --edges data/raw/bom_edges.csv \
        --nodes data/raw/bom_nodes.csv --root IX-9000 --out data/outputs
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Dict, List, Optional

import networkx as nx
import pandas as pd


class BOMCycleError(ValueError):
    """Raised when the BOM edge list contains a circular reference."""


class BOMGraph:
    """A multi-level BOM held as a networkx DiGraph.

    Edge attribute ``qty_per`` is the quantity of the child consumed by one
    unit of the parent.  Shared components (one child under several parents)
    are fully supported — each usage path is exploded independently, exactly
    like SAP CS12 / Teamcenter structure expand.
    """

    def __init__(self, edges: pd.DataFrame,
                 nodes: Optional[pd.DataFrame] = None) -> None:
        self.graph = nx.DiGraph()
        for row in edges.itertuples(index=False):
            self.graph.add_edge(row.parent_id, row.child_id,
                                qty_per=float(row.qty_per),
                                seq=getattr(row, "seq", 0))
        if nodes is not None:
            meta = nodes.set_index("node_id").to_dict("index")
            nx.set_node_attributes(self.graph, meta)
        self._validate()

    # ------------------------------------------------------------------ #
    def _validate(self) -> None:
        if not nx.is_directed_acyclic_graph(self.graph):
            cycle = nx.find_cycle(self.graph)
            pretty = " -> ".join(str(e[0]) for e in cycle) + f" -> {cycle[-1][1]}"
            raise BOMCycleError(f"BOM contains a circular reference: {pretty}")

    # ------------------------------------------------------------------ #
    def explode(self, root: str, max_depth: Optional[int] = None) -> pd.DataFrame:
        """Full multi-level explosion below ``root``.

        Returns one row per *usage occurrence* with:
          level, parent_id, component_id, qty_per,
          extended_qty  (product of qty_per along the path from root),
          path          (root > ... > component)
        """
        if root not in self.graph:
            raise KeyError(f"Root '{root}' not found in BOM graph")

        rows: List[dict] = []
        # iterative DFS: (node, level, extended_qty, path)
        stack = [(root, 0, 1.0, root)]
        while stack:
            node, level, ext_qty, path = stack.pop()
            if max_depth is not None and level >= max_depth:
                continue
            children = sorted(self.graph.successors(node),
                              key=lambda c: self.graph[node][c].get("seq", 0),
                              reverse=True)
            for child in children:
                qty = self.graph[node][child]["qty_per"]
                child_ext = ext_qty * qty
                child_path = f"{path} > {child}"
                rows.append({"level": level + 1, "parent_id": node,
                             "component_id": child, "qty_per": qty,
                             "extended_qty": child_ext, "path": child_path})
                stack.append((child, level + 1, child_ext, child_path))

        df = pd.DataFrame(rows)
        if not df.empty:
            desc = {n: d.get("description", "")
                    for n, d in self.graph.nodes(data=True)}
            df["description"] = df["component_id"].map(desc)
        return df

    # ------------------------------------------------------------------ #
    def where_used(self, component: str) -> pd.DataFrame:
        """Implosion: every parent chain from ``component`` up to each root."""
        if component not in self.graph:
            raise KeyError(f"Component '{component}' not found")
        rows = []
        stack = [(component, [component])]
        while stack:
            node, chain = stack.pop()
            parents = list(self.graph.predecessors(node))
            if not parents:
                rows.append({"component_id": component,
                             "top_level": node,
                             "usage_path": " < ".join(chain),
                             "depth": len(chain) - 1})
            for p in parents:
                stack.append((p, chain + [p]))
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------ #
    def total_quantity(self, root: str, component: str) -> float:
        """Cumulative extended quantity of ``component`` per one ``root``."""
        flat = self.explode(root)
        return float(flat.loc[flat.component_id == component,
                              "extended_qty"].sum())

    # ------------------------------------------------------------------ #
    def stats(self) -> Dict[str, float]:
        g = self.graph
        leaves = [n for n in g.nodes if g.out_degree(n) == 0]
        shared = [n for n in g.nodes if g.in_degree(n) > 1]
        return {"nodes": g.number_of_nodes(), "edges": g.number_of_edges(),
                "leaf_parts": len(leaves), "shared_components": len(shared),
                "max_depth": nx.dag_longest_path_length(g)}


# ---------------------------------------------------------------------- #
def run_explosion(edges_csv: Path, nodes_csv: Optional[Path], root: str,
                  out_dir: Path) -> dict:
    """CLI / Airflow entrypoint: explode and persist the flattened BOM.
    Returns a KPI dict (node count, elapsed seconds)."""
    edges = pd.read_csv(edges_csv)
    nodes = pd.read_csv(nodes_csv) if nodes_csv else None

    t0 = time.perf_counter()
    bom = BOMGraph(edges, nodes)
    flat = bom.explode(root)
    elapsed = time.perf_counter() - t0

    out_dir.mkdir(parents=True, exist_ok=True)
    flat.to_csv(out_dir / "bom_exploded.csv", index=False)

    kpi = {"root": root, "explosion_rows": len(flat),
           "elapsed_seconds": round(elapsed, 3), **bom.stats()}
    print(f"BOM explosion: {kpi['nodes']:,} nodes / {kpi['edges']:,} edges "
          f"-> {kpi['explosion_rows']:,} usage rows in "
          f"{kpi['elapsed_seconds']}s  (KPI target < 120s)")
    return kpi


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--edges", required=True, type=Path)
    ap.add_argument("--nodes", type=Path, default=None)
    ap.add_argument("--root", default="IX-9000")
    ap.add_argument("--out", type=Path, default=Path("data/outputs"))
    a = ap.parse_args()
    run_explosion(a.edges, a.nodes, a.root, a.out)
