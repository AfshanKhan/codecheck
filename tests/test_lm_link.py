import json
from unittest.mock import MagicMock, patch

from codecheck.lm_link import resolve_model_location, set_preferred_device


def _completed(stdout: str, returncode: int = 0):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = ""
    return result


def _fake_run(link_status: dict, ps_output: list):
    def run(args, **kwargs):
        if "link" in args:
            return _completed(json.dumps(link_status))
        return _completed(json.dumps(ps_output))

    return run


def test_resolves_remote_when_model_is_in_a_peers_loaded_models():
    link_status = {
        "deviceIdentifier": "local-id",
        "peers": [{"deviceName": "Some-Remote-Box", "deviceIdentifier": "remote-id", "loadedModels": ["google/gemma-4-12b"]}],
    }
    with patch("codecheck.lm_link.shutil.which", return_value="/usr/bin/lms"), \
         patch("codecheck.lm_link.subprocess.run", side_effect=_fake_run(link_status, [])):
        loc = resolve_model_location("google/gemma-4-12b")

    assert loc.is_local is False
    assert loc.device_name == "Some-Remote-Box"
    assert loc.is_ambiguous is False
    assert "Some-Remote-Box" in loc.description


def test_remote_only_model_is_not_misread_as_also_local():
    # regression: `lms ps --json` lists every model loaded anywhere on the LM
    # Link network, not just this machine -- entries for remote models carry a
    # non-null deviceIdentifier. A remote-only model used to get flagged as
    # ambiguous because that field wasn't checked.
    link_status = {
        "deviceIdentifier": "local-id",
        "peers": [{"deviceName": "Some-Remote-Box", "deviceIdentifier": "remote-id", "loadedModels": ["google/gemma-4-e4b"]}],
    }
    ps_output = [{"identifier": "google/gemma-4-e4b", "deviceIdentifier": "remote-id"}]

    with patch("codecheck.lm_link.shutil.which", return_value="/usr/bin/lms"), \
         patch("codecheck.lm_link.subprocess.run", side_effect=_fake_run(link_status, ps_output)):
        loc = resolve_model_location("google/gemma-4-e4b")

    assert loc.is_local is False
    assert loc.is_ambiguous is False
    assert loc.device_name == "Some-Remote-Box"


def test_resolves_local_when_model_is_in_lms_ps_but_not_any_peer():
    link_status = {"deviceIdentifier": "local-id", "peers": []}
    ps_output = [{"identifier": "google/gemma-4-e2b"}]

    with patch("codecheck.lm_link.shutil.which", return_value="/usr/bin/lms"), \
         patch("codecheck.lm_link.subprocess.run", side_effect=_fake_run(link_status, ps_output)):
        loc = resolve_model_location("google/gemma-4-e2b")

    assert loc.is_local is True
    assert loc.is_ambiguous is False
    assert "locally" in loc.description


def test_undetermined_when_not_found_anywhere():
    link_status = {"deviceIdentifier": "local-id", "peers": []}
    ps_output = []

    with patch("codecheck.lm_link.shutil.which", return_value="/usr/bin/lms"), \
         patch("codecheck.lm_link.subprocess.run", side_effect=_fake_run(link_status, ps_output)):
        loc = resolve_model_location("some/unknown-model")

    assert loc.is_local is None
    assert loc.is_ambiguous is False


def test_undetermined_when_lms_cli_not_installed():
    with patch("codecheck.lm_link.shutil.which", return_value=None):
        loc = resolve_model_location("anything")

    assert loc.is_local is None
    assert "lms CLI not found" in loc.description


def test_ambiguous_when_loaded_both_locally_and_remotely():
    link_status = {
        "deviceIdentifier": "local-id",
        "peers": [{"deviceName": "Some-Remote-Box", "deviceIdentifier": "remote-id", "loadedModels": ["google/gemma-4-12b"]}],
    }
    ps_output = [{"identifier": "google/gemma-4-12b"}]

    with patch("codecheck.lm_link.shutil.which", return_value="/usr/bin/lms"), \
         patch("codecheck.lm_link.subprocess.run", side_effect=_fake_run(link_status, ps_output)):
        loc = resolve_model_location("google/gemma-4-12b")

    assert loc.is_local is None
    assert loc.is_ambiguous is True
    labels = {c.label for c in loc.candidates}
    assert "Some-Remote-Box" in labels
    assert any("Local" in label for label in labels)
    local_candidate = next(c for c in loc.candidates if "Local" in c.label)
    assert local_candidate.device_identifier == "local-id"
    remote_candidate = next(c for c in loc.candidates if c.label == "Some-Remote-Box")
    assert remote_candidate.device_identifier == "remote-id"


def test_set_preferred_device_success():
    with patch("codecheck.lm_link.shutil.which", return_value="/usr/bin/lms"), \
         patch("codecheck.lm_link.subprocess.run", return_value=_completed("Updated preferred device")):
        ok, msg = set_preferred_device("remote-id")

    assert ok is True


def test_set_preferred_device_failure():
    failed = _completed("", returncode=1)
    failed.stderr = "device not found"
    with patch("codecheck.lm_link.shutil.which", return_value="/usr/bin/lms"), \
         patch("codecheck.lm_link.subprocess.run", return_value=failed):
        ok, msg = set_preferred_device("bogus-id")

    assert ok is False
    assert "device not found" in msg


def test_set_preferred_device_no_lms_cli():
    with patch("codecheck.lm_link.shutil.which", return_value=None):
        ok, msg = set_preferred_device("remote-id")

    assert ok is False
    assert "lms CLI not found" in msg
