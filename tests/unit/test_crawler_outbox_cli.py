from __future__ import annotations

from careerops.cli import crawler_outbox


def test_crawler_outbox_cli_uses_database_dispatch_reader_not_file_metadata() -> None:
    parser = crawler_outbox._parser()

    options = {action.dest for action in parser._actions}
    assert "root" in options
    assert "owner" in options
    assert "limit" in options
    assert "lease_seconds" in options
    assert "poll_seconds" in options
    assert "metadata_dir" not in options
