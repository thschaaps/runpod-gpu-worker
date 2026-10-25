"""The RunPod entry point: the job logic is in worker.py (testable without the RunPod SDK).

RunPod's GitHub integration looks for runpod.serverless.start() in this file, at the top level.
"""
import os

import runpod

from worker import run_job


def handler(job):
    yield from run_job(job.get("input"), os.environ.get("WORK_ROOT"))


runpod.serverless.start({"handler": handler, "return_aggregate_stream": False})
