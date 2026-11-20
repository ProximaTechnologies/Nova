#!/usr/bin/env python3
"""Validate local GitOps references without contacting Kubernetes or Helm repos."""
import argparse
import fnmatch
from pathlib import Path
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: python3 -m pip install -r gitops/scripts/requirements-validation.txt")


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def documents(path):
    return list(yaml.load_all(path.read_text(), Loader=UniqueLoader))


def render(value, element):
    return re.sub(r"{{\s*(\w+)\s*}}", lambda m: str(element.get(m[1], m[0])), value)


def applications(root):
    for path in sorted((root / "appsets").glob("*.yaml")):
        for doc in documents(path):
            if not isinstance(doc, dict) or doc.get("kind") != "ApplicationSet":
                continue
            for generator in doc["spec"]["generators"]:
                if "list" not in generator:
                    raise ValueError(f"{path.name}: validator supports list generators only")
                for element in generator["list"].get("elements", []):
                    yield path, doc, element


def validate(root):
    errors = []
    repo = root.parent
    for path in sorted(root.rglob("*")):
        if path.suffix in (".yaml", ".yml"):
            try:
                documents(path)
            except (yaml.YAMLError, ValueError) as exc:
                errors.append(f"{path.relative_to(root)}: {exc}")
    if errors:
        return errors
    projects = {d["metadata"]["name"]: d["spec"] for d in documents(root / "appsets/projects.yaml")
                if isinstance(d, dict) and d.get("kind") == "AppProject"}
    root_app = documents(root / "argo-cfg/root-app.yaml")[0]
    local_url = root_app["spec"]["source"]["repoURL"]
    if root_app["spec"]["source"]["path"] != f"{root.name}/appsets":
        errors.append("root Application must target gitops/appsets")
    active_envs = set()
    names = set()
    for path, doc, element in applications(root):
        template = doc["spec"]["template"]
        spec = template["spec"]
        name = render(template["metadata"]["name"], element)
        if name in names:
            errors.append(f"duplicate generated Application name: {name}")
        names.add(name)
        active_envs.add(element["env"])
        project_name = render(spec["project"], element)
        project = projects.get(project_name)
        if project is None:
            errors.append(f"{name}: missing project {project_name}")
            continue
        sources = spec.get("sources", [spec["source"]] if "source" in spec else [])
        refs = {source["ref"]: source for source in sources if "ref" in source}
        for source in sources:
            url = source["repoURL"]
            if not any(fnmatch.fnmatchcase(url, allowed) for allowed in project.get("sourceRepos", [])):
                errors.append(f"{name}: repository {url} not allowed by {project_name}")
            if url == local_url and source.get("targetRevision") != root_app["spec"]["source"]["targetRevision"]:
                errors.append(f"{name}: repository revision differs from root Application")
            if url == local_url and "path" in source:
                source_path = render(source["path"], element)
                if not (repo / source_path).is_dir():
                    errors.append(f"{name}: missing source directory {source_path}")
            for value in source.get("helm", {}).get("valueFiles", []):
                if not value.startswith("$"):
                    continue
                ref, _, value_path = value[1:].partition("/")
                if ref not in refs:
                    errors.append(f"{name}: undefined values reference ${ref}")
                elif refs[ref]["repoURL"] == local_url:
                    value_path = render(value_path, element)
                    if not (repo / value_path).is_file():
                        errors.append(f"{name}: missing values file {value_path}")
        destination = {k: render(v, element) for k, v in spec["destination"].items()}
        if not any(all(fnmatch.fnmatchcase(destination.get(k, ""), pattern) for k, pattern in allowed.items())
                   for allowed in project.get("destinations", [])):
            errors.append(f"{name}: destination not allowed by {project_name}")
    for env in active_envs:
        paths = [root / "env" / env, *(root / "overlays").glob(f"*/{env}")]
        for folder in paths:
            for path in folder.rglob("*.yaml"):
                if "REPLACEME" in path.read_text():
                    errors.append(f"active environment contains placeholder: {path.relative_to(root)}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--applications", action="store_true", help="Print active generated Application names")
    parser.add_argument("--app", action="append", default=[])
    parser.add_argument("--env", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.applications:
            selected = [render(d["spec"]["template"]["metadata"]["name"], e)
                        for _, d, e in applications(args.root)
                        if (not args.app or d["metadata"]["name"] in args.app)
                        and (not args.env or e["env"] in args.env)]
            if not selected:
                raise ValueError("no active Applications match the selected filters")
            print("\n".join(selected))
            return 0
        errors = validate(args.root)
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        if errors:
            return 1
    except (KeyError, TypeError, ValueError, OSError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("GitOps validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
