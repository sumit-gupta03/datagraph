import textwrap

from datagraph import AirflowExtractor, NodeType
from datagraph.cli import main

DAG_FILE = textwrap.dedent('''
    from airflow import DAG
    from airflow.operators.python import PythonOperator
    from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator

    def load_customers():
        pass

    with DAG(dag_id="nightly_bookings", schedule="@daily") as dag:
        extract = PythonOperator(task_id="extract_customers", python_callable=load_customers)
        build_dim = SQLExecuteQueryOperator(
            task_id="build_dim_customer",
            sql="INSERT INTO analytics.dim_customer SELECT customer_id AS customer_key, email FROM raw.customers",
        )
        build_fact = SQLExecuteQueryOperator(
            task_id="build_fact_booking",
            sql="""CREATE OR REPLACE TABLE analytics.fact_booking AS
                   SELECT b.id, d.customer_key FROM raw.bookings b JOIN analytics.dim_customer d ON b.cid = d.customer_key""",
        )
        notify = PythonOperator(task_id="notify", python_callable=lambda: None)
        extract >> build_dim >> build_fact
        [build_dim, build_fact] >> notify
''')


def test_airflow_dag_tasks_and_dependencies(tmp_path):
    (tmp_path / "nightly.py").write_text(DAG_FILE, encoding="utf-8")
    graph = AirflowExtractor(tmp_path).extract()
    assert graph.get_node("dag:nightly_bookings").type == NodeType.DAG
    tasks = {n.name for n in graph.nodes(NodeType.TASK)}
    assert tasks == {"extract_customers", "build_dim_customer", "build_fact_booking", "notify"}
    # a change in extract_customers affects everything after it
    affected = graph.impact("task:nightly_bookings/extract_customers")
    assert {"task:nightly_bookings/build_dim_customer", "task:nightly_bookings/build_fact_booking", "task:nightly_bookings/notify"} <= set(affected)
    # list dependency [a, b] >> notify
    assert "task:nightly_bookings/notify" in graph.impact("task:nightly_bookings/build_fact_booking")


def test_airflow_sql_bridge_and_python_callable(tmp_path):
    (tmp_path / "nightly.py").write_text(DAG_FILE, encoding="utf-8")
    graph = AirflowExtractor(tmp_path).extract()
    edges = {(e.src, e.dst, e.type.value) for e in graph.edges()}
    assert ("task:nightly_bookings/build_dim_customer", "table:analytics.dim_customer", "writes_to") in edges
    assert ("task:nightly_bookings/build_dim_customer", "table:raw.customers", "depends_on") in edges
    assert ("task:nightly_bookings/build_fact_booking", "table:analytics.fact_booking", "writes_to") in edges
    assert ("task:nightly_bookings/extract_customers", "func:nightly.py::load_customers", "depends_on") in edges
    # a raw table change reaches the task, the table it writes, and downstream tasks
    affected = graph.impact("table:raw.customers")
    assert "task:nightly_bookings/build_dim_customer" in affected
    assert "table:analytics.dim_customer" in affected
    assert "task:nightly_bookings/build_fact_booking" in affected


def test_airflow_cli(tmp_path, capsys):
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "nightly.py").write_text(DAG_FILE, encoding="utf-8")
    gp = tmp_path / "g.json"
    assert main(["build", "--airflow", str(dags), "-o", str(gp)]) == 0
    assert "airflow:" in capsys.readouterr().out
    assert main(["impact", "table:raw.customers", "--graph", str(gp)]) == 0
    assert "build_dim_customer" in capsys.readouterr().out
