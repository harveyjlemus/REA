import csv
import json

import demo
import fakeworld
from lib.ai_read import check_answer
from lib.cache import DomainMemo
from lib.state import RunDir


def test_offline_demo_passes(tmp_path):
    passed, total, lines = demo.run_demo(tmp_path)
    assert passed == total, "\n".join(lines)


def test_send_ready_holds_only_verified(tmp_path):
    demo.run_demo(tmp_path)
    with open(tmp_path / "run" / "send_ready.csv", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows and all(r["verification"] == "ok" and r["find_status"] == "found" for r in rows)
    assert all(r["company"] for r in rows), "original columns come along"


def test_every_unfound_row_has_a_reason(tmp_path):
    demo.run_demo(tmp_path)
    with open(tmp_path / "run" / "results.csv", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["find_status"] != "found":
                assert r["absent_reason"], r


def test_budgets_cover_the_whole_run_folder(tmp_path, cfg):
    run = RunDir(tmp_path / "run")
    fakeworld.write_input(tmp_path / "in.csv")
    _, rows = run.create(tmp_path / "in.csv", cfg)
    t = fakeworld.transport()
    memo = DomainMemo(tmp_path / "m.json")
    pipe, _ = demo.build(run, cfg, memo, t)
    pipe.run_rows(rows)
    used = pipe.budgets["mv"].used
    pipe2, _ = demo.build(run, cfg, memo, t)
    assert pipe2.budgets["mv"].used == used, "a resumed run starts from what was already spent"


def test_manual_round_trip(tmp_path, cfg):
    run = RunDir(tmp_path / "run")
    fakeworld.write_input(tmp_path / "in.csv")
    _, rows = run.create(tmp_path / "in.csv", cfg)
    t = fakeworld.transport()
    memo = DomainMemo(tmp_path / "m.json")
    pipe, _ = demo.build(run, cfg, memo, t, verify_mode="manual")
    pipe.run_rows(rows)
    fakeworld.answer_ai_read(run.ai_dir)
    rows = run.load_rows()
    pipe, _ = demo.build(run, cfg, memo, t, verify_mode="manual")
    assert pipe.run_rows(rows) == "done"
    assert not any(e.get("provider") == "millionverifier" for e in pipe.ledger.events())

    import export_guesses
    import import_report
    for rnd in range(1, 8):
        if export_guesses.main(["--run-dir", str(run.path)]) != 0:
            break
        upload = run.path / f"mv_upload_round{rnd}.csv"
        if not upload.exists():
            break
        report = run.path / f"round{rnd}_FULL_REPORT.csv"
        with open(upload, newline="") as fh, open(report, "w", newline="") as out:
            w = csv.writer(out)
            w.writerow(["email", "fef_row_id", "quality", "result", "free", "role"])
            for line in csv.DictReader(fh):
                e = line["email"]
                res = "catch_all" if e.endswith("@bluepeakhvac.test") else ("ok" if e in fakeworld.MV_OK else "invalid")
                w.writerow([e, line["fef_row_id"], "good", res, "no", "no"])
        import_report.main(["--run-dir", str(run.path), "--report", str(report)])
    with open(run.path / "results.csv", newline="") as fh:
        got = {r["company"]: r for r in csv.DictReader(fh) if r["find_status"] != "duplicate"}
    assert got["Northwind Roofing"]["founder_email"] == "cmendes@northwindroofing.test"
    assert got["Keystone Landscape"]["find_status"] == "found"
    assert got["Blue Peak HVAC"]["find_status"] == "risky"


def test_role_inbox_in_the_input_is_not_the_founder(tmp_path, cfg):
    p = tmp_path / "in.csv"
    p.write_text("company,website,email\nAcme Roofing,acme.test,info@acme.test\n")
    run = RunDir(tmp_path / "run")
    _, rows = run.create(p, cfg)
    pipe, _ = demo.build(run, cfg, DomainMemo(None), fakeworld.transport())
    pipe.wave_clean(rows)
    assert rows[0].business_email == "info@acme.test" and rows[0].found_emails == []
    assert rows[0].kind == "company_only"


def test_a_skipped_ai_answer_does_not_wait_forever(tmp_path, cfg):
    run = RunDir(tmp_path / "run")
    fakeworld.write_input(tmp_path / "in.csv")
    _, rows = run.create(tmp_path / "in.csv", cfg)
    memo = DomainMemo(tmp_path / "m.json")
    pipe, _ = demo.build(run, cfg, memo, fakeworld.transport())
    assert pipe.run_rows(rows) == "ai_read_pending"
    from lib import ai_read
    for batch in ai_read.pending_batches(run.ai_dir):
        (run.ai_dir / "answers" / batch.name).write_text("")  # the reader wrote nothing
    rows = run.load_rows()
    pipe, _ = demo.build(run, cfg, memo, fakeworld.transport())
    assert pipe.run_rows(rows) == "done"
    sage = next(r for r in rows if r.domain == "sagebrushdesign.test")
    assert "no answer" in sage.lane("ai_read")["outcome"]


def test_ai_answers_are_checked_against_the_page(tmp_path, cfg):
    page = tmp_path / "p.txt"
    page.write_text("Sagebrush Design is the studio of Elena Ruiz.\nShe founded it in 2015.")
    entry = {"row_id": "r1", "task": "find_owner", "first_name": None,
             "pages": [{"url": "https://s.test/about", "text_path": str(page), "kind": "about"}]}
    good = {"found": True, "name": "Elena Ruiz", "title": "Founder", "url": "https://s.test/about",
            "quote": "Sagebrush Design is the studio of Elena Ruiz."}
    assert check_answer(good, entry, cfg)[1] == "accepted"
    assert "not word-for-word" in check_answer({**good, "quote": "Elena Ruiz owns Sagebrush."}, entry, cfg)[1]
    assert "url" in check_answer({**good, "url": "https://elsewhere.test"}, entry, cfg)[1]
    assert "not inside the quote" in check_answer({**good, "name": "Maria Lopez"}, entry, cfg)[1]
    assert "founder" in check_answer({**good, "title": "Designer"}, entry, cfg)[1]
    last = {**entry, "task": "find_last_name", "first_name": "Dana"}
    assert "first name" in check_answer(good, last, cfg)[1]
    assert check_answer({"found": False, "reason": "nobody"}, entry, cfg)[0] is None


def test_offline_cli_never_touches_the_network(tmp_path, monkeypatch):
    import find_founders

    def boom(*a, **k):
        raise AssertionError("network used in --offline")
    monkeypatch.setattr("lib.http.Transport.get", boom)
    monkeypatch.setattr("lib.http.Transport.post_json", boom)
    fakeworld.write_input(tmp_path / "in.csv")
    code = find_founders.main(["--input", str(tmp_path / "in.csv"), "--run-dir", str(tmp_path / "run"),
                               "--yes", "--offline", "--ai-read", "off"])
    assert code == 0
    with open(tmp_path / "run" / "results.csv", newline="") as fh:
        statuses = {r["find_status"] for r in csv.DictReader(fh)}
    assert statuses <= {"skipped", "not_found", "duplicate", "unverified", "awaiting_verification"}
    report = (tmp_path / "run" / "run_report.md").read_text()
    assert "Run report" in report


def test_preview_prints_cards_and_estimate(tmp_path, monkeypatch, capsys):
    import find_founders

    monkeypatch.setattr("lib.http.Transport.get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    fakeworld.write_input(tmp_path / "in.csv")
    code = find_founders.main(["--input", str(tmp_path / "in.csv"), "--run-dir", str(tmp_path / "run"),
                               "--preview", "2", "--offline"])
    assert code == 0
    out = capsys.readouterr().out
    assert "## Sample rows" in out and "Estimate for the whole file" in out and "--yes" in out
    assert (tmp_path / "run" / "preview.md").is_file()
    state = json.loads((tmp_path / "run" / "mapping.json").read_text())
    assert state["fields"]["company"] == "company"
