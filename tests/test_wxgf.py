"""Unit tests for the wxgf→JPEG decoder. Synthetic bytes only — the real corpus
is user-private and validated separately by work/perf_20260816/wxgf_validate.py."""
import struct
import subprocess
from pathlib import Path

from PIL import Image

from chat_daily_tg import wxgf
from chat_daily_tg.wxgf import decode_wxgf, is_wxgf


def synthetic_wxgf(payload: bytes = b"\x42" * 64) -> bytes:
    header = b"wxgf" + bytes([11]) + b"\x00\x02" + struct.pack(">HH", 64, 48)
    body = b"\x00\x00\x00\x01" + payload
    return header + struct.pack(">I", len(body)) + body


def write_real_jpeg(path: Path, size: tuple[int, int] = (64, 48)) -> None:
    Image.new("RGB", size, (200, 40, 40)).save(path, "JPEG", quality=90)


def test_is_wxgf_magic_detection():
    assert is_wxgf(b"wxgf\x13\x00\x02")
    assert is_wxgf(synthetic_wxgf())
    assert not is_wxgf(b"\xff\xd8\xff\xe0jpeg")
    assert not is_wxgf(b"wxg")
    assert not is_wxgf(b"")


def test_find_partitions_reads_length_prefix():
    data = synthetic_wxgf(b"\x11" * 32)
    assert wxgf._find_partitions(data) == [(15, 36)]


def test_payload_candidates_start_at_annex_b_start_code():
    data = synthetic_wxgf(b"\x24" * 40)
    candidates = wxgf._payload_candidates(data)
    assert candidates == [b"\x00\x00\x00\x01" + b"\x24" * 40]


def test_decode_wxgf_false_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(wxgf.shutil, "which", lambda name: None)
    monkeypatch.setattr(wxgf, "_FFMPEG_FALLBACK", tmp_path / "missing-ffmpeg")
    src = tmp_path / "a.jpg"
    src.write_bytes(synthetic_wxgf())
    dst = tmp_path / "out.jpg"
    assert decode_wxgf(src, dst) is False
    assert not dst.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg"]


def test_decode_wxgf_false_for_non_wxgf_input(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("ffmpeg must not run for non-wxgf input")

    monkeypatch.setattr(wxgf.subprocess, "run", boom)
    src = tmp_path / "real.jpg"
    write_real_jpeg(src)
    dst = tmp_path / "out.jpg"
    assert decode_wxgf(src, dst) is False
    assert not dst.exists()


def test_decode_wxgf_success_with_fake_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(wxgf, "_find_ffmpeg", lambda: "/fake/ffmpeg")
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs["input"]
        write_real_jpeg(Path(cmd[-1]))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(wxgf.subprocess, "run", fake_run)
    src = tmp_path / "a.jpg"
    src.write_bytes(synthetic_wxgf(b"\x24" * 40))
    dst = tmp_path / "decoded.jpg"
    assert decode_wxgf(src, dst) is True
    with Image.open(dst) as img:
        assert img.size == (64, 48)
    assert seen["input"].startswith(b"\x00\x00\x00\x01")
    assert "-frames:v" in seen["cmd"]
    assert "hevc" in seen["cmd"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg", "decoded.jpg"]


def test_decode_wxgf_in_place_replaces_container(tmp_path, monkeypatch):
    monkeypatch.setattr(wxgf, "_find_ffmpeg", lambda: "/fake/ffmpeg")

    def fake_run(cmd, **kwargs):
        write_real_jpeg(Path(cmd[-1]))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(wxgf.subprocess, "run", fake_run)
    path = tmp_path / "a.jpg"
    path.write_bytes(synthetic_wxgf())
    assert decode_wxgf(path, path) is True
    with Image.open(path) as img:
        assert img.size == (64, 48)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg"]


def test_decode_wxgf_ffmpeg_failure_leaves_no_residue(tmp_path, monkeypatch):
    monkeypatch.setattr(wxgf, "_find_ffmpeg", lambda: "/fake/ffmpeg")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, b"", b"decode error")

    monkeypatch.setattr(wxgf.subprocess, "run", fake_run)
    src = tmp_path / "a.jpg"
    original = synthetic_wxgf()
    src.write_bytes(original)
    dst = tmp_path / "out.jpg"
    assert decode_wxgf(src, dst) is False
    assert not dst.exists()
    assert src.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg"]


def test_decode_wxgf_rejects_non_image_ffmpeg_output(tmp_path, monkeypatch):
    monkeypatch.setattr(wxgf, "_find_ffmpeg", lambda: "/fake/ffmpeg")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"not an image at all")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(wxgf.subprocess, "run", fake_run)
    src = tmp_path / "a.jpg"
    src.write_bytes(synthetic_wxgf())
    dst = tmp_path / "out.jpg"
    assert decode_wxgf(src, dst) is False
    assert not dst.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg"]


def test_decode_wxgf_timeout_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(wxgf, "_find_ffmpeg", lambda: "/fake/ffmpeg")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=20)

    monkeypatch.setattr(wxgf.subprocess, "run", fake_run)
    src = tmp_path / "a.jpg"
    src.write_bytes(synthetic_wxgf())
    dst = tmp_path / "out.jpg"
    assert decode_wxgf(src, dst) is False
    assert not dst.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg"]
