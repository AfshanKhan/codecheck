"""Resolves which device will actually serve a given LM Studio model -- this
machine, or a remote device connected via LM Link. Shells out to the `lms`
CLI. `set_preferred_device()` lets the caller pick explicitly when a model
is loaded on more than one device.

Separate from reviewers/local_llm.py: this is a pre-flight CLI concern."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field

LOCAL_DEVICE_LABEL = "Local (this machine)"


@dataclass
class DeviceCandidate:
    label: str
    device_identifier: str


@dataclass
class ModelLocation:
    # True = this machine, False = confirmed single remote device, None =
    # undetermined/ambiguous.
    is_local: bool | None
    description: str
    device_name: str | None = None
    is_ambiguous: bool = False
    candidates: list[DeviceCandidate] = field(default_factory=list)


def _run_lms_json(args: list[str]) -> tuple[dict | list | None, str | None]:
    try:
        result = subprocess.run(["lms", *args], capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, str(e)
    if result.returncode != 0:
        return None, result.stderr.strip() or f"lms {' '.join(args)} exited {result.returncode}"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError:
        return None, f"could not parse output of `lms {' '.join(args)}`"


def resolve_model_location(model_id: str) -> ModelLocation:
    if shutil.which("lms") is None:
        return ModelLocation(
            is_local=None,
            description="lms CLI not found — cannot confirm which device will serve this model",
        )

    status, status_error = _run_lms_json(["link", "status", "--json"])
    if status_error:
        return ModelLocation(is_local=None, description=f"could not query LM Link status: {status_error}")

    local_device_id = status.get("deviceIdentifier") if isinstance(status, dict) else None
    remote_matches: list[DeviceCandidate] = []
    if isinstance(status, dict):
        for peer in status.get("peers", []):
            if model_id in (peer.get("loadedModels") or []):
                remote_matches.append(
                    DeviceCandidate(
                        label=peer.get("deviceName", "unknown remote device"),
                        device_identifier=peer.get("deviceIdentifier", ""),
                    )
                )

    loaded, ps_error = _run_lms_json(["ps", "--json"])
    if ps_error:
        return ModelLocation(is_local=None, description=f"could not query local model status: {ps_error}")

    # deviceIdentifier is null/absent for a local model, set for a remote one.
    is_local_loaded = any(
        (entry.get("identifier") == model_id or entry.get("modelKey") == model_id)
        and not entry.get("deviceIdentifier")
        for entry in (loaded or [])
    )

    if remote_matches and is_local_loaded:
        candidates = [
            DeviceCandidate(label=LOCAL_DEVICE_LABEL, device_identifier=local_device_id or ""),
            *remote_matches,
        ]
        names = ", ".join(c.label for c in candidates)
        return ModelLocation(
            is_local=None,
            description=f"{model_id!r} is loaded on multiple devices ({names}) — ambiguous which one will serve requests",
            is_ambiguous=True,
            candidates=candidates,
        )

    if remote_matches:
        m = remote_matches[0]
        return ModelLocation(
            is_local=False,
            description=f"{model_id!r} is loaded on remote device {m.label!r} via LM Link",
            device_name=m.label,
        )

    if is_local_loaded:
        return ModelLocation(
            is_local=True,
            description=f"{model_id!r} is currently loaded locally on this machine",
        )

    return ModelLocation(
        is_local=None,
        description=(
            f"{model_id!r} is not currently loaded anywhere detectable — "
            "LM Studio may load it on an unpredictable device on first use"
        ),
    )


def set_preferred_device(device_identifier: str) -> tuple[bool, str]:
    """Sets LM Studio's LM Link preferred device -- a persistent app
    setting, not scoped to this run."""
    if shutil.which("lms") is None:
        return False, "lms CLI not found"
    try:
        result = subprocess.run(
            ["lms", "link", "set-preferred-device", device_identifier],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip() or f"exited {result.returncode}"
    return True, result.stdout.strip()
