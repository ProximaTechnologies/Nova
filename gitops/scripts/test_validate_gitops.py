"""Regression tests for GitOps wiring; never contacts a cluster."""
from pathlib import Path
import shutil
import tempfile
import unittest

import validate_gitops as validation


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "gitops"
        shutil.copytree(Path(__file__).resolve().parent.parent, self.root)

    def test_repository_is_valid(self):
        self.assertEqual(validation.validate(self.root), [])

    def test_missing_referenced_value_file(self):
        (self.root / "env/dev/apisix.yaml").unlink()
        self.assertTrue(any("missing values file" in e for e in validation.validate(self.root)))

    def test_undefined_values_reference(self):
        path = self.root / "appsets/apisix.yaml"
        path.write_text(path.read_text().replace("$values/", "$unknown/"))
        self.assertTrue(any("undefined values reference" in e for e in validation.validate(self.root)))

    def test_missing_overlay(self):
        shutil.rmtree(self.root / "overlays/nova/dev")
        self.assertTrue(any("missing source directory" in e for e in validation.validate(self.root)))

    def test_source_repo_permission(self):
        path = self.root / "appsets/apisix.yaml"
        path.write_text(path.read_text().replace("https://apache.github.io/apisix-helm-chart", "https://unapproved.example/charts"))
        self.assertTrue(any("not allowed" in e for e in validation.validate(self.root)))

    def test_duplicate_yaml_key(self):
        path = self.root / "env/dev/apisix.yaml"
        path.write_text("replicaCount: 1\nreplicaCount: 2\n")
        self.assertTrue(any("duplicate YAML key" in e for e in validation.validate(self.root)))

    def test_active_placeholder(self):
        path = self.root / "env/dev/apisix.yaml"
        path.write_text(path.read_text() + "\nexample: REPLACEME_TEST\n")
        self.assertTrue(any("placeholder" in e for e in validation.validate(self.root)))

    def test_discovery_uses_actual_generator_pairs(self):
        path = self.root / "appsets/apisix.yaml"
        path.write_text(path.read_text().replace("env: dev", "env: stage"))
        pairs = {(doc["metadata"]["name"], element["env"])
                 for _, doc, element in validation.applications(self.root)}
        self.assertIn(("apisix", "stage"), pairs)
        self.assertNotIn(("apisix", "dev"), pairs)
        self.assertIn(("nova", "dev"), pairs)
        self.assertNotIn(("nova", "stage"), pairs)


if __name__ == "__main__":
    unittest.main()
