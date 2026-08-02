"""
Airflow DAG: nightly BOM-aware yield intelligence run
=====================================================
extract (SAP + Teamcenter, parallel)
  -> BOM graph explosion (KPI-timed)
  -> ML analytics (correlator | supplier ranker | anomaly screen, parallel)
  -> Snowflake load
  -> dbt build (staging + marts + tests)
  -> Power BI dataset refresh
  -> excursion alert (only if new excursions found)

Deploy: copy this file (or git-sync the repo) into $AIRFLOW_HOME/dags.
Connections/variables expected:
  Variable  yield_intel_project_root   absolute path of the repo checkout
  Env       SNOWFLAKE_*, SAP_*         as documented in the module docstrings
  Conn      powerbi_api                HTTP conn with AAD service principal
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.models import Variable
from airflow.operators.bash import BashOperator
from airflow.operators.empty import EmptyOperator
from airflow.operators.python import BranchPythonOperator, PythonOperator
from airflow.utils.task_group import TaskGroup

PROJECT_ROOT = Path(Variable.get(
    "yield_intel_project_root",
    default_var="/opt/yield-intelligence"))
RAW = PROJECT_ROOT / "data" / "raw"
OUT = PROJECT_ROOT / "data" / "outputs"

DEFAULT_ARGS = {
    "owner": "yield-engineering",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "sla": timedelta(hours=2),
    "depends_on_past": False,
}


def _sys_path():
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def task_extract_sap(**_):
    _sys_path()
    from bom.sap_bom_extractor import (MockSAPGateway, SAPBOMExtractor,
                                       flatten_tree)
    gw = MockSAPGateway(RAW / "bom_edges.csv", RAW / "bom_nodes.csv")
    tree = SAPBOMExtractor(gw).extract_tree("IX-9000", "1000")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sap_bom_tree.json").write_text(json.dumps(tree))
    return len(flatten_tree(tree))


def task_parse_teamcenter(**_):
    _sys_path()
    import pandas as pd
    from bom.teamcenter_xml_parser import parse_plmxml, write_sample_plmxml
    xml = write_sample_plmxml(pd.read_csv(RAW / "bom_edges.csv"),
                              pd.read_csv(RAW / "bom_nodes.csv"),
                              OUT / "sample_plmxml.xml")
    tables = parse_plmxml(xml)
    for name, df in tables.items():
        df.to_csv(OUT / f"teamcenter_{name}.csv", index=False)
    return {k: len(v) for k, v in tables.items()}


def task_explode_bom(**context):
    _sys_path()
    from bom.bom_graph_builder import run_explosion
    kpi = run_explosion(RAW / "bom_edges.csv", RAW / "bom_nodes.csv",
                        "IX-9000", OUT)
    context["ti"].xcom_push(key="explosion_kpi", value=kpi)
    if kpi["elapsed_seconds"] > 120:
        raise RuntimeError(
            f"BOM explosion KPI breached: {kpi['elapsed_seconds']}s > 120s")


def task_correlate(**_):
    _sys_path()
    from analytics.yield_defect_correlator import run
    run(RAW, OUT)


def task_rank_suppliers(**_):
    _sys_path()
    from analytics.supplier_lot_ranker import run
    run(RAW, OUT)


def task_flag_anomalies(**_):
    _sys_path()
    from analytics.anomaly_flagging import run
    run(RAW, OUT)


def task_load_snowflake(**_):
    _sys_path()
    import os
    sys.path.insert(0, str(PROJECT_ROOT / "snowflake"))
    from yield_mart_loader import load
    dry = "SNOWFLAKE_ACCOUNT" not in os.environ
    load(RAW, OUT, dry_run=dry)


def branch_on_excursions(**_):
    import pandas as pd
    exc = pd.read_csv(OUT / "yield_excursions.csv")
    return ("alert_excursions" if exc["is_excursion"].any()
            else "no_new_excursions")


def task_alert(**_):
    import pandas as pd
    exc = pd.read_csv(OUT / "yield_excursions.csv")
    hits = exc[exc.is_excursion]
    # wire to Slack/Teams/PagerDuty via the relevant provider in production
    print(f"ALERT: {len(hits)} excursion lot(s): "
          f"{', '.join(hits.wafer_lot_id.head(10))}")


with DAG(
    dag_id="yield_intelligence_pipeline",
    description="BOM-aware defect correlation: SAP/Teamcenter -> Snowflake "
                "-> ML root-cause -> Power BI",
    schedule="0 2 * * *",                    # nightly, 02:00 fab time
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=DEFAULT_ARGS,
    tags=["semiconductor", "yield", "bom", "ml"],
    doc_md=__doc__,
) as dag:

    with TaskGroup("ingestion") as ingestion:
        extract_sap = PythonOperator(task_id="extract_sap_bom",
                                     python_callable=task_extract_sap)
        parse_tc = PythonOperator(task_id="parse_teamcenter_xml",
                                  python_callable=task_parse_teamcenter)

    explode = PythonOperator(task_id="explode_bom_graph",
                             python_callable=task_explode_bom)

    with TaskGroup("ml_analytics") as ml:
        correlate = PythonOperator(task_id="yield_defect_correlator",
                                   python_callable=task_correlate)
        rank = PythonOperator(task_id="supplier_lot_ranker",
                              python_callable=task_rank_suppliers)
        anomalies = PythonOperator(task_id="anomaly_flagging",
                                   python_callable=task_flag_anomalies)

    load_wh = PythonOperator(task_id="load_snowflake",
                             python_callable=task_load_snowflake)

    dbt_build = BashOperator(
        task_id="dbt_build",
        bash_command=(f"cd {PROJECT_ROOT}/dbt_models && "
                      "dbt deps --quiet && dbt build --fail-fast"),
    )

    refresh_pbi = BashOperator(
        task_id="refresh_powerbi_dataset",
        bash_command=(
            "curl -sf -X POST "
            "-H \"Authorization: Bearer $POWERBI_TOKEN\" "
            "https://api.powerbi.com/v1.0/myorg/groups/$PBI_WORKSPACE_ID"
            "/datasets/$PBI_DATASET_ID/refreshes "
            "|| echo 'Power BI refresh skipped (no credentials)'"),
    )

    gate = BranchPythonOperator(task_id="check_excursions",
                                python_callable=branch_on_excursions)
    alert = PythonOperator(task_id="alert_excursions",
                           python_callable=task_alert)
    quiet = EmptyOperator(task_id="no_new_excursions")

    ingestion >> explode >> ml >> load_wh >> dbt_build >> refresh_pbi
    dbt_build >> gate >> [alert, quiet]
