"""LLM providers that run on your Claude / ChatGPT *subscription* instead of API keys.

They shell out to the locally installed, logged-in CLIs:

  provider: claude_cli  ->  `claude -p`   (Claude Code; Pro/Max plan)
  provider: codex_cli   ->  `codex exec`  (OpenAI Codex CLI; ChatGPT plan)

No API tokens are billed, but calls count against the plan's usage limits, and
each call takes a few seconds (a full CLI start-up), so keep max_candidates modest.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess  # nosec B404
import tempfile
from pathlib import Path

from ..models.candidates import CandidateMoment
from .base import ClipPick
from .prompts import CLIP_PICK_JSON_SCHEMA, build_candidate_prompt, get_system_prompt

log = logging.getLogger(__name__)

# Env vars that would make the CLIs bill an API key instead of the subscription.
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _API_KEY_VARS}


def _schema_json() -> str:
    schema = CLIP_PICK_JSON_SCHEMA
    return json.dumps(schema if isinstance(schema, dict) else json.loads(schema))


class ClaudeCLIProvider:
    """Uses `claude -p` (Claude Code) logged in with a Claude subscription."""

    def __init__(self, *, model: str = "sonnet", prompt_version: str = "v1", timeout_s: int = 180):
        self._bin = shutil.which("claude")
        if not self._bin:
            raise RuntimeError("`claude` CLI not found. Install Claude Code and run `claude` once to log in.")
        self._model = model
        self._system = get_system_prompt(prompt_version)
        self._timeout = timeout_s

    def pick(
        self, transcript_window: str, candidate: CandidateMoment, stream_context: str
    ) -> ClipPick | None:
        prompt = f"{stream_context}\n\n{build_candidate_prompt(transcript_window, candidate)}"
        cmd = [
            self._bin,
            "-p",
            "--model", self._model,
            "--output-format", "json",
            "--system-prompt", self._system,
            "--json-schema", _schema_json(),
            "--tools", "",              # pure text task: no tools
            "--setting-sources", "",    # ignore user/project settings, hooks, CLAUDE.md
            "--strict-mcp-config",      # no MCP servers
            "--no-session-persistence",
        ]
        try:
            out = subprocess.run(  # nosec B603 — fixed binary, prompt goes via stdin
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=_clean_env(),
                cwd=tempfile.gettempdir(),
            )
            if out.returncode != 0:
                raise RuntimeError(out.stderr.strip()[-400:] or out.stdout.strip()[-400:])
            envelope = json.loads(out.stdout)
            if envelope.get("is_error"):
                raise RuntimeError(str(envelope.get("result"))[:400])
            structured = envelope.get("structured_output")
            if isinstance(structured, dict):
                return ClipPick.from_dict(structured)
            return ClipPick.from_json(envelope.get("result", ""))
        except Exception as exc:
            log.warning("llm_failed provider=claude_cli candidate=%.1f error=%s", candidate.t_center, exc)
            return None


class CodexCLIProvider:
    """Uses `codex exec` (OpenAI Codex CLI) logged in with a ChatGPT subscription."""

    def __init__(self, *, model: str = "", prompt_version: str = "v1", timeout_s: int = 240):
        self._bin = shutil.which("codex")
        if not self._bin:
            raise RuntimeError("`codex` CLI not found. Install it and run `codex login`.")
        self._model = model
        self._system = get_system_prompt(prompt_version)
        self._timeout = timeout_s

    def pick(
        self, transcript_window: str, candidate: CandidateMoment, stream_context: str
    ) -> ClipPick | None:
        prompt = (
            f"{self._system}\n\n{stream_context}\n\n"
            f"{build_candidate_prompt(transcript_window, candidate)}\n\n"
            "Answer ONLY with the JSON object. Do not run any commands."
        )
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.json"
            schema_path.write_text(_schema_json(), encoding="utf-8")
            last = Path(tmp) / "last.txt"
            cmd = [
                self._bin, "exec", "--skip-git-repo-check", "--ephemeral",
                "--sandbox", "read-only",
                "--output-schema", str(schema_path),
                "-o", str(last),
            ]
            if self._model:
                cmd += ["-m", self._model]
            cmd.append("-")  # read prompt from stdin
            try:
                out = subprocess.run(  # nosec B603
                    cmd, input=prompt, capture_output=True, text=True,
                    timeout=self._timeout, env=_clean_env(), cwd=tmp,
                )
                if out.returncode != 0 or not last.exists():
                    raise RuntimeError(out.stderr.strip()[-400:])
                return ClipPick.from_json(last.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("llm_failed provider=codex_cli candidate=%.1f error=%s", candidate.t_center, exc)
                return None


def ask_json(prompt: str, schema: dict, provider: str = "codex_cli", model: str = "") -> dict:
    """One-off structured JSON answer from the subscription CLIs (no API billing)."""
    if provider == "claude_cli":
        bin_ = shutil.which("claude")
        cmd = [bin_, "-p", "--model", model or "sonnet", "--output-format", "json",
               "--json-schema", json.dumps(schema), "--tools", "", "--setting-sources", "",
               "--strict-mcp-config", "--no-session-persistence"]
        out = subprocess.run(cmd, input=prompt, capture_output=True, text=True,  # nosec B603
                             timeout=180, env=_clean_env(), cwd=tempfile.gettempdir())
        env = json.loads(out.stdout)
        if env.get("is_error"):
            raise RuntimeError(str(env.get("result"))[:300])
        return env.get("structured_output") or json.loads(env.get("result", "{}"))
    with tempfile.TemporaryDirectory() as tmp:
        sp = Path(tmp) / "schema.json"
        sp.write_text(json.dumps(schema), encoding="utf-8")
        last = Path(tmp) / "last.txt"
        cmd = [shutil.which("codex"), "exec", "--skip-git-repo-check", "--ephemeral",
               "--sandbox", "read-only", "--output-schema", str(sp), "-o", str(last)]
        if model:
            cmd += ["-m", model]
        cmd.append("-")
        out = subprocess.run(cmd, input=prompt + "\n\nAnswer ONLY with the JSON object.",  # nosec B603
                             capture_output=True, text=True, timeout=240, env=_clean_env(), cwd=tmp)
        if out.returncode != 0 or not last.exists():
            raise RuntimeError(out.stderr[-300:])
        txt = last.read_text(encoding="utf-8").strip().strip("`")
        return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
