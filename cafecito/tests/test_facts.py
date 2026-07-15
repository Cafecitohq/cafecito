

def test_concurrent_record_green_never_crashes(tmp_path):
    # regression: a shared temp filename let one racer's os.replace steal
    # another's file — the losing gate thread died with FileNotFoundError
    # (seen live: CI 3.14, test_parallel, 2026-07-15). Per-writer mkstemp
    # makes every replace source exist; lost updates remain acceptable.
    import concurrent.futures
    import json as _json

    from cafecito.facts import FactsStore

    store_dir = tmp_path
    errors = []

    def hammer(worker: int) -> None:
        try:
            s = FactsStore(store_dir)
            for i in range(100):
                s.record_green(f"k-{worker}-{i}", f"t{worker}.py")
        except OSError as exc:  # the bug's signature
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(hammer, range(8)))

    assert not errors, errors
    facts = _json.loads((store_dir / "facts.json").read_text())
    assert facts, "at least one racer's facts survived"
    # no orphaned temp files left behind
    assert not list(store_dir.glob("*.tmp"))
