import json

import mic_filter
import pytest


@pytest.mark.parametrize(
    "noise_floor_db, speech_level_db",
    [
        (-60.0, -38.0),  # quiet mic: big positive gain
        (-55.0, -30.0),
        (-50.0, -18.0),  # already at target: no gain
        (-40.0, -8.0),   # hot mic: negative gain
    ],
)
def test_gate_thresholds_bracket_noise_and_speech_after_gain(noise_floor_db, speech_level_db):
    """The gate sits after the Gain filter, so compare against the levels
    it actually sees: room noise must stay below the close threshold (the
    gate closes on it), speech above the open threshold (the gate opens)."""
    gain_db, open_db, close_db = mic_filter.compute_mic_filter_values(noise_floor_db, speech_level_db)
    noise_after_gain = noise_floor_db + gain_db
    speech_after_gain = speech_level_db + gain_db
    assert noise_after_gain < close_db < open_db < speech_after_gain


def test_gain_brings_speech_to_target():
    gain_db, _, _ = mic_filter.compute_mic_filter_values(-60.0, -38.0)
    assert -38.0 + gain_db == mic_filter.TARGET_SPEECH_DBFS


def test_thresholds_are_clamped_to_obs_range():
    _, open_db, close_db = mic_filter.compute_mic_filter_values(-200.0, -150.0)
    assert close_db >= -96.0 and open_db >= -96.0
    _, open_db, close_db = mic_filter.compute_mic_filter_values(10.0, 20.0)
    assert close_db <= 0.0 and open_db <= 0.0


def test_saved_gate_uses_post_gain_thresholds(tmp_path):
    scenes = tmp_path / "scenes.json"
    scenes.write_text(json.dumps({"AuxAudioDevice1": {"filters": []}}))

    mic_filter.save_mic_filter_levels(str(scenes), "AuxAudioDevice1", -60.0, -38.0)

    filters = json.loads(scenes.read_text())["AuxAudioDevice1"]["filters"]
    by_id = {f["id"]: f for f in filters}
    gain_db, open_db, close_db = mic_filter.compute_mic_filter_values(-60.0, -38.0)
    assert by_id["gain_filter"]["settings"]["db"] == gain_db
    assert by_id["noise_gate_filter"]["settings"] == {"close_threshold": close_db, "open_threshold": open_db}
    # Gain is applied before the gate.
    ids = [f["id"] for f in filters]
    assert ids.index("gain_filter") < ids.index("noise_gate_filter")
