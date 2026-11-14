import pytest

from fre_platform.config import ConfigError, Settings, parse_channels, redact_uri


def test_defaults_are_valid():
    s = Settings.from_env(env={})
    assert s.source == "airspyhf"
    assert s.block_samples == 192000
    assert [c.name for c in s.channels] == ["DCF77", "DCF49", "HGA22", "DCF39"]


def test_env_parsing_types():
    s = Settings.from_env(env={"FRE_SOURCE": "simulated", "FRE_BATCH_SIZE": "17", "FRE_AIRSPY_LNA": "yes",
                               "FRE_BLOCK_SECONDS": "0.5", "FRE_CHANNELS": "A:100000,B:120000",
                               "FRE_DATA_DIR": "/tmp/x", "FRE_SIM_SEED": "3"})
    assert s.batch_size == 17 and s.airspy_lna is True and s.block_seconds == 0.5
    assert [c.freq_hz for c in s.channels] == [100000.0, 120000.0]
    assert str(s.data_dir) == "/tmp/x" and s.sim_seed == 3


@pytest.mark.parametrize("env", [
    {"FRE_SAMPLE_RATE_HZ": "123456"},
    {"FRE_SOURCE": "transmitter"},
    {"FRE_BATCH_SIZE": "abc"},
    {"FRE_AIRSPY_AGC": "maybe"},
    {"FRE_CHANNELS": "FAR:5000000"},
    {"FRE_MONGODB_URI": "http://example.com"},
    {"FRE_SOURCE": "replay"},
    {"FRE_AIRSPY_ATTENUATION": "12"},
    {"FRE_BLOCK_SECONDS": "0.25"},
])
def test_invalid_values_raise(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env=env)


def test_duplicate_channels_rejected():
    with pytest.raises(ConfigError):
        parse_channels("A:1,A:2")


def test_secret_never_in_repr_or_safe_dict():
    uri = "mongodb+srv://alice:s3cr3t-pw@cluster0.example.mongodb.net/?retryWrites=true"
    s = Settings.from_env(env={"FRE_MONGODB_URI": uri})
    assert "s3cr3t-pw" not in repr(s)
    assert "s3cr3t-pw" not in str(s.safe_dict())
    assert redact_uri(uri).startswith("mongodb+srv://alice:****@cluster0")


def test_env_file_loading(tmp_path, monkeypatch):
    f = tmp_path / "fre.env"
    f.write_text("FRE_STATION_ID=from-file\nFRE_BATCH_SIZE=9\n")
    monkeypatch.setenv("FRE_BATCH_SIZE", "11")  # process env wins over the file
    s = Settings.from_env(env_file=f)
    assert s.station_id == "from-file" and s.batch_size == 11


def test_uri_built_from_parts_and_redacted():
    s = Settings.from_env(env={"FRE_MONGODB_USERNAME": "u", "FRE_MONGODB_PASSWORD": "p@ss/w:rd",
                               "FRE_MONGODB_HOST": "cluster0.abc.mongodb.net"})
    assert s.mongodb_uri == "mongodb+srv://u:p%40ss%2Fw%3Ard@cluster0.abc.mongodb.net/?retryWrites=true&w=majority"
    assert "p%40ss" not in str(s.safe_dict())


@pytest.mark.parametrize("env", [
    {"FRE_MONGODB_USERNAME": "u", "FRE_MONGODB_PASSWORD": "p"},                          # host missing
    {"FRE_MONGODB_USERNAME": "u", "FRE_MONGODB_PASSWORD": "p", "FRE_MONGODB_HOST": "u:p@h/x"},
])
def test_incomplete_or_bad_parts(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env=env)


def test_missing_env_file():
    with pytest.raises(ConfigError):
        Settings.from_env(env_file="/nonexistent/fre.env")
