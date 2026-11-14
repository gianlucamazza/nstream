"""Doctor must be useful even without a working install and must remain read-only."""

import json
import stat

from nstream import doctor
from nstream.config import config_path


def test_doctor_reports_missing_tools_without_network(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    report = doctor.inspect()
    assert not report["ok"]
    assert not report["network_tested"] and not report["playback_tested"]
    assert all(c["status"] == "missing" for c in report["checks"] if c["name"] != "config")


def test_doctor_preserves_config_bytes_and_permissions(monkeypatch, capsys):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/fake/bin")
    path = config_path()
    raw = '{"torrentio_base":"https://example.test/SECRET"}'
    path.write_text(raw)
    path.chmod(0o644)
    assert doctor.run(json_mode=True) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"][-1]["status"] == "permissions"
    assert "SECRET" not in json.dumps(report)
    assert path.read_text() == raw and stat.S_IMODE(path.stat().st_mode) == 0o644


def test_doctor_finds_castbridge_where_the_cast_path_does(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor.bridge, "bridge_available", lambda: True)
    checks = {c["name"]: c["status"] for c in doctor.inspect()["checks"]}
    assert checks["castbridge"] == "ok"


def test_doctor_explains_local_hls_requirements_without_probing(monkeypatch):
    from nstream.config import Config

    monkeypatch.setattr(doctor, "load", lambda **kwargs: Config(torrentio_base="tb"))
    monkeypatch.setattr(doctor.remux, "available", lambda: True)
    monkeypatch.setattr(doctor.bridge, "bridge_available", lambda: False)
    monkeypatch.setattr(
        doctor.remux, "_probe_meta", lambda _: (_ for _ in ()).throw(AssertionError())
    )
    checks = {c["name"]: c for c in doctor.inspect()["checks"]}
    assert checks["cast_live"]["status"] == "missing"
    assert "castbridge" in checks["cast_live"]["detail"]
    monkeypatch.setattr(doctor.bridge, "bridge_available", lambda: True)
    checks = {c["name"]: c for c in doctor.inspect()["checks"]}
    assert checks["cast_live"]["status"] == "ok"
    assert "non verificate" in checks["cast_live"]["detail"]
