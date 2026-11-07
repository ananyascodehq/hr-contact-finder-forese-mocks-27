import time

import pytest

import hr_phone_finder as finder
import webapp

PAGES = {
    "https://a.example/1": ("Careers", "Acme HR: +91 98765 43210"),
    "https://a.example/2": ("Jobs", "Nothing useful here"),
}


def fake_backend(key):
    def search(query):
        if key == "bad-key":
            raise finder.SearchStopped("search API returned 401")
        if "Acme" in query:
            return [{"link": "https://a.example/1"}, {"link": "https://a.example/2"},
                    {"link": "https://www.linkedin.com/posts/x"}, {"link": "https://a.example/gone"}]
        return []
    return search, PAGES.get


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "COMPANIES_FILE", tmp_path / "companies.txt")
    monkeypatch.setattr(webapp, "DO_NOT_CALL_FILE", tmp_path / "do_not_call.txt")
    monkeypatch.setattr(webapp, "OUTPUT_FILE", tmp_path / "hr_contacts.csv")
    monkeypatch.setattr(webapp, "STORE_FILE", tmp_path / "finder_data.json")
    monkeypatch.setattr(webapp, "store", None)
    monkeypatch.setattr(webapp, "make_backend", fake_backend)
    monkeypatch.setattr(webapp, "api_key", "")
    monkeypatch.setattr(webapp, "run", webapp.empty_run())
    return webapp.app.test_client()


def start(client, **overrides):
    body = {"companies": "Acme\nBeta\n", "city": "Chennai", "api_key": "k", "do_not_call": ""}
    return client.post("/api/start", json={**body, **overrides})


def wait_until_finished(client):
    for _ in range(200):
        state = client.get("/api/state").get_json()
        if state["status"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError("run never finished")


def test_full_run_reports_progress_and_saves(client):
    assert start(client).status_code == 200
    state = wait_until_finished(client)
    assert state["status"] == "done"
    assert [c["found"] for c in state["companies"]] == [1, 0]
    assert all(c["state"] == "done" for c in state["companies"])
    assert state["rows"][0]["phone"] == "098765 43210"
    assert state["pages"] == 2
    log = " ".join(e["text"] for e in state["events"])
    assert "Found 1 HR number" in log
    assert "do-not-read list" in log
    assert "Couldn't read" in log
    assert webapp.OUTPUT_FILE.exists()
    assert "098765 43210" in client.get("/results.csv").get_data(as_text=True)


def test_saves_inputs_and_honours_do_not_call(client):
    start(client, do_not_call="98765 43210\n")
    state = wait_until_finished(client)
    assert state["rows"] == []
    assert "98765 43210" in webapp.DO_NOT_CALL_FILE.read_text()
    assert "Acme" in webapp.COMPANIES_FILE.read_text()


def test_bad_key_ends_as_stopped_with_a_reason(client):
    start(client, api_key="bad-key")
    state = wait_until_finished(client)
    assert state["status"] == "stopped"
    assert any("401" in e["text"] for e in state["events"])
    assert state["companies"][0]["state"] == "stopped"


def test_stop_button(client):
    webapp.stop_requested.clear()
    original = fake_backend

    def slow_backend(key):
        search, read = original(key)

        def slow_read(url):
            webapp.stop_requested.set()  # the user clicks Stop during the first page
            return read(url)
        return search, slow_read

    webapp.make_backend = slow_backend
    start(client)
    state = wait_until_finished(client)
    assert state["status"] == "stopped"
    assert state["pages"] == 1


def test_requires_companies_and_key(client):
    assert start(client, companies="# only a comment\n").status_code == 400
    assert start(client, api_key="").status_code == 400


def test_second_run_refused_while_running(client):
    webapp.run["status"] = "running"
    assert start(client).status_code == 409


def test_start_needs_json_so_other_sites_cant_trigger_it(client):
    assert client.post("/api/start", data="companies=Acme").status_code == 415


def test_other_hostnames_refused(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "127.0.0.1:5057"}).status_code == 200


def test_key_is_never_returned(client):
    start(client, api_key="secret-key")
    wait_until_finished(client)
    assert "secret-key" not in client.get("/api/config").get_data(as_text=True)
    assert "secret-key" not in client.get("/api/state").get_data(as_text=True)


def searches_for(state, company):
    return [e for e in state["events"] if e["text"].startswith(company + " (")]


def restart_app(client):
    """Forget everything in memory, as closing and reopening the finder does."""
    webapp.store = None
    webapp.run = webapp.saved_run()


def test_results_survive_a_restart_and_appear_on_opening(client):
    start(client, companies="Acme\n")
    wait_until_finished(client)
    restart_app(client)
    state = client.get("/api/state").get_json()
    assert state["status"] == "idle"
    assert state["rows"][0]["phone"] == "098765 43210"
    assert state["companies"][0]["state"] == "skipped"
    assert state["companies"][0]["found"] == 1
    assert "098765 43210" in client.get("/results.csv").get_data(as_text=True)


def test_new_numbers_are_added_to_old_ones(client):
    start(client, companies="Acme\n")
    wait_until_finished(client)
    PAGES["https://a.example/1"] = ("Careers", "Acme HR: +91 98765 43210, Acme HR 91234 56789")
    try:
        start(client, companies="Acme\n", only="Acme")
        state = wait_until_finished(client)
    finally:
        PAGES["https://a.example/1"] = ("Careers", "Acme HR: +91 98765 43210")
    assert {r["phone"] for r in state["rows"]} == {"098765 43210", "091234 56789"}
    assert "1 new number, 2 saved in total" in state["events"][-1]["text"]
    assert "091234 56789" in webapp.OUTPUT_FILE.read_text(encoding="utf-8-sig")


def test_searched_companies_are_skipped_and_use_no_searches(client):
    start(client, companies="Acme\n")
    first = wait_until_finished(client)
    assert first["searches"] == len(finder.SEARCH_PHRASES)
    start(client, companies="Acme\nBeta\n")
    second = wait_until_finished(client)
    assert searches_for(second, "Acme") == []
    assert len(searches_for(second, "Beta")) == 1
    assert second["searches"] == len(finder.SEARCH_PHRASES)
    assert second["companies"][0]["state"] == "skipped"
    assert second["companies"][0]["found"] == 1


def test_search_again_runs_only_that_company(client):
    start(client)
    wait_until_finished(client)
    start(client, only="Beta")
    state = wait_until_finished(client)
    assert searches_for(state, "Acme") == []
    assert len(searches_for(state, "Beta")) == 1
    assert state["companies"][0]["state"] == "skipped"


def test_search_again_for_a_removed_company_is_refused(client):
    assert start(client, only="Gone").status_code == 400


def test_nothing_to_search_needs_no_key(client):
    start(client, companies="Acme\n")
    wait_until_finished(client)
    webapp.api_key = ""
    assert start(client, companies="Acme\n", api_key="").status_code == 200
    state = wait_until_finished(client)
    assert state["status"] == "done"
    assert any("Nothing new to search" in e["text"] for e in state["events"])


def test_stopped_company_is_not_marked_done(client):
    def stops_midway(key):
        search, read = fake_backend(key)

        def read_then_stop(url):
            webapp.stop_requested.set()
            return read(url)
        return search, read_then_stop

    webapp.make_backend = stops_midway
    start(client, companies="Acme\n")
    state = wait_until_finished(client)
    assert state["status"] == "stopped"
    assert state["rows"]  # what it found is kept
    assert not webapp.get_store().was_searched("Acme")


def test_do_not_call_removes_a_number_already_saved(client):
    start(client, companies="Acme\n")
    wait_until_finished(client)
    start(client, companies="Acme\n", do_not_call="98765 43210\n")
    state = wait_until_finished(client)
    assert state["rows"] == []
    restart_app(client)
    assert client.get("/api/state").get_json()["rows"] == []
    assert "98765" not in webapp.STORE_FILE.read_text()
    assert "43210" not in webapp.OUTPUT_FILE.read_text(encoding="utf-8-sig")


def test_key_is_not_written_to_the_saved_file(client):
    start(client, api_key="secret-key")
    wait_until_finished(client)
    assert "secret-key" not in webapp.STORE_FILE.read_text()
