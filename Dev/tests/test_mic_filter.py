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


@pytest.mark.parametrize("ratio, db", [(1.0, 0.0), (0.1, -20.0), (0.0, -96.0), (1e-12, -96.0), (1e-6, -96.0)])
def test_ratios_become_decibels_down_to_the_floor(ratio, db):
    assert mic_filter._amplitude_ratio_to_db(ratio) == pytest.approx(db)


def test_rms_of_samples():
    assert mic_filter._rms_db_int16([]) == -96.0
    assert mic_filter._rms_db_int16([0, 0]) == -96.0
    assert mic_filter._rms_db_int16([32768, -32768]) == pytest.approx(0.0)
    assert mic_filter._rms_db_int16([3277, -3277]) == pytest.approx(-20.0, abs=0.01)


def test_only_speech_counts_towards_the_speech_level():
    # The noise floor's frames would drag a plain average down.
    assert mic_filter._voice_gated_level_db([-20.0, -20.0, -70.0, -70.0], -60.0) == pytest.approx(-20.0)


def test_a_frame_exactly_at_the_margin_isnt_speech():
    assert mic_filter._voice_gated_level_db([-54.0, -30.0], -60.0) == pytest.approx(-30.0)


def test_nobody_speaking_falls_back_to_every_frame():
    assert mic_filter._voice_gated_level_db([-70.0, -70.0], -60.0) == pytest.approx(-70.0)


def test_no_frames_is_silence():
    assert mic_filter._voice_gated_level_db([], -60.0) == -96.0
    assert mic_filter._voice_gated_level_db([-200.0], -300.0) == -96.0


def test_levels_are_averaged_as_power_not_decibels():
    assert mic_filter._voice_gated_level_db([0.0, -100.0], -300.0, margin_db=0.0) == pytest.approx(-3.01, abs=0.01)


@pytest.mark.parametrize("content, configured", [
    (None, False),
    ("{broken", False),
    ("{}", False),
    ('{"Mic": {}}', False),
    ('{"Mic": {"filters": [{"id": "limiter_filter"}]}}', False),
    ('{"Mic": {"filters": [{"id": "gain_filter"}]}}', True),
    ('{"Mic": {"filters": [{"id": "noise_gate_filter"}]}}', True),
])
def test_a_configured_mic_is_recognised(tmp_path, content, configured):
    path = tmp_path / "scenes.json"
    if content is not None:
        path.write_text(content)
    assert mic_filter.has_mic_filter_been_configured(str(path), "Mic") is configured


def test_filters_cant_be_saved_without_a_mic(tmp_path, capsys):
    path = tmp_path / "scenes.json"
    mic_filter.save_mic_filter_levels(str(path), "Mic", -60.0, -30.0)
    assert "can't set mic filters" in capsys.readouterr().err and not path.exists()
    path.write_text("{}")
    mic_filter.save_mic_filter_levels(str(path), "Mic", -60.0, -30.0)
    assert "no mic source to filter" in capsys.readouterr().err and path.read_text() == "{}"


def test_every_filter_is_created_in_order_keeping_a_hand_added_one(tmp_path):
    path = tmp_path / "scenes.json"
    path.write_text(json.dumps({"Mic": {"filters": [{"id": "upward_compressor", "name": "Mine"}]}}))
    mic_filter.save_mic_filter_levels(str(path), "Mic", -60.0, -30.0)
    ids = [f["id"] for f in json.loads(path.read_text())["Mic"]["filters"]]
    assert ids == [*mic_filter.HEAD_FILTER_IDS, "upward_compressor", *mic_filter.TAIL_FILTER_IDS]


def test_saving_twice_doesnt_double_the_filters(tmp_path):
    path = tmp_path / "scenes.json"
    path.write_text(json.dumps({"Mic": {}}))
    mic_filter.save_mic_filter_levels(str(path), "Mic", -60.0, -30.0)
    first = len(json.loads(path.read_text())["Mic"]["filters"])
    mic_filter.save_mic_filter_levels(str(path), "Mic", -50.0, -20.0)
    filters = json.loads(path.read_text())["Mic"]["filters"]
    assert len(filters) == first
    gain = next(f for f in filters if f["id"] == "gain_filter")
    assert gain["settings"]["db"] == mic_filter.compute_mic_filter_values(-50.0, -20.0)[0]
