import threading
from hycad_monitor.__main__ import follow_file


def test_tail_eof_partial_line_rotation_and_truncation(tmp_path):
    path = tmp_path / 'events.jsonl'
    path.write_bytes(b'historical\n')
    stop = threading.Event()
    lines = follow_file(path, stop)
    assert next(lines) is None
    with path.open('ab') as f:
        f.write(b'{"part":')
    assert next(lines) is None
    with path.open('ab') as f:
        f.write(b'1}\n')
    assert next(lines) == b'{"part":1}'
    path.rename(tmp_path / 'rotated.jsonl')
    path.write_bytes(b'new\n')
    assert next(lines) == b'!source_reset'
    assert next(lines) == b'new'
    path.write_bytes(b'x\n')
    assert next(lines) == b'!source_reset'
    assert next(lines) == b'x'
    stop.set()
    lines.close()
