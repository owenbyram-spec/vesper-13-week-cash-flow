#!/usr/bin/env python3
"""Copy a pinned, read-only PE source dependency graph, never its data/history."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = (
    "app.routers.cashflow_forecast", "app.assistant.main", "app.routers.agent",
    "app.routers.actions", "app.routers.proposals", "app.routers.reporting",
    "app.tasks.cash_forecast_refresh", "app.reporting.excel_sections_forecast",
    "app.reporting.pdf_sections_forecast", "app.services.tenant_llm_credentials",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args()
    source = args.source.resolve()

    def git(*values):
        return subprocess.check_output(["git", "-C", str(source), *values])

    revision = git("rev-parse", args.ref + "^{commit}").decode().strip()
    tree = {}
    for record in git("ls-tree", "-rz", revision, "app", "requirements.txt").split(b"\0"):
        if record:
            header, path = record.split(b"\t", 1)
            mode, kind, blob = header.decode().split()
            if kind == "blob":
                tree[path.decode()] = (mode, blob)
    blobs = {path: git("cat-file", "blob", blob) for path, (_, blob) in tree.items()}
    modules = {}
    for path in tree:
        if path.endswith(".py"):
            module = path[:-3].replace("/", ".")
            modules[module.removesuffix(".__init__")] = path
    pending = list(ENTRYPOINTS)
    # These are registry-discovered packages as well as ordinary imports.
    pending += [name for name in modules if name.startswith(("app.agent.", "app.assistant."))]
    selected = set()
    while pending:
        module = pending.pop()
        if module not in modules or module in selected:
            continue
        selected.add(module)
        path = modules[module]
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        for index in range(1, len(module.split("."))):
            pending.append(".".join(module.split(".")[:index]))
        for node in ast.walk(ast.parse(blobs[path], filename=path)):
            if isinstance(node, ast.Import):
                pending += [alias.name for alias in node.names if alias.name.startswith("app")]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = package.split(".")[:len(package.split(".")) - node.level + 1]
                    base = ".".join(prefix + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                if base == "app" or base.startswith("app."):
                    pending.append(base)
                    pending += [base + "." + alias.name for alias in node.names]
            elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str) and value.startswith("app."):
                    pending.append(value)
    paths = {modules[name] for name in selected}
    paths.add("app/assistant/asc_knowledge/ASC_KNOWLEDGE_MAP.md")
    paths.add("requirements.txt")
    patterns = [r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", r"(?<![\w])sk-(?:proj-)?[A-Za-z0-9_-]{25,}", r"github_pat_[A-Za-z0-9_]{30,}"]
    destination = ROOT / "pe_platform" / "upstream"
    records = []
    for path in sorted(paths):
        content = blobs[path]
        if any(re.search(pattern, content.decode("utf-8")) for pattern in patterns):
            raise SystemExit("Review credential-like source content before copying: " + path)
        target = destination / path
        if target.exists() and target.read_bytes() != content:
            raise SystemExit("Refusing to overwrite an existing different source file: " + path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        records.append({"path": path, "blob": tree[path][1], "sha256": hashlib.sha256(content).hexdigest(), "adaptation": "none"})
    manifest = {
        "source_repository": "PE Workflow Builder",
        "source_revision": revision,
        "scope": "Cash-flow and governed assistant dependency graph; other catalog APIs/UI are not exposed",
        "method": "Git blobs plus conservative AST/registry-package dependency closure",
        "entrypoints": list(ENTRYPOINTS),
        "files": records,
        "local_adapters": "pe_platform/backend and pe_platform/frontend",
        "limitations": ["Source hashes establish origin, not runtime parity or live-provider validation."],
    }
    (ROOT / "pe_platform" / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Copied {len(records)} source files at {revision}; {sum(len(blobs[p]) for p in paths):,} bytes")


if __name__ == "__main__":
    main()
