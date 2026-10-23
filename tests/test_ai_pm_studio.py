"""Offline contract tests and a real Hermes/HTTP-adapter smoke test.

The local endpoint returns fixtures, NOT real model reasoning. No provider
credentials or external requests are needed for the smoke test.
"""

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from examples.ai_pm_studio.studio import (
    WorkflowError,
    checked_entries,
    initialize,
    parse_output,
    run_workflow,
    validate_brief,
)


ROOT = Path(__file__).resolve().parents[1]
BRIEF = {
    "idea": "个人阅读收件箱",
    "constraints": ["命令行"],
    "evidence": [{"id": "E1", "text": "尚无真实使用数据"}],
}


def answer(role):
    result = {
        "summary": f"{role}: test fixture, not model reasoning",
        "proposal": "先用手动清单做一周验证",
        "risks": ["使用频次未知"],
        "next_experiment": "记录一周整理耗时；没有重复需求则停止",
        "claims": [
            {
                "id": {
                    "critic": "C1",
                    "designer": "D1",
                    "verifier": "V1",
                    "baseline": "B1",
                }[role],
                "text": "尚无真实使用数据",
                "kind": "provided",
                "source_ids": ["E1"],
            }
        ],
    }
    if role in {"verifier", "baseline"}:
        result["verdict"] = "revise"
    if role == "verifier":
        result["claim_checks"] = [
            {
                "claim_id": cid,
                "status": "supported",
                "reason": "E1 explicitly says no usage data",
            }
            for cid in ("C1", "D1")
        ]
    return result


def entry(index, role):
    return {
        "task_index": index,
        "status": "completed",
        "exit_reason": "completed",
        "summary": json.dumps(answer(role)),
        "api_calls": 1,
        "tokens": {"input": 100, "output": 40},
        "duration_seconds": 0.1,
    }


class Backend:
    metadata = {"engine": "fixture"}

    def __init__(self):
        self.batches = []

    def skill(self, role):
        return f"Fixture instructions for {role}"

    def delegate(self, tasks):
        self.batches.append(tasks)
        roles = [re.search(r"PM_STUDIO_ROLE=(\w+)", t["goal"]).group(1) for t in tasks]
        # Completion order need not match input order.
        return {
            "results": list(reversed([entry(i, role) for i, role in enumerate(roles)]))
        }


def test_parallel_handoff_and_auditable_artifacts(tmp_path):
    backend = Backend()
    out = tmp_path / "run"
    results = run_workflow(BRIEF, backend, out)
    assert [len(x) for x in backend.batches] == [2, 1]
    review_input = backend.batches[1][0]["context"]
    assert results["critic"]["summary"] in review_input
    assert results["designer"]["summary"] in review_input
    assert json.loads((out / "run.json").read_text())["status"] == "completed"
    report = (out / "report.md").read_text()
    assert "C1 [supported]" in report
    assert "D1 [provided; sources: E1]" in report
    assert (out / "batch-0-result.json").exists()
    with pytest.raises(FileExistsError):
        run_workflow(BRIEF, backend, out)


@pytest.mark.parametrize(
    "status,exit_reason",
    [
        ("timeout", "timeout"),
        ("completed", "max_iterations"),
        ("interrupted", "interrupted"),
        ("error", "error"),
    ],
)
def test_failure_never_runs_verifier_or_emits_final(tmp_path, status, exit_reason):
    backend = Backend()
    calls = []

    def fail(tasks):
        calls.append(tasks)
        item = entry(0, "critic")
        item.update(status=status, exit_reason=exit_reason)
        return {"results": [item, entry(1, "designer")]}

    backend.delegate = fail
    out = tmp_path / "run"
    with pytest.raises(WorkflowError):
        run_workflow(BRIEF, backend, out)
    assert len(calls) == 1
    assert json.loads((out / "run.json").read_text())["status"] == "failed"
    assert (out / "batch-0-result.json").exists()
    assert not (out / "report.md").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_source",
        "missing_source",
        "duplicate_claim",
        "missing_check",
        "duplicate_check",
        "invented_check",
        "non_json",
        "empty_claims",
    ],
)
def test_output_contract_rejects_unusable_handoffs(mutation):
    role = "verifier" if "check" in mutation else "critic"
    data = answer(role)
    if mutation == "unknown_source":
        data["claims"][0]["source_ids"] = ["invented"]
    elif mutation == "missing_source":
        data["claims"][0]["source_ids"] = []
    elif mutation == "duplicate_claim":
        data["claims"] *= 2
    elif mutation == "missing_check":
        data["claim_checks"].pop()
    elif mutation == "duplicate_check":
        data["claim_checks"] *= 2
    elif mutation == "invented_check":
        data["claim_checks"][0]["claim_id"] = "C999"
    elif mutation == "empty_claims":
        data["claims"] = []
    raw = "Looks good!" if mutation == "non_json" else json.dumps(data)
    with pytest.raises(WorkflowError):
        parse_output(raw, role, {"E1"}, ["C1", "D1"])


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "failure"},
        {"results": []},
        {"results": [entry(0, "critic"), entry(0, "designer")]},
    ],
)
def test_invalid_batch_rejected(payload):
    with pytest.raises(WorkflowError):
        checked_entries(payload, 2)


def test_single_baseline_has_one_call(tmp_path):
    backend = Backend()
    results = run_workflow(BRIEF, backend, tmp_path / "run", "single")
    assert list(results) == ["baseline"]
    assert len(backend.batches) == 1
    assert len(backend.batches[0]) == 1


def test_initialization_does_not_overwrite_a_profile(tmp_path):
    home = tmp_path / "studio"
    initialize(home)
    (home / "config.yaml").write_text("user's settings")
    with pytest.raises(FileExistsError):
        initialize(home)
    assert (home / "config.yaml").read_text() == "user's settings"


def test_brief_validation():
    assert validate_brief({**BRIEF, "evidence": []}) == set()
    with pytest.raises(WorkflowError):
        validate_brief({**BRIEF, "evidence": BRIEF["evidence"] * 2})
    with pytest.raises(WorkflowError):
        validate_brief({**BRIEF, "idea": " "})


@pytest.mark.parametrize(
    "mode,malformed", [("multi", False), ("single", False), ("multi", True)]
)
def test_real_hermes_via_local_http(tmp_path, mode, malformed):
    """No mocking of AIAgent, delegate_task, skill_view or SessionDB."""
    requests = []
    barrier = threading.Barrier(2)
    overlapped = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/v1/chat/completions":
                # Hermes probes local model metadata (e.g. Ollama /api/show).
                self.send_error(404)
                return
            text = json.dumps(body["messages"], ensure_ascii=False)
            role = re.search(r"PM_STUDIO_ROLE=(\w+)", text).group(1)
            requests.append((role, body))
            if role in {"critic", "designer"}:
                try:
                    barrier.wait(timeout=15)
                    overlapped.append(role)
                except threading.BrokenBarrierError:
                    pass
            content = (
                "invalid JSON"
                if malformed and role == "critic"
                else json.dumps(answer(role), ensure_ascii=False)
            )
            response = {
                "id": f"chatcmpl-{role}",
                "object": "chat.completion",
                "created": 1,
                "model": "pm-fixture",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "total_tokens": 140,
                },
            }
            if body.get("stream"):
                chunk = {
                    **response,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": content},
                            "finish_reason": None,
                        }
                    ],
                }
                end = {
                    **response,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                data = (
                    "data: "
                    + json.dumps(chunk)
                    + "\n\ndata: "
                    + json.dumps(end)
                    + "\n\ndata: [DONE]\n\n"
                ).encode()
            else:
                data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/event-stream" if body.get("stream") else "application/json",
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "profile"
    initialize(home)
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(BRIEF), encoding="utf-8")
    env = {
        **os.environ,
        "AI_PM_STUDIO_API_KEY": "local-fixture-only",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "examples.ai_pm_studio",
                "run",
                "--home",
                str(home),
                "--brief",
                str(brief_path),
                "--model",
                "pm-fixture",
                "--base-url",
                f"http://127.0.0.1:{server.server_port}/v1",
                "--mode",
                mode,
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert proc.returncode == (1 if malformed else 0), proc.stdout + proc.stderr
    roles = [r for r, _ in requests]
    assert sorted(roles) == (
        ["critic", "designer"]
        if malformed
        else ["critic", "designer", "verifier"]
        if mode == "multi"
        else ["baseline"]
    )
    if mode == "multi":
        assert set(overlapped) == {"critic", "designer"}
    # An empty allow-list must stay empty through actual child construction.
    assert all(not body.get("tools") for _, body in requests)
    assert all(
        "Product " in json.dumps(b["messages"])
        or "Single-agent baseline" in json.dumps(b["messages"])
        for _, b in requests
    )
    run_dir = next((home / "studio" / "runs").iterdir())
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["status"] == ("failed" if malformed else "completed")
    assert (run_dir / "report.md").exists() is not malformed
    assert (home / "state.db").exists()
    with sqlite3.connect(home / "state.db") as connection:
        saved = connection.execute(
            "SELECT COUNT(DISTINCT session_id) FROM messages WHERE role = 'assistant'"
        ).fetchone()[0]
    assert saved == len(roles), "each real child conversation must be persisted"
    # Nothing generated by child assessments is promoted to permanent memory.
    assert not list((home / "memories").glob("*.md"))
    assert "local-fixture-only" not in (run_dir / "run.json").read_text()
