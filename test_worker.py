"""Tests of worker.run_job (no RunPod SDK needed): real bash, real tar archives, in a temporary directory."""
import base64
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest

import worker


def tar_b64(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


class RunJob(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)

    def run_job(self, job_input):
        items = list(worker.run_job(job_input, self.root.name))
        for item in items:
            self.assertLess(len(json.dumps(item)), 1_000_000)
        self.assertEqual(items[-1]["type"], "end")
        self.assertEqual(os.listdir(self.root.name), [])  # the scratch directory is gone
        return items

    def log(self, items):
        return "".join(i["data"] for i in items if i["type"] == "log")

    def test_script_runs_in_workspace_with_env_and_exit_code(self):
        items = self.run_job({"archive": tar_b64({"src/a.txt": b"hello"}), "env": {"GREETING": "hi"},
                              "script": "cat src/a.txt; echo; echo $GREETING >&2; exit 3"})
        self.assertEqual(self.log(items), "hello\nhi\n")
        self.assertEqual(items[-1]["code"], 3)
        self.assertFalse(items[-1]["timed_out"])

    def test_signal_exit_code(self):
        items = self.run_job({"script": "kill -9 $$"})
        self.assertEqual(items[-1]["code"], 137)

    def test_timeout_kills_the_group(self):
        items = self.run_job({"script": "echo start; sleep 30 & sleep 30; echo never", "timeout": 1})
        self.assertTrue(items[-1]["timed_out"])
        self.assertIn("start", self.log(items))
        self.assertNotIn("never", self.log(items))
        self.assertLess(items[-1]["seconds"], 15)

    def test_long_output_is_chunked(self):
        items = self.run_job({"script": "head -c 600000 /dev/zero | tr '\\0' 'x'; echo"})
        self.assertEqual(len(self.log(items)), 600001)
        self.assertGreater(sum(1 for i in items if i["type"] == "log"), 1)

    def test_artifacts_round_trip(self):
        script = "mkdir -p out/sub; echo one > out/a.txt; echo two > out/sub/b.txt; head -c 1500000 /dev/urandom > out/big.bin; exit 1"
        items = self.run_job({"script": script, "artifacts": ["out"]})
        head = next(i for i in items if i["type"] == "artifacts")
        chunks = [i for i in items if i["type"] == "chunk"]
        self.assertEqual(head["chunks"], len(chunks))
        self.assertGreater(len(chunks), 1)
        data = b"".join(base64.b64decode(c["data"]) for c in sorted(chunks, key=lambda c: c["index"]))
        self.assertEqual(len(data), head["bytes"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), head["sha256"])
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            self.assertEqual(sorted(t.getnames()), ["out/a.txt", "out/big.bin", "out/sub/b.txt"])
        self.assertEqual(head["files"], 3)
        self.assertEqual(items[-1]["code"], 1)  # the script's code, artifacts or not

    def test_artifact_glob_and_limit(self):
        items = self.run_job({"script": "echo a > x.log; echo b > y.txt", "artifacts": ["*.log"]})
        self.assertEqual(next(i for i in items if i["type"] == "artifacts")["files"], 1)
        items = self.run_job({"script": "head -c 100000 /dev/urandom > r.bin", "artifacts": ["r.bin"],
                              "artifact_limit": 1000})
        self.assertIn("artifact_limit", next(i for i in items if i["type"] == "error")["data"])
        self.assertEqual(items[-1]["code"], 125)

    def test_bad_inputs(self):
        for bad, words in [({}, "script"), ({"script": "true", "env": {"A": 1}}, "env"),
                           ({"script": "true", "artifacts": ["../x"]}, "inside"),
                           ({"script": "true", "archive": "%%%"}, "base64"),
                           ({"script": "true", "archive_url": "http://example.invalid/a.tgz"}, "https")]:
            items = self.run_job(bad)
            self.assertIn(words, next(i for i in items if i["type"] == "error")["data"])
            self.assertEqual(items[-1]["code"], 125)

    def test_archive_escape_rejected(self):
        items = self.run_job({"script": "true", "archive": tar_b64({"../evil": b"x"})})
        self.assertEqual(items[-1]["code"], 125)
        self.assertTrue(any(i["type"] == "error" for i in items))


if __name__ == "__main__":
    unittest.main()
