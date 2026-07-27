from pathlib import Path

from app.storage import finalize_files


def test_finalize_moves_media_and_deduplicates(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.jpg").write_bytes(b"same")
    (work / "b.jpg").write_bytes(b"same")
    (work / "video.mp4.part").write_bytes(b"partial")

    result = finalize_files(
        work_dir=work,
        download_root=tmp_path / "downloads",
        platform_folder="Instagram",
        created_at="2026-07-27T10:00:00+00:00",
    )
    assert len(result.files) == 1
    assert result.files[0].is_file()
    assert result.output_dir.name == "Instagram"
    assert result.output_dir.parent.name == "2026-07"
