"""AWS Lambda extractor — Serverless Framework (serverless.yml) and AWS SAM /
CloudFormation templates (template.yaml / .json).

Emits:
  * ``lambda:<name>`` nodes,
  * lambda DEPENDS_ON the handler function (``func:<file>::<name>``) so a code
    change reaches the lambda,
  * ``api:<METHOD /path>`` nodes with lambda EXPOSES api (the lambda feeds the API),
  * S3 / SQS / SNS / DynamoDB-stream event sources as ``table:s3://bucket`` etc.
    with lambda DEPENDS_ON source (a change to the bucket/queue affects it),
  * DynamoDB tables and S3 buckets declared as resources (``table:<name>``),
    referenced from a function's environment (``!Ref``) -> lambda DEPENDS_ON table.

YAML parsing needs PyYAML (``pip install impactgraph[yaml]``); JSON works without it.
CloudFormation intrinsic tags (``!Ref``, ``!GetAtt``, ``!Sub``) are accepted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor


class LambdaExtractor(Extractor):
    name = "lambda"

    def __init__(self, template: Union[str, Path], code_root: Optional[Union[str, Path]] = None) -> None:
        self.template = Path(template)
        self.code_root = Path(code_root) if code_root else self.template.parent

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        doc = _load(self.template)
        rel_template = self.template.name
        graph.add_node(Node(id=f"file:{rel_template}", type=NodeType.FILE, name=rel_template, path=rel_template))
        if "functions" in doc:  # Serverless Framework
            self._serverless(graph, doc, rel_template)
        if "Resources" in doc:  # SAM / CloudFormation
            self._sam(graph, doc, rel_template)
        return graph

    # ------------------------------------------------------------ serverless
    def _serverless(self, graph: ImpactGraph, doc: Dict, rel_template: str) -> None:
        service = str(doc.get("service", "")) if not isinstance(doc.get("service"), dict) else str(doc["service"].get("name", ""))
        for name, fn in (doc.get("functions") or {}).items():
            lid = self._lambda(graph, name, fn.get("handler"), rel_template, service)
            for ev in fn.get("events") or []:
                if not isinstance(ev, dict):
                    continue
                for kind, spec in ev.items():
                    self._event(graph, lid, kind, spec)
            self._env_refs(graph, lid, fn.get("environment") or {}, doc)
        # provider-level env + resources
        for lid in [n.id for n in graph.nodes(NodeType.LAMBDA)]:
            self._env_refs(graph, lid, (doc.get("provider") or {}).get("environment") or {}, doc)
        self._cfn_resources(graph, (doc.get("resources") or {}).get("Resources") or {})

    # ------------------------------------------------------------------- sam
    def _sam(self, graph: ImpactGraph, doc: Dict, rel_template: str) -> None:
        resources = doc.get("Resources") or {}
        self._cfn_resources(graph, resources)
        for name, res in resources.items():
            rtype = str(res.get("Type", ""))
            if rtype not in ("AWS::Serverless::Function", "AWS::Lambda::Function"):
                continue
            props = res.get("Properties") or {}
            lid = self._lambda(graph, name, props.get("Handler"), rel_template, None, code_uri=props.get("CodeUri"))
            for ev_name, ev in (props.get("Events") or {}).items():
                self._event(graph, lid, str(ev.get("Type", "")).lower(), ev.get("Properties") or {})
            env = ((props.get("Environment") or {}).get("Variables")) or {}
            self._env_refs(graph, lid, env, doc)
            for policy in props.get("Policies") or []:
                if isinstance(policy, dict):
                    for pname, pval in policy.items():
                        if "TableName" in (pval or {}) if isinstance(pval, dict) else False:
                            tname = _ref_name(pval.get("TableName"))
                            if tname:
                                graph.add_edge(Edge(src=lid, dst=f"table:{tname.lower()}", type=EdgeType.DEPENDS_ON, meta={"via": "policy"}))

    # --------------------------------------------------------------- helpers
    def _lambda(self, graph, name, handler, rel_template, service, code_uri=None) -> str:
        lid = f"lambda:{name}"
        graph.add_node(Node(id=lid, type=NodeType.LAMBDA, name=str(name), path=rel_template,
                            meta={"handler": handler, "service": service, "code_uri": code_uri}))
        graph.add_edge(Edge(src=f"file:{rel_template}", dst=lid, type=EdgeType.CONTAINS))
        func_id = self._handler_func_id(handler, code_uri)
        if func_id:
            graph.add_edge(Edge(src=lid, dst=func_id, type=EdgeType.DEPENDS_ON, meta={"via": "handler"}))
        return lid

    def _handler_func_id(self, handler, code_uri) -> Optional[str]:
        if not handler or not isinstance(handler, str) or "." not in handler:
            return None
        module, _, func = handler.rpartition(".")
        module_path = module.replace(".", "/")
        candidates = []
        base = Path(str(code_uri)) if code_uri and not str(code_uri).startswith("s3://") else Path(".")
        candidates.append((base / f"{module_path}.py").as_posix().lstrip("./"))
        candidates.append((base / module_path / "__init__.py").as_posix().lstrip("./"))
        for rel in candidates:
            if (self.code_root / rel).exists():
                return f"func:{rel}::{func}"
        return f"func:{candidates[0]}::{func}"  # placeholder id; merges if the Python extractor sees the same path

    def _event(self, graph, lid, kind, spec) -> None:
        kind = (kind or "").lower()
        spec = spec or {}
        if kind in ("http", "httpapi", "api", "alb"):
            method = str(spec.get("method") or spec.get("Method") or "ANY").upper()
            path = spec.get("path") or spec.get("Path") or "/"
            api_id = f"api:{method} {path}"
            graph.add_node(Node(id=api_id, type=NodeType.API, name=f"{method} {path}"))
            graph.add_edge(Edge(src=lid, dst=api_id, type=EdgeType.EXPOSES))
        elif kind in ("s3",):
            bucket = _ref_name(spec.get("bucket") or spec.get("Bucket") or (spec if isinstance(spec, str) else None))
            if bucket:
                tid = f"table:s3://{bucket.lower()}"
                graph.add_node(Node(id=tid, type=NodeType.TABLE, name=f"s3://{bucket}", meta={"platform": "s3"}))
                graph.add_edge(Edge(src=lid, dst=tid, type=EdgeType.DEPENDS_ON, meta={"via": "event:s3"}))
        elif kind in ("sqs", "sns", "kinesis", "stream", "dynamodb"):
            arn = _ref_name(spec.get("arn") or spec.get("Queue") or spec.get("Topic") or spec.get("Stream") or spec.get("topicName") or spec)
            if arn:
                name = str(arn).split(":")[-1].split("/")[-1]
                tid = f"table:{kind}://{name.lower()}"
                graph.add_node(Node(id=tid, type=NodeType.TABLE, name=f"{kind}://{name}", meta={"platform": kind}))
                graph.add_edge(Edge(src=lid, dst=tid, type=EdgeType.DEPENDS_ON, meta={"via": f"event:{kind}"}))
        elif kind in ("schedule", "eventbridge", "cloudwatchevent"):
            graph.get_node(lid).meta["scheduled"] = True

    def _env_refs(self, graph, lid, env: Dict, doc: Dict) -> None:
        for key, value in (env or {}).items():
            name = _ref_name(value)
            if not name:
                continue
            # a Ref to a declared DynamoDB table / S3 bucket resource, or a literal table name in *_TABLE / *_BUCKET vars
            if str(key).upper().endswith(("_TABLE", "_TABLE_NAME", "TABLE")):
                tid = f"table:{name.lower()}"
                graph.add_node(Node(id=tid, type=NodeType.TABLE, name=name, meta={"platform": "dynamodb"}))
                graph.add_edge(Edge(src=lid, dst=tid, type=EdgeType.DEPENDS_ON, meta={"via": "env"}))
            elif str(key).upper().endswith(("_BUCKET", "BUCKET")):
                tid = f"table:s3://{name.lower()}"
                graph.add_node(Node(id=tid, type=NodeType.TABLE, name=f"s3://{name}", meta={"platform": "s3"}))
                graph.add_edge(Edge(src=lid, dst=tid, type=EdgeType.DEPENDS_ON, meta={"via": "env"}))

    def _cfn_resources(self, graph, resources: Dict) -> None:
        for name, res in (resources or {}).items():
            rtype = str((res or {}).get("Type", ""))
            props = (res or {}).get("Properties") or {}
            if rtype == "AWS::DynamoDB::Table":
                tname = _ref_name(props.get("TableName")) or name
                graph.add_node(Node(id=f"table:{str(tname).lower()}", type=NodeType.TABLE, name=str(tname), meta={"platform": "dynamodb", "resource": name}))
                graph.add_node(Node(id=f"table:{name.lower()}", type=NodeType.TABLE, name=name, meta={"platform": "dynamodb"})) if str(tname).lower() != name.lower() else None
            elif rtype == "AWS::S3::Bucket":
                bname = _ref_name(props.get("BucketName")) or name
                graph.add_node(Node(id=f"table:s3://{str(bname).lower()}", type=NodeType.TABLE, name=f"s3://{bname}", meta={"platform": "s3", "resource": name}))


def _ref_name(value: Any) -> Optional[str]:
    """Literal string, or the name inside {Ref: X} / {Fn::GetAtt: [X, Arn]} / '!Ref X' / '${self:...}'."""
    if value is None:
        return None
    if isinstance(value, str):
        m = re.match(r"^\s*!?(Ref|GetAtt)\s+([\w\.]+)", value)
        if m:
            return m.group(2).split(".")[0]
        if "${" in value and "}" in value:
            inner = re.findall(r"\$\{([^}]+)\}", value)
            return inner[-1].split(":")[-1].split(".")[-1] if inner else None
        return value
    if isinstance(value, dict):
        if "Ref" in value:
            return str(value["Ref"])
        if "Fn::GetAtt" in value:
            ga = value["Fn::GetAtt"]
            return str(ga[0] if isinstance(ga, list) else str(ga).split(".")[0])
        if "Fn::Sub" in value:
            return _ref_name(value["Fn::Sub"] if isinstance(value["Fn::Sub"], str) else value["Fn::Sub"][0])
        if "Fn::ImportValue" in value:
            return str(value["Fn::ImportValue"])
    if isinstance(value, list) and value:
        return _ref_name(value[0])
    return None


def _load(path: Path) -> Dict:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise ImportError("PyYAML is required for YAML templates: pip install impactgraph[yaml]") from e

    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, suffix, node):  # !Ref X -> {"Ref": "X"}, !GetAtt X.Arn -> {"Fn::GetAtt": [...]}, !Sub ...
        if isinstance(node, yaml.ScalarNode):
            val = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            val = loader.construct_sequence(node)
        else:
            val = loader.construct_mapping(node)
        key = "Ref" if suffix == "Ref" else f"Fn::{suffix}"
        if suffix == "GetAtt" and isinstance(val, str):
            val = val.split(".")
        return {key: val}

    _Loader.add_multi_constructor("!", _tag)
    return yaml.load(text, Loader=_Loader) or {}
