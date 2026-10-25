# runpod-gpu-worker

A generic [RunPod Serverless](https://docs.runpod.io/serverless/overview) worker for GPU build, test and
render jobs. A job carries a workspace (a tar.gz) and a bash script; the worker unpacks the workspace,
runs the script in it, streams the log back as it is written, and can return files the script produced.
It knows nothing about the project it builds: everything project-specific is in the request.

The image: CUDA 13.4 (`nvidia/cuda:13.4.3-devel-ubuntu24.04`, pinned by digest), CMake, Ninja,
g++-14, Python 3, git. Idle workers cost nothing (Flex workers scale to zero).

## Protocol (version 1)

Input (`POST /v2/<endpoint>/run`, `{"input": {...}, "policy": {"executionTimeout": <ms>}}`):

| field | | |
|---|---|---|
| `script` | required | bash script; its working directory is the unpacked workspace |
| `archive` | optional | the workspace as a base64 tar.gz (the request may be at most 10 MB) |
| `archive_url` | optional | or an https URL of one |
| `env` | optional | `{name: value}` added to the script's environment |
| `timeout` | optional | seconds before the script's process group is killed (default 3300) |
| `artifacts` | optional | glob patterns relative to the workspace (`**` crosses directories), collected after the script, whatever its exit code |
| `artifact_limit` | optional | the largest artifact archive in bytes (default 256 MiB) |

Output, read from `/stream/<job id>` (every item is below RunPod's 1 MB chunk limit):

| item | |
|---|---|
| `{"type": "log", "data": text}` | stdout and stderr of the script, in order |
| `{"type": "artifacts", "chunks": n, "bytes": b, "sha256": h, "files": f}` | then `n` items `{"type": "chunk", "index": i, "data": base64}`: the artifacts' tar.gz |
| `{"type": "error", "data": text}` | the worker's own failure (bad input, archive, artifacts) |
| `{"type": "end", "code": c, "seconds": s, "timed_out": bool}` | always last; `c` is the script's exit status (128 + N for signal N), 125 if the worker failed |

## Endpoint settings

- Build from this repository (RunPod's GitHub integration), Dockerfile at the root. A push does not
  update the endpoint: **a new GitHub release does** (RunPod then rebuilds the image).
- GPUs: the 16 GB and 24 GB classes (Ampere sm_86 and Ada sm_89; code built for sm_86 runs on both).
- **Minimum CUDA version 13.0** (`minCudaVersion`). Do not use the "allowed CUDA versions" list: it
  matches exactly and hides hosts with newer drivers.
- Active workers 0, max workers 1–2, idle timeout 5 s, FlashBoot on.
- Execution timeout: 3600 s (a request may set a lower one in `policy.executionTimeout`).

## Security

Anyone holding an API key for the endpoint can run arbitrary code on its workers: the key is the
boundary. Use a key restricted to this endpoint where RunPod allows it, keep it in CI secrets only.

## Layout

`worker.py` is the job logic; `handler.py` only hands jobs to it and starts the RunPod worker
(RunPod's GitHub integration requires `runpod.serverless.start()` at the top level of `handler.py`).

## Tests

`python3 -m unittest test_worker` (Python ≥ 3.12, no RunPod SDK needed).

## License

MIT, see [LICENSE](LICENSE).
