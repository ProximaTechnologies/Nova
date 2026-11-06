#!/usr/bin/env python3
import json
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer


DESIRED_STATES = {
    "node-docker-enabled": {
        "version": "docker-enabled-1",
        "features": {
            "containerMonitor": True,
            "dockerLogs": False,
        },
        "actions": [],
    },
    "node-docker-disabled": {
        "version": "docker-disabled-1",
        "features": {
            "containerMonitor": False,
            "dockerLogs": False,
        },
        "actions": [],
    },
    "node-systemctl": {
        "version": "systemctl-1",
        "actions": [
            {
                "id": "restart-alloy",
                "tool": "systemctl",
                "args": ["restart", "alloy.service"],
                "run_once": True,
                "timeout": "30s",
            }
        ],
    },
    "node-alloy-http": {
        "version": "alloy-http-1",
        "alloy": {
            "http": {
                "base_url": "http://127.0.0.1:18081",
                "reload": True,
                "ready": True,
                "healthy": True,
                "timeout": "5s",
            }
        },
        "actions": [],
    },
    "node-systemctl-reject": {
        "version": "systemctl-reject-1",
        "actions": [
            {
                "id": "enable-alloy",
                "tool": "systemctl",
                "args": ["enable", "alloy.service"],
                "run_once": True,
                "timeout": "30s",
            }
        ],
    },
    "node-default-deny": {
        "version": "default-deny-1",
        "actions": [
            {
                "id": "default-deny-systemctl",
                "tool": "systemctl",
                "args": ["restart", "alloy.service"],
                "run_once": True,
                "timeout": "30s",
            }
        ],
    },
    "node-kubectl": {
        "version": "kubectl-1",
        "actions": [
            {
                "id": "kubectl-rejected",
                "tool": "kubectl",
                "args": ["get", "pods"],
                "run_once": True,
                "timeout": "30s",
            }
        ],
    },
    "node-helm": {
        "version": "helm-1",
        "actions": [
            {
                "id": "helm-rejected",
                "tool": "helm",
                "args": ["list"],
                "run_once": True,
                "timeout": "30s",
            }
        ],
    },
    "node-argocd": {
        "version": "argocd-1",
        "actions": [
            {
                "id": "argocd-rejected",
                "tool": "argocd",
                "args": ["app", "list"],
                "run_once": True,
                "timeout": "30s",
            }
        ],
    },
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        prefix = "/agent/supervisor/desired/"
        if path.startswith(prefix):
            node_id = path.removeprefix(prefix)
            desired = DESIRED_STATES.get(node_id)
            if desired is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(desired).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/agent/version/") or path.startswith("/agent/supervisor/version/"):
            self.send_response(204)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        print("fake-nova:", fmt % args, flush=True)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 18080), Handler).serve_forever()
