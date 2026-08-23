import json

import pytest

from impactgraph import LambdaExtractor, NodeType, PythonExtractor, ImpactGraph
from impactgraph.cli import main


SAM_JSON = {
    "Resources": {
        "BookingsTable": {"Type": "AWS::DynamoDB::Table", "Properties": {"TableName": "bookings"}},
        "UploadsBucket": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": "uploads-prod"}},
        "GetBookings": {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "Handler": "handlers/bookings.get_bookings",
                "CodeUri": "src/",
                "Environment": {"Variables": {"BOOKINGS_TABLE": {"Ref": "BookingsTable"}}},
                "Events": {"Api": {"Type": "Api", "Properties": {"Path": "/bookings", "Method": "get"}}},
            },
        },
        "Ingest": {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "Handler": "handlers/ingest.main",
                "CodeUri": "src/",
                "Events": {"Upload": {"Type": "S3", "Properties": {"Bucket": {"Ref": "UploadsBucket"}}}},
            },
        },
    }
}


@pytest.fixture
def sam_project(tmp_path):
    (tmp_path / "template.json").write_text(json.dumps(SAM_JSON), encoding="utf-8")
    src = tmp_path / "src" / "handlers"
    src.mkdir(parents=True)
    (src / "bookings.py").write_text("def get_bookings(event, ctx):\n    return query('SELECT * FROM bookings')\n", encoding="utf-8")
    (src / "ingest.py").write_text("def main(event, ctx):\n    return 1\n", encoding="utf-8")
    return tmp_path


def test_sam_template(sam_project):
    graph = LambdaExtractor(sam_project / "template.json").extract()
    assert graph.get_node("lambda:GetBookings").type == NodeType.LAMBDA
    edges = {(e.src, e.dst, e.type.value) for e in graph.edges()}
    assert ("lambda:GetBookings", "func:src/handlers/bookings.py::get_bookings", "depends_on") in edges
    assert ("lambda:GetBookings", "api:GET /bookings", "exposes") in edges
    assert ("lambda:GetBookings", "table:bookingstable", "depends_on") in edges or ("lambda:GetBookings", "table:bookings", "depends_on") in edges
    assert ("lambda:Ingest", "table:s3://uploadsbucket", "depends_on") in edges or ("lambda:Ingest", "table:s3://uploads-prod", "depends_on") in edges


def test_lambda_plus_python_code_change_reaches_api(sam_project):
    graph = ImpactGraph()
    graph.merge(PythonExtractor(sam_project).extract())
    graph.merge(LambdaExtractor(sam_project / "template.json").extract())
    affected = graph.impact("func:src/handlers/bookings.py::get_bookings")
    assert "lambda:GetBookings" in affected
    assert "api:GET /bookings" in affected


def test_serverless_yaml(tmp_path):
    yaml = pytest.importorskip("yaml")
    (tmp_path / "serverless.yml").write_text(
        "service: shop\nprovider:\n  name: aws\n  environment:\n    ORDERS_TABLE: orders\nfunctions:\n"
        "  createOrder:\n    handler: src/orders.create\n    events:\n      - httpApi:\n          path: /orders\n          method: post\n"
        "      - sqs:\n          arn: arn:aws:sqs:us-east-1:123:order-events\n",
        encoding="utf-8",
    )
    graph = LambdaExtractor(tmp_path / "serverless.yml").extract()
    edges = {(e.src, e.dst, e.type.value) for e in graph.edges()}
    assert ("lambda:createOrder", "api:POST /orders", "exposes") in edges
    assert ("lambda:createOrder", "func:src/orders.py::create", "depends_on") in edges
    assert ("lambda:createOrder", "table:sqs://order-events", "depends_on") in edges
    assert ("lambda:createOrder", "table:orders", "depends_on") in edges


def test_lambda_cli(sam_project, capsys):
    gp = sam_project / "g.json"
    assert main(["build", "--repo", str(sam_project), "--lambda", str(sam_project / "template.json"), "-o", str(gp)]) == 0
    assert "lambda:" in capsys.readouterr().out
    assert main(["impact", "func:src/handlers/bookings.py::get_bookings", "--graph", str(gp)]) == 0
    assert "GET /bookings" in capsys.readouterr().out
