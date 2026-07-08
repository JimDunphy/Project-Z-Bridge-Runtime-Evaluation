#!/usr/bin/env python3
"""
Project Z Bridge host AI runner.

Purpose:
- Execute locally installed provider CLIs (codex/claude/agy) on the host.
- Expose a minimal localhost HTTP API for the bridge container.
- Reuse existing host-side provider authentication/session state.

Security:
- By default, no token is required (low-friction local/family mode).
- If BRIDGE_AI_RUNNER_TOKEN is set, requests must include it via:
  - X-Bridge-Runner-Token: <token>
  - or Authorization: Bearer <token>
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


RUNNER_VERSION = "0.1.0"
DEFAULT_TIMEOUT_MS = int(os.getenv("BRIDGE_AI_RUNNER_DEFAULT_TIMEOUT_MS", "90000"))
MAX_TIMEOUT_MS = int(os.getenv("BRIDGE_AI_RUNNER_MAX_TIMEOUT_MS", "600000"))
RUNNER_BIND = os.getenv("BRIDGE_AI_RUNNER_BIND", "0.0.0.0").strip() or "0.0.0.0"
RUNNER_PORT = int(os.getenv("BRIDGE_AI_RUNNER_PORT", "8765"))
RUNNER_TOKEN = os.getenv("BRIDGE_AI_RUNNER_TOKEN", "").strip()
RUNNER_STRICT_SCHEMA = env_flag("BRIDGE_AI_RUNNER_STRICT_SCHEMA", False)
RUNNER_CONTEXT_DIR = (
    os.getenv("BRIDGE_AI_RUNNER_CONTEXT_DIR", "data/ai-runner/context").strip()
    or "data/ai-runner/context"
)
RUNNER_COMPOSE_SESSION_DIR = (
    os.getenv("BRIDGE_AI_RUNNER_COMPOSE_SESSION_DIR", "data/ai-runner/compose-sessions").strip()
    or "data/ai-runner/compose-sessions"
)
RUNNER_COMPOSE_SESSION_TTL_HOURS = int(
    os.getenv("BRIDGE_AI_RUNNER_COMPOSE_SESSION_TTL_HOURS", "24")
)
RUNNER_COMPOSE_HISTORY_LIMIT = int(os.getenv("BRIDGE_AI_RUNNER_COMPOSE_HISTORY_LIMIT", "3"))
CODEX_MODEL = os.getenv("BRIDGE_AI_CODEX_MODEL", "").strip()


def runner_settings_json() -> dict[str, Any]:
    return {
        "bind": RUNNER_BIND,
        "port": RUNNER_PORT,
        "tokenRequired": bool(RUNNER_TOKEN),
        "strictSchema": RUNNER_STRICT_SCHEMA,
        "codexModel": CODEX_MODEL or None,
        "contextDir": RUNNER_CONTEXT_DIR,
        "composeSessionDir": RUNNER_COMPOSE_SESSION_DIR,
        "composeSessionTtlHours": RUNNER_COMPOSE_SESSION_TTL_HOURS,
        "composeHistoryLimit": RUNNER_COMPOSE_HISTORY_LIMIT,
        "allowedProviders": ALLOWED_PROVIDERS,
    }


def ensure_context_dir() -> Path:
    path = Path(RUNNER_CONTEXT_DIR).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def ensure_compose_session_root() -> Path:
    path = Path(RUNNER_COMPOSE_SESSION_DIR).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def sanitize_compose_session_id(raw: Any) -> str:
    value = "" if raw is None else str(raw).strip()
    out = []
    for ch in value[:96]:
        if ch.isalnum() or ch in {"-", "_", "."}:
            out.append(ch)
    return "".join(out)[:80]


def prune_compose_sessions() -> None:
    if RUNNER_COMPOSE_SESSION_TTL_HOURS <= 0:
        return
    try:
        root = ensure_compose_session_root()
        cutoff = time.time() - RUNNER_COMPOSE_SESSION_TTL_HOURS * 3600
        for child in root.iterdir():
            if not child.is_dir():
                continue
            try:
                if child.stat().st_mtime >= cutoff:
                    continue
                for path in sorted(child.rglob("*"), reverse=True):
                    if path.is_file() or path.is_symlink():
                        path.unlink(missing_ok=True)
                    elif path.is_dir():
                        path.rmdir()
                child.rmdir()
            except Exception:
                continue
    except Exception:
        return


def compose_session_dir(session_id: str) -> Path | None:
    session_id = sanitize_compose_session_id(session_id)
    if not session_id:
        return None
    root = ensure_compose_session_root()
    path = (root / session_id).resolve()
    if root not in path.parents:
        return None
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_provider(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value == "claude-code":
        return "claude"
    return value


def load_allowed_providers() -> list[str]:
    raw = os.getenv("BRIDGE_AI_RUNNER_ALLOW_PROVIDERS", "codex,claude,agy")
    out: list[str] = []
    seen: set[str] = set()
    for token in raw.split(","):
        provider = normalize_provider(token)
        if not provider:
            continue
        if provider not in {"codex", "claude", "agy"}:
            continue
        if provider in seen:
            continue
        seen.add(provider)
        out.append(provider)
    if not out:
        out = ["codex", "claude", "agy"]
    return out


ALLOWED_PROVIDERS = load_allowed_providers()


def default_schema_json() -> str:
    return json.dumps(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["schemaVersion", "planId", "summary", "actions"],
            "properties": {
                "schemaVersion": {"type": "integer"},
                "planId": {"type": "string"},
                "summary": {"type": "string"},
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "actionType",
                            "messageIds",
                            "params",
                            "reason",
                            "confidence",
                        ],
                        "properties": {
                            "actionType": {"type": "string"},
                            "messageIds": {"type": "array", "items": {"type": "string"}},
                            "params": {"type": "object"},
                            "reason": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                    },
                },
            },
        }
    )


def default_compose_schema_json(max_versions: int = 2) -> str:
    max_versions = max(1, min(int(max_versions or 2), 2))
    return json.dumps(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "versions"],
            "properties": {
                "summary": {"type": "string"},
                "versions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": max_versions,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["label", "body"],
                        "properties": {
                            "label": {"type": "string"},
                            "body": {"type": "string"},
                        },
                    },
                },
            },
        }
    )


def truncate(raw: str, max_chars: int = 2000) -> str:
    if len(raw) <= max_chars:
        return raw
    head = raw[: max_chars // 2]
    tail = raw[-(max_chars // 2) :]
    return f"{head} ... {tail}"


def provider_stdout_error_detail(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    candidates = [raw]
    extracted = extract_last_json_object(raw)
    if extracted and extracted != raw:
        candidates.insert(0, extracted)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        parts: list[str] = []
        api_status = parsed.get("api_error_status")
        if api_status is not None:
            parts.append(f"api_error_status={api_status}")
        for key in ("error", "message", "result"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
                break
        if parts:
            return " ".join(parts)
        if parsed.get("is_error") is True:
            return truncate(json.dumps(parsed, ensure_ascii=False))

    return truncate(raw)


def provider_process_error(provider: str, proc: subprocess.CompletedProcess[str]) -> str:
    stderr = (proc.stderr or "").strip()
    stdout = provider_stdout_error_detail(proc.stdout or "")
    parts = []
    if stderr:
        parts.append(stderr)
    if stdout:
        parts.append(stdout)
    detail = " | ".join(parts) if parts else "provider returned no stderr/stdout"
    return f"{provider} exited with {proc.returncode}: {truncate(detail)}"


def extract_last_json_object(raw: str) -> str | None:
    start = None
    depth = 0
    in_string = False
    escaped = False
    last_obj: str | None = None
    for idx, ch in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if start is None:
                start = idx
            depth += 1
            continue
        if ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                candidate = raw[start : idx + 1]
                try:
                    json.loads(candidate)
                except Exception:
                    start = None
                    continue
                last_obj = candidate
                start = None
    return last_obj


def parse_timeout_ms(raw: Any) -> int:
    try:
        value = int(raw)
    except Exception:
        value = DEFAULT_TIMEOUT_MS
    if value < 1000:
        value = 1000
    if value > MAX_TIMEOUT_MS:
        value = MAX_TIMEOUT_MS
    return value


def decode_context_json_payload(payload: dict[str, Any]) -> str:
    raw_context_json = payload.get("contextJson", "")
    context_json = "" if raw_context_json is None else str(raw_context_json)
    if context_json.strip():
        return context_json

    raw_encoded = payload.get("contextJsonGzipB64", "")
    encoded = "" if raw_encoded is None else str(raw_encoded).strip()
    if not encoded:
        return ""

    try:
        compressed = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError(f"invalid contextJsonGzipB64 payload: {exc}") from exc

    try:
        raw = gzip.decompress(compressed)
    except Exception as exc:
        raise ValueError(f"failed to decompress contextJsonGzipB64: {exc}") from exc

    try:
        return raw.decode("utf-8")
    except Exception as exc:
        raise ValueError(f"contextJsonGzipB64 is not valid utf-8: {exc}") from exc


def clamp_text(raw: Any, max_chars: int) -> str:
    text = "" if raw is None else str(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:max_chars]


def write_session_text(session_dir: Path | None, filename: str, text: str) -> None:
    if session_dir is None or not text:
        return
    try:
        (session_dir / filename).write_text(text, encoding="utf-8")
    except Exception:
        return


def read_session_text(session_dir: Path | None, filename: str, max_chars: int) -> str:
    if session_dir is None:
        return ""
    try:
        return clamp_text((session_dir / filename).read_text(encoding="utf-8"), max_chars)
    except Exception:
        return ""


def compose_source_text(payload: dict[str, Any], session_dir: Path | None = None) -> str:
    source = payload.get("sourceContext")
    if isinstance(source, dict):
        for key in ("contextText", "bodyText", "editorContextText", "fragment"):
            value = clamp_text(source.get(key), 50000)
            if value:
                write_session_text(session_dir, "source-context.txt", value)
                return value
    value = clamp_text(payload.get("sourceMessageText"), 50000)
    if value:
        write_session_text(session_dir, "source-context.txt", value)
        return value
    return read_session_text(session_dir, "source-context.txt", 50000)


def compose_suggestion_text_from_output(raw: str, max_chars: int = 6000) -> str:
    raw = clamp_text(raw, max_chars)
    if not raw:
        return ""
    candidate = extract_last_json_object(raw) or raw
    try:
        parsed = json.loads(candidate)
    except Exception:
        return raw
    if isinstance(parsed, dict):
        versions = parsed.get("versions")
        if isinstance(versions, list):
            out: list[str] = []
            for idx, item in enumerate(versions[:2], start=1):
                if not isinstance(item, dict):
                    continue
                label = clamp_text(item.get("label") or f"Version {idx}", 80)
                body = clamp_text(item.get("body"), 2500)
                if body:
                    out.append(f"{label}:\n{body}")
            if out:
                return clamp_text("\n\n".join(out), max_chars)
        response = parsed.get("response")
        if isinstance(response, str) and response.strip():
            return compose_suggestion_text_from_output(response, max_chars)
    return raw


def append_compose_suggestion(
    session_dir: Path | None, provider: str, instruction: str, output: str
) -> None:
    if session_dir is None or not output:
        return
    text = compose_suggestion_text_from_output(output)
    if not text:
        return
    record = {
        "ts": int(time.time()),
        "provider": provider,
        "instruction": clamp_text(instruction, 1000),
        "suggestionText": clamp_text(text, 6000),
    }
    try:
        with (session_dir / "suggestions.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


def recent_compose_suggestions(session_dir: Path | None) -> list[dict[str, Any]]:
    if session_dir is None or RUNNER_COMPOSE_HISTORY_LIMIT <= 0:
        return []
    try:
        lines = (session_dir / "suggestions.jsonl").read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-RUNNER_COMPOSE_HISTORY_LIMIT:]:
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if isinstance(parsed, dict) and parsed.get("suggestionText"):
            out.append(parsed)
    return out


def compose_selected_candidates(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw = payload.get("selectedCandidates")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for idx, item in enumerate(raw[:2], start=1):
        if not isinstance(item, dict):
            continue
        body = clamp_text(item.get("body"), 50000)
        if not body:
            continue
        label = clamp_text(item.get("label"), 120) or f"Candidate {idx}"
        out.append({"label": label, "body": body})
    return out


def selected_candidates_text(candidates: list[dict[str, str]], max_chars: int = 50000) -> str:
    parts: list[str] = []
    for item in candidates:
        label = clamp_text(item.get("label"), 120) or "Candidate"
        body = clamp_text(item.get("body"), 25000)
        if body:
            parts.extend([f"{label}:", body])
    return clamp_text("\n\n".join(parts), max_chars)


def build_compose_prompt(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    prune_compose_sessions()
    session_id = sanitize_compose_session_id(payload.get("composeSessionId"))
    session_dir = compose_session_dir(session_id)
    instruction = clamp_text(
        payload.get("instruction", payload.get("prompt", "Please draft a reply.")), 4000
    )
    action = clamp_text(payload.get("action", "rewrite"), 80).lower() or "rewrite"
    compose_mode = clamp_text(payload.get("composeMode", "html"), 40).lower() or "html"
    draft = clamp_text(payload.get("draftText"), 50000)
    source_text = compose_source_text(payload, session_dir)
    selected_candidates = compose_selected_candidates(payload)
    selected_text = selected_candidates_text(selected_candidates)
    if selected_text:
        write_session_text(session_dir, "selected-candidates-latest.txt", selected_text)
    if draft:
        write_session_text(session_dir, "current-draft.txt", draft)
    prior_suggestions = recent_compose_suggestions(session_dir)
    try:
        max_versions = int(payload.get("maxVersions", 2))
    except Exception:
        max_versions = 2
    max_versions = max(1, min(max_versions, 2))

    if action == "reply":
        task = "Draft a reply."
    elif action == "shorter":
        task = "Make the draft shorter."
    elif action == "professional":
        task = "Make the draft more professional."
    elif action == "warmer":
        task = "Make the draft warmer."
    elif action == "grammar":
        task = "Fix grammar only."
    else:
        task = "Improve the email draft."

    parts = [
        task,
        f"Return JSON only with up to {max_versions} plain-text version bodies.",
        "The user reviews, edits, and sends manually.",
    ]
    if instruction:
        parts.extend(["", "User instruction:", instruction])
    if source_text:
        parts.extend(["", "Message context:", source_text])
    if selected_candidates:
        parts.extend(["", "Selected candidate draft(s) to improve:"])
        for item in selected_candidates:
            label = clamp_text(item.get("label"), 120) or "Candidate"
            body = clamp_text(item.get("body"), 25000)
            if body:
                parts.extend(["", label + ":", body])
    elif prior_suggestions:
        parts.extend(["", "Previous AI suggestions for this same reply:"])
        for item in prior_suggestions:
            provider = clamp_text(item.get("provider"), 40) or "ai"
            previous_instruction = clamp_text(item.get("instruction"), 500)
            suggestion = clamp_text(item.get("suggestionText"), 4000)
            if not suggestion:
                continue
            heading = f"{provider}"
            if previous_instruction:
                heading += f" ({previous_instruction})"
            parts.extend(["", heading + ":", suggestion])
    if draft and not selected_candidates:
        parts.extend(["", "Current draft:", draft])
    parts.extend(
        [
            "",
            'JSON shape: {"summary":"brief note","versions":[{"label":"Version 1","body":"email text"}]}',
        ]
    )
    normalized = {
        "composeSessionId": session_id,
        "instruction": instruction,
        "action": action,
        "composeMode": compose_mode,
        "draftText": draft,
        "sourceContextText": source_text,
        "selectedCandidates": selected_candidates,
        "selectedCandidateCount": len(selected_candidates),
        "priorSuggestionCount": len(prior_suggestions),
        "maxVersions": max_versions,
    }
    return "\n".join(parts), normalized


@dataclass
class ProbeResult:
    provider: str
    binary: str
    available: bool
    status: str
    detail: str
    version: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "binary": self.binary,
            "available": self.available,
            "status": self.status,
            "detail": self.detail,
            "version": self.version,
        }


def binary_for_provider(provider: str) -> str:
    if provider == "agy":
        return os.getenv("BRIDGE_AI_RUNNER_AGY_BIN", "agy")
    return provider


def run_probe(provider: str, timeout_ms: int) -> ProbeResult:
    provider = normalize_provider(provider)
    if provider not in {"codex", "claude", "agy"}:
        return ProbeResult(
            provider=provider or "",
            binary="",
            available=False,
            status="unsupported",
            detail="unsupported provider",
            version=None,
        )
    if provider not in ALLOWED_PROVIDERS:
        return ProbeResult(
            provider=provider,
            binary=binary_for_provider(provider),
            available=False,
            status="forbidden",
            detail="provider is not allowed by runner configuration",
            version=None,
        )

    binary = binary_for_provider(provider)
    try:
        output = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000.0,
            check=False,
        )
    except FileNotFoundError as exc:
        return ProbeResult(
            provider=provider,
            binary=binary,
            available=False,
            status="missing",
            detail=f"failed to execute {exc}",
            version=None,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(
            provider=provider,
            binary=binary,
            available=False,
            status="timeout",
            detail=f"preflight timed out after {timeout_ms}ms",
            version=None,
        )
    except Exception as exc:
        return ProbeResult(
            provider=provider,
            binary=binary,
            available=False,
            status="error",
            detail=f"preflight failed: {exc}",
            version=None,
        )

    version = (output.stdout or "").strip()
    if not version:
        version = (output.stderr or "").strip()
    version = version[:180] if version else None

    if output.returncode == 0:
        return ProbeResult(
            provider=provider,
            binary=binary,
            available=True,
            status="ok",
            detail="binary is available",
            version=version,
        )
    return ProbeResult(
        provider=provider,
        binary=binary,
        available=False,
        status="error",
        detail=f"command exited with status {output.returncode}",
        version=version,
    )


def run_plan(
    provider: str,
    prompt: str,
    schema: str,
    timeout_ms: int,
    context_json: str | None = None,
) -> str:
    provider = normalize_provider(provider)
    if provider not in {"codex", "claude", "agy"}:
        raise RuntimeError("unsupported provider")
    if provider not in ALLOWED_PROVIDERS:
        raise RuntimeError("provider is not allowed by runner configuration")
    workspace_tmp = tempfile.TemporaryDirectory(prefix="zbridge-ai-runner-")
    workspace = Path(workspace_tmp.name)

    try:
        if context_json and context_json.strip():
            context_file = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                delete=False,
                encoding="utf-8",
                dir=str(workspace),
            )
            context_path = Path(context_file.name)
            context_file.write(context_json)
            context_file.flush()
            context_file.close()
            prompt = (
                f"{prompt}\n\n"
                "The sampled messages JSON is available in this local file path:\n"
                f"{context_path}\n"
                "Read that file before planning. Use ONLY messageIds from that file.\n"
                "Return only the ActionPlan JSON object.\n"
            )

        if provider == "codex":
            schema_path: Path | None = None
            out_file = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                delete=False,
                encoding="utf-8",
                dir=str(workspace),
            )
            out_path = Path(out_file.name)
            try:
                command = [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "read-only",
                ]
                if CODEX_MODEL:
                    command.extend(["--model", CODEX_MODEL])
                if RUNNER_STRICT_SCHEMA:
                    schema_file = tempfile.NamedTemporaryFile(
                        mode="w",
                        suffix=".json",
                        delete=False,
                        encoding="utf-8",
                        dir=str(workspace),
                    )
                    schema_path = Path(schema_file.name)
                    schema_file.write(schema)
                    schema_file.flush()
                    schema_file.close()
                    command.extend(["--output-schema", str(schema_path)])
                command.extend(["--output-last-message", str(out_path), "-"])
                proc = subprocess.run(
                    command,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout_ms / 1000.0,
                    check=False,
                    cwd=str(workspace),
                )
                if proc.returncode != 0:
                    raise RuntimeError(provider_process_error("codex", proc))
                file_output = (
                    out_path.read_text(encoding="utf-8").strip() if out_path.exists() else ""
                )
                if file_output:
                    return file_output
                stdout = (proc.stdout or "").strip()
                if not stdout:
                    raise RuntimeError("codex returned no output")
                return stdout
            finally:
                if schema_path is not None:
                    try:
                        schema_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass

        if provider == "claude":
            command = [
                "claude",
                "-p",
                "--output-format",
                "json",
                "--input-format",
                "text",
                "--permission-mode",
                "default",
            ]
            if RUNNER_STRICT_SCHEMA:
                command.extend(["--json-schema", schema])
            proc = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_ms / 1000.0,
                check=False,
                cwd=str(workspace),
            )
            if proc.returncode != 0:
                raise RuntimeError(provider_process_error("claude", proc))
            stdout = (proc.stdout or "").strip()
            if not stdout:
                raise RuntimeError("claude returned empty output")
            return stdout

        proc = subprocess.run(
            [binary_for_provider("agy"), "--print", prompt],
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000.0,
            check=False,
            cwd=str(workspace),
        )
        if proc.returncode != 0:
            raise RuntimeError(provider_process_error("agy", proc))
        stdout = (proc.stdout or "").strip()
        if not stdout:
            raise RuntimeError("agy returned empty output")
        wrapper_text = extract_last_json_object(stdout) or stdout
        try:
            wrapper = json.loads(wrapper_text)
        except Exception:
            return wrapper_text
        if isinstance(wrapper, dict):
            response = wrapper.get("response")
            if isinstance(response, str) and response.strip():
                return response.strip()
        return wrapper_text
    finally:
        workspace_tmp.cleanup()


class Handler(BaseHTTPRequestHandler):
    server_version = "zbridge-ai-runner/0.1"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ai-runner client disconnected before response status={status}",
                flush=True,
            )

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            return {}
        body = self.rfile.read(content_length)
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def _authorized(self) -> bool:
        if not RUNNER_TOKEN:
            return True
        header_token = self.headers.get("X-Bridge-Runner-Token", "").strip()
        if header_token and header_token == RUNNER_TOKEN:
            return True
        authz = self.headers.get("Authorization", "").strip()
        if authz.startswith("Bearer "):
            return authz[7:].strip() == RUNNER_TOKEN
        return False

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            probes = [run_probe(provider, 2000).to_json() for provider in ALLOWED_PROVIDERS]
            self._send_json(
                200,
                {
                    "ok": True,
                    "runnerVersion": RUNNER_VERSION,
                    "settings": runner_settings_json(),
                    "providers": probes,
                },
            )
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        path = self.path.rstrip("/")
        if path == "/v1/probe":
            provider = normalize_provider(str(payload.get("provider", "")))
            if not provider:
                self._send_json(400, {"ok": False, "error": "provider is required"})
                return
            timeout_ms = parse_timeout_ms(payload.get("timeoutMs"))
            probe = run_probe(provider, timeout_ms).to_json()
            probe["ok"] = True
            self._send_json(200, probe)
            return

        if path == "/v1/plan":
            provider = normalize_provider(str(payload.get("provider", "")))
            prompt = str(payload.get("prompt", ""))
            try:
                context_json = decode_context_json_payload(payload)
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            schema = str(payload.get("schema", "")).strip() or default_schema_json()
            timeout_ms = parse_timeout_ms(payload.get("timeoutMs"))

            if not provider:
                self._send_json(400, {"ok": False, "error": "provider is required"})
                return
            if not prompt.strip():
                self._send_json(400, {"ok": False, "error": "prompt is required"})
                return

            started = time.time()
            try:
                output = run_plan(provider, prompt, schema, timeout_ms, context_json)
            except subprocess.TimeoutExpired:
                self._send_json(
                    504,
                    {
                        "ok": False,
                        "provider": provider,
                        "error": f"provider request timed out after {timeout_ms}ms",
                    },
                )
                return
            except FileNotFoundError as exc:
                self._send_json(
                    502,
                    {
                        "ok": False,
                        "provider": provider,
                        "error": f"failed to start {provider}: {exc}",
                    },
                )
                return
            except Exception as exc:
                self._send_json(
                    502,
                    {
                        "ok": False,
                        "provider": provider,
                        "error": str(exc),
                    },
                )
                return

            duration_ms = int((time.time() - started) * 1000.0)
            self._send_json(
                200,
                {
                    "ok": True,
                    "provider": provider,
                    "durationMs": duration_ms,
                    "output": output,
                },
            )
            return

        if path == "/v1/compose":
            provider = normalize_provider(str(payload.get("provider", "")))
            prompt, normalized = build_compose_prompt(payload)
            timeout_ms = parse_timeout_ms(payload.get("timeoutMs"))
            try:
                max_versions = int(normalized.get("maxVersions", 2))
            except Exception:
                max_versions = 2
            schema = (
                str(payload.get("schema", "")).strip()
                or default_compose_schema_json(max_versions)
            )

            if payload.get("debugPromptOnly"):
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "debugPromptOnly": True,
                        "aiPromptText": prompt,
                        "normalized": normalized,
                        "notes": [
                            "debug preview only; no AI provider was called",
                            "the runner converted bridge JSON into provider prompt text",
                        ],
                    },
                )
                return

            if not provider:
                self._send_json(400, {"ok": False, "error": "provider is required"})
                return
            if (
                not normalized["draftText"]
                and not normalized["sourceContextText"]
                and not normalized["selectedCandidates"]
            ):
                self._send_json(
                    400,
                    {
                        "ok": False,
                        "error": "compose requires draftText, selectedCandidates, or sourceContext/sourceMessageText",
                    },
                )
                return

            started = time.time()
            try:
                output = run_plan(provider, prompt, schema, timeout_ms, None)
                append_compose_suggestion(
                    compose_session_dir(normalized.get("composeSessionId")),
                    provider,
                    normalized.get("instruction", ""),
                    output,
                )
            except subprocess.TimeoutExpired:
                self._send_json(
                    504,
                    {
                        "ok": False,
                        "provider": provider,
                        "error": f"provider request timed out after {timeout_ms}ms",
                    },
                )
                return
            except FileNotFoundError as exc:
                self._send_json(
                    502,
                    {
                        "ok": False,
                        "provider": provider,
                        "error": f"failed to start {provider}: {exc}",
                    },
                )
                return
            except Exception as exc:
                self._send_json(
                    502,
                    {
                        "ok": False,
                        "provider": provider,
                        "error": str(exc),
                    },
                )
                return

            duration_ms = int((time.time() - started) * 1000.0)
            self._send_json(
                200,
                {
                    "ok": True,
                    "provider": provider,
                    "durationMs": duration_ms,
                    "output": output,
                },
            )
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] ai-runner {self.client_address[0]} {format % args}", flush=True)


def main() -> None:
    server = ThreadingHTTPServer((RUNNER_BIND, RUNNER_PORT), Handler)
    print(
        json.dumps(
            {
                "ok": True,
                "message": "ai runner started",
                "settings": runner_settings_json(),
            }
        ),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
