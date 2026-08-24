"""AI provider factory for the report pipeline.

One provider = one CLI/backend that can answer a prompt and return strict
JSON on stdout. Only Claude is implemented today; codex, ollama, Grok etc.
plug in by subclassing Provider and adding an entry to PROVIDERS.

A provider that is not installed on the machine is a normal setup, not an
error: it is simply absent from detect() and every AI stage is skipped.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
from pathlib import Path

TIMEOUT_S = 900


class Provider:
    """One AI backend: detection, selectable instances, prompt execution."""

    name = ""   # registry key, also the --ai-provider CLI value
    label = ""  # human-readable name shown in the panel

    def available(self, instance: str = "") -> bool:
        """True when the provider can run on this machine (CLI installed)."""
        raise NotImplementedError

    def instances(self) -> list[str]:
        """Selectable configurations (accounts/models); [] when unavailable."""
        raise NotImplementedError

    def instance_label(self, instance: str) -> str:
        """Display label for one instance in the panel dropdown."""
        return instance

    def run(self, prompt: str, instance: str) -> subprocess.CompletedProcess:
        """Execute `prompt` on `instance` and return the completed process.
        May raise FileNotFoundError (CLI gone) or subprocess.TimeoutExpired."""
        raise NotImplementedError


class ClaudeProvider(Provider):
    """Claude Code CLI (`claude -p`). Instances map to config dirs: the
    default `claude` uses ~/.claude, `claude-<x>` uses ~/.claude-<x> via
    CLAUDE_CONFIG_DIR; anything else is treated as a path to the binary."""

    name = "claude"
    label = "Claude"

    def _cmd_env(self, instance: str) -> tuple[list[str], dict]:
        env = os.environ.copy()
        instance = instance or "claude"
        cmd = [instance, "-p"]
        m = re.fullmatch(r"claude-([\w.-]+)", instance)
        if m:
            env["CLAUDE_CONFIG_DIR"] = str(Path.home() / f".claude-{m.group(1)}")
            cmd = ["claude", "-p"]
        resolved = shutil.which(cmd[0])
        if resolved:
            cmd[0] = resolved
        return cmd, env

    def available(self, instance: str = "") -> bool:
        cmd, _env = self._cmd_env(instance)
        return shutil.which(cmd[0]) is not None

    def instances(self) -> list[str]:
        if not self.available():
            return []
        home = Path.home()
        items = ["claude"] if (home / ".claude").is_dir() else []
        items += sorted(d.name[1:] for d in home.glob(".claude-*") if d.is_dir())
        return items or ["claude"]

    def instance_label(self, instance: str) -> str:
        return "claude (~/.claude)" if instance == "claude" else f"{instance} (~/.{instance})"

    def run(self, prompt: str, instance: str) -> subprocess.CompletedProcess:
        cmd, env = self._cmd_env(instance)
        return subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, env=env, timeout=TIMEOUT_S)


PROVIDERS: dict[str, Provider] = {p.name: p for p in (ClaudeProvider(),)}


def get(name: str) -> Provider | None:
    return PROVIDERS.get(name)


def detect() -> list[dict]:
    """Available providers with their instances, in panel wire format:
    [{"name", "label", "instances": [{"name", "label"}, ...]}, ...]."""
    out = []
    for p in PROVIDERS.values():
        inst = p.instances()
        if inst:
            out.append({"name": p.name, "label": p.label,
                        "instances": [{"name": i, "label": p.instance_label(i)}
                                      for i in inst]})
    return out
