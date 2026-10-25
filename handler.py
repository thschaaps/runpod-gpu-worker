"""A generic RunPod Serverless worker: unpack a workspace, run a bash script in it, stream the log back.

The worker knows nothing about the project it builds. A job's input (protocol version 1):

  script         bash script to run (required); its working directory is the unpacked workspace
  archive        the workspace as a base64 tar.gz (optional)
  archive_url    or an https URL of one (optional; for workspaces beyond the request size limit)
  env            {name: value} added to the script's environment (optional)
  timeout        seconds before the script's process group is killed (optional, default 3300)
  artifacts      glob patterns relative to the workspace, collected after the script ends,
                 whatever its exit code (optional; "**" matches across directories)
  artifact_limit the largest artifact archive in bytes (optional, default 256 MiB)

The job streams dicts (each well under RunPod's 1 MB limit per streamed chunk):

  {"type": "log", "data": text}                       the script's stdout and stderr, in order
  {"type": "artifacts", "chunks": n, "bytes": b, "sha256": h, "files": f}
  {"type": "chunk", "index": i, "data": base64}       n chunks of the artifacts' tar.gz
  {"type": "error", "data": text}                     the worker's own failure (bad input, ...)
  {"type": "end", "code": c, "seconds": s, "timed_out": bool}   always last

code is the script's exit status (128 + N for a signal N), or 125 if the worker failed before or
around it. Anyone holding an API key for the endpoint can run code here: that key is the boundary.
"""
import base64
import glob
import hashlib
import os
import queue
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request

PROTOCOL = 1
LOG_CHUNK_CHARS = 128 * 1024   # JSON escapes a character into at most 6, so a log item stays < 1 MB
ART_CHUNK_BYTES = 512 * 1024   # 512 KiB raw is ~700 KB of base64
MAX_ARCHIVE_BYTES = 2 << 30
FLUSH_SECONDS = 1.0


class JobError(Exception):
    pass


def fetch_archive(url, dest):
    if not url.startswith("https://"):
        raise JobError("archive_url must be https")
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
        total = 0
        while True:
            block = r.read(1 << 20)
            if not block:
                break
            total += len(block)
            if total > MAX_ARCHIVE_BYTES:
                raise JobError("archive_url: more than %d bytes" % MAX_ARCHIVE_BYTES)
            f.write(block)


def unpack(job_input, workdir, scratch):
    path = os.path.join(scratch, "workspace.tar.gz")
    if job_input.get("archive"):
        try:
            data = base64.b64decode(job_input["archive"], validate=True)
        except ValueError as e:
            raise JobError("archive: not base64: %s" % e)
        with open(path, "wb") as f:
            f.write(data)
    elif job_input.get("archive_url"):
        fetch_archive(job_input["archive_url"], path)
    else:
        return
    try:
        with tarfile.open(path, "r:*") as t:
            t.extractall(workdir, filter="data")   # no absolute paths, no escapes, no devices
    except (tarfile.TarError, OSError) as e:
        raise JobError("archive: %s" % e)


def check_input(job_input):
    if not isinstance(job_input, dict):
        raise JobError("input must be an object")
    if not isinstance(job_input.get("script"), str) or not job_input["script"].strip():
        raise JobError("input.script: a non-empty string is required")
    env = job_input.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise JobError("input.env must map strings to strings")
    arts = job_input.get("artifacts") or []
    if not isinstance(arts, list) or not all(isinstance(a, str) for a in arts):
        raise JobError("input.artifacts must be a list of strings")
    for a in arts:
        if os.path.isabs(a) or ".." in a.split("/"):
            raise JobError("input.artifacts: %r must stay inside the workspace" % a)
    try:
        timeout = float(job_input.get("timeout", 3300))
        limit = int(job_input.get("artifact_limit", 256 << 20))
    except (TypeError, ValueError):
        raise JobError("input.timeout and input.artifact_limit must be numbers")
    return env, arts, timeout, limit


def run_script(script_path, workdir, env, timeout):
    """Yields log text as it comes; returns (exit code, timed out) through StopIteration.value."""
    p = subprocess.Popen(["/bin/bash", script_path], cwd=workdir, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    lines = queue.Queue()

    def reader():
        for raw in iter(lambda: p.stdout.readline(LOG_CHUNK_CHARS), b""):
            lines.put(raw.decode("utf-8", "replace"))
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.monotonic() + timeout
    timed_out = False
    pending, last = [], time.monotonic()
    done = False
    while not done:
        try:
            item = lines.get(timeout=0.2)
            if item is None:
                done = True
            else:
                pending.append(item)
        except queue.Empty:
            pass
        size = sum(len(s) for s in pending)
        if pending and (done or size >= LOG_CHUNK_CHARS // 2 or time.monotonic() - last >= FLUSH_SECONDS):
            text = "".join(pending)
            pending = []
            for i in range(0, len(text), LOG_CHUNK_CHARS):
                yield text[i:i + LOG_CHUNK_CHARS]
            last = time.monotonic()
        if not timed_out and time.monotonic() > deadline:
            timed_out = True
            yield "\n[worker] timeout after %.0f s: killing the script\n" % timeout
            kill_group(p)
    rc = p.wait()
    p.stdout.close()
    return (128 - rc if rc < 0 else rc), timed_out


def kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def collect_artifacts(patterns, workdir, scratch, limit):
    files = set()
    for pattern in patterns:
        for match in glob.glob(pattern, root_dir=workdir, recursive=True):
            full = os.path.join(workdir, match)
            if os.path.isdir(full):
                for dirpath, _, names in os.walk(full):
                    files.update(os.path.relpath(os.path.join(dirpath, n), workdir) for n in names)
            elif os.path.isfile(full):
                files.add(match)
    if not files:
        return None
    path = os.path.join(scratch, "artifacts.tar.gz")
    with tarfile.open(path, "w:gz") as t:
        for rel in sorted(files):
            t.add(os.path.join(workdir, rel), arcname=rel, recursive=False)
    size = os.path.getsize(path)
    if size > limit:
        raise JobError("artifacts: %d bytes, more than artifact_limit %d" % (size, limit))
    return path, len(files)


def stream_artifacts(path, nfiles):
    size = os.path.getsize(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    chunks = max(1, -(-size // ART_CHUNK_BYTES))
    yield {"type": "artifacts", "chunks": chunks, "bytes": size, "sha256": h.hexdigest(), "files": nfiles}
    with open(path, "rb") as f:
        for i in range(chunks):
            yield {"type": "chunk", "index": i, "data": base64.b64encode(f.read(ART_CHUNK_BYTES)).decode("ascii")}


def run_job(job_input, work_root=None):
    start = time.monotonic()
    code, timed_out = 125, False
    scratch = tempfile.mkdtemp(prefix="job-", dir=work_root)
    workdir = os.path.join(scratch, "ws")
    os.mkdir(workdir)
    try:
        env_add, arts, timeout, limit = check_input(job_input)
        unpack(job_input, workdir, scratch)
        script = os.path.join(scratch, "job.sh")
        with open(script, "w") as f:
            f.write(job_input["script"])
        env = dict(os.environ)
        env.update(env_add)
        gen = run_script(script, workdir, env, timeout)
        while True:
            try:
                yield {"type": "log", "data": next(gen)}
            except StopIteration as stop:
                code, timed_out = stop.value
                break
        if arts:
            try:
                found = collect_artifacts(arts, workdir, scratch, limit)
                if found:
                    yield from stream_artifacts(*found)
                else:
                    yield {"type": "log", "data": "[worker] no file matched the artifact patterns\n"}
            except (JobError, OSError) as e:
                yield {"type": "error", "data": str(e)}
                code = code or 125
    except (JobError, OSError) as e:
        yield {"type": "error", "data": str(e)}
        code = 125
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    yield {"type": "end", "code": code, "seconds": round(time.monotonic() - start, 1), "timed_out": timed_out}


def handler(job):
    yield from run_job(job.get("input"), os.environ.get("WORK_ROOT"))


if __name__ == "__main__":
    import runpod
    runpod.serverless.start({"handler": handler, "return_aggregate_stream": False})
