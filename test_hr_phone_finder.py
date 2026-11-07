import csv
from datetime import date

import pytest

import hr_phone_finder as finder

ACME = finder.parse_company("Acme | Acme Software Solutions")
BETA = finder.parse_company("Beta Systems")


def numbers(text, title="", company=ACME):
    return [n[0] for n in finder.extract_numbers(text, title, company)]


def test_keeps_number_near_company_and_hr_words():
    assert numbers("Acme is hiring. Contact HR Priya at +91 98765 43210.") == ["+919876543210"]


def test_alias_and_ten_digit_format():
    assert numbers("Acme Software Solutions recruiter: 9876543210") == ["+919876543210"]


def test_rejects_number_without_hr_context():
    assert numbers("Acme customer support: +91 98765 43210") == []


def test_rejects_number_without_company():
    assert numbers("Other Corp HR desk: +91 98765 43210") == []


def test_company_in_title_counts():
    assert numbers("HR contact: +91 98765 43210", title="Careers at Acme") == ["+919876543210"]


def test_company_name_must_be_a_whole_word():
    assert numbers("Acmeville HR desk: +91 98765 43210") == []


def test_hr_word_must_be_close_to_the_number():
    text = "Acme HR team. " + "filler words " * 40 + "Call +91 98765 43210"
    assert numbers(text) == []


def test_rejects_toll_free():
    assert numbers("Acme HR helpline 1800 123 4567") == []


def test_ignores_dates_and_pincodes():
    assert numbers("Acme HR office, Chennai 600001, drive on 12-11-2026") == []


def test_landline_is_labelled():
    result = finder.extract_numbers("Acme HR office: 044 2345 6789", "", ACME)
    assert result[0][2] == "landline"


def test_agency_is_flagged():
    result = finder.extract_numbers("ABC Consultancy, hiring for Acme, HR 98765 43210", "", ACME)
    assert result[0][3] is True


def name_of(text):
    return finder.extract_numbers(text, "Careers at Acme", ACME)[0][4]


@pytest.mark.parametrize("text, expected", [
    ("Contact HR Priya at +91 98765 43210", "Priya"),
    ("HR Manager: Priya Sharma, 98765 43210", "Priya Sharma"),
    ("Acme HR contact person: Ms. Priya Sharma - 98765 43210", "Priya Sharma"),
    ("Priya Sharma (HR) - 98765 43210", "Priya Sharma"),
    ("Karthik Raj, Talent Acquisition Lead, Mobile: 98765 43210", "Karthik Raj"),
    ("Acme HR desk 98765 43210 (Priya)", "Priya"),
    ("Recruiter: S. Karthik 98765 43210", "S. Karthik"),
    ("Acme HR desk: 98765 43210", ""),
    ("Acme HR 98765 43210", ""),
    ("Acme Software Solutions HR 98765 43210", ""),
    ("Global Tech HR 98765 43210", ""),
    ("Please contact HR Team on 98765 43210", ""),
    ("HR Department, Chennai 98765 43210", ""),
    ("HR: 98765 43210 Walk in on Sunday", ""),
    ("Priya Sharma from HR met us. Office line 98765 43210", ""),
    ("HR MANAGER: PRIYA SHARMA, 98765 43210", "Priya Sharma"),
    ("PRIYA SHARMA (HR) - 98765 43210", "Priya Sharma"),
    ("CONTACT HR S. KARTHIK 98765 43210", "S. Karthik"),
    ("Contact HR PRIYA SHARMA at 98765 43210", "Priya Sharma"),
    ("ABC HR 98765 43210", ""),
    ("KARTHIK (HR) - 98765 43210", ""),
    ("JOIN OUR HR 98765 43210", ""),
    ("ACME HR 98765 43210", ""),
    ("IT HR 98765 43210", ""),
])
def test_hr_name(text, expected):
    assert name_of(text) == expected


def test_conflicting_names_are_flagged():
    contacts = finder.collect(
        [ACME], "Chennai",
        lambda query: [{"link": "https://a.example/1"}, {"link": "https://a.example/2"}],
        {"https://a.example/1": ("", "Acme HR Priya 98765 43210"),
         "https://a.example/2": ("", "Acme HR Divya 98765 43210")}.get,
        log=lambda *_: None,
    )
    row = finder.to_rows(contacts)[0]
    assert row["hr_name"] == "Priya"
    assert "different names found: Priya, Divya" in row["check_first"]


def test_blocked_domains():
    assert finder.is_blocked("https://in.linkedin.com/posts/abc")
    assert finder.is_blocked("https://www.zoominfo.com/p/abc")
    assert not finder.is_blocked("https://notlinkedin.com/page")
    assert not finder.is_blocked("https://acme.example/careers")


def pages():
    return {
        "https://a.example/1": ("Careers", "Acme HR: +91 98765 43210"),
        "https://a.example/2": ("Jobs", "Acme recruiter 98765 43210"),
        "https://a.example/3": ("Jobs", "Beta Systems HR 98765 43210, Beta Systems HR 91234 56789"),
        "https://a.example/4": ("Jobs", "Acme HR 90000 12345"),
        "https://www.linkedin.com/posts/x": ("Post", "Acme HR 99999 11111"),
    }


def fake_search(query):
    if "Acme" in query:
        urls = ["https://a.example/1", "https://a.example/2",
                "https://a.example/4", "https://www.linkedin.com/posts/x"]
    else:
        urls = ["https://a.example/3"]
    return [{"link": url} for url in urls]


def run(do_not_call=frozenset(), search_fn=fake_search):
    contacts = finder.collect(
        [ACME, BETA], "Chennai", search_fn, pages().get, do_not_call, log=lambda *_: None
    )
    return finder.to_rows(contacts)


def test_end_to_end_merges_sources_and_flags_shared_numbers():
    rows = run()
    acme = next(r for r in rows if r["company"] == "Acme" and r["sources_found"] == 2)
    assert acme["phone"] == "098765 43210"
    assert "several companies" in acme["check_first"]
    beta_only = next(r for r in rows if r["phone"] == "091234 56789")
    assert beta_only["check_first"] == "only one source"


def test_end_to_end_never_reads_blocked_sites():
    assert not any("99999" in r["phone"] for r in run())


def test_do_not_call_is_honoured():
    rows = run(do_not_call={finder.e164("90000 12345")})
    assert not any(r["phone"] == "090000 12345" for r in rows)


def test_stops_cleanly_when_credits_run_out():
    def stops(query):
        if "Beta" in query:
            raise finder.SearchStopped("no credits")
        return fake_search(query)

    rows = run(search_fn=stops)
    assert {r["company"] for r in rows} == {"Acme"}


def test_csv_output(tmp_path):
    path = tmp_path / "out.csv"
    finder.write_csv(path, run())
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == finder.CSV_COLUMNS
    assert all(r["company"] and r["phone"] for r in rows)


TODAY = date(2026, 11, 7)


@pytest.mark.parametrize("raw, expected", [
    ("3 days ago", "2026-11-04"),
    ("2 weeks ago", "2026-10-24"),
    ("5 hours ago", "2026-11-07"),
    ("Mar 3, 2025", "2025-03-03"),
    ("3 March 2025", "2025-03-03"),
    ("2025-03-03T10:00:00+05:30", "2025-03-03"),
])
def test_dates_are_understood(raw, expected):
    assert finder.parse_date_text(raw, TODAY).isoformat() == expected


@pytest.mark.parametrize("raw", ["", None, "soon", "2030-01-01"])
def test_unclear_or_future_dates_are_ignored(raw):
    assert finder.parse_date_text(raw, TODAY) is None


def test_recent_pages_are_read_first():
    results = [
        {"link": "old", "date": "Jan 1, 2020"},
        {"link": "undated"},
        {"link": "recent", "date": "2 weeks ago"},
    ]
    assert [r["link"] for r in finder.prefer_recent(results, TODAY)] == ["recent", "undated", "old"]


def test_newest_date_is_kept_and_old_pages_flagged():
    contacts = finder.collect(
        [ACME], "Chennai",
        lambda query: [{"link": "https://a.example/1", "date": "Jan 1, 2020"},
                       {"link": "https://a.example/2", "date": "Jan 1, 2021"},
                       {"link": "https://a.example/3"}],
        {"https://a.example/1": ("", "Acme HR 98765 43210"),
         "https://a.example/2": ("", "Acme HR 98765 43210"),
         "https://a.example/3": ("", "Acme HR 91234 56789", "2026-10-01")}.get,
        log=lambda *_: None,
    )
    rows = finder.to_rows(contacts)
    assert rows[0]["phone"] == "091234 56789"  # newest page first
    assert rows[0]["page_date"] == "2026-10-01"
    assert rows[1]["page_date"] == "2021-01-01"
    assert "over 18 months old" in rows[1]["check_first"]
    assert "over 18 months" not in rows[0]["check_first"]


def make_pdf(text):
    """Build a small real PDF containing text."""
    stream = f"BT /F1 12 Tf 40 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Title (Campus notice) /CreationDate (D:20260315120000) >>",
    ]
    body = b"%PDF-1.4\n"
    offsets = []
    for number, content in enumerate(objects, 1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + content + b"\nendobj\n"
    xref = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode()
    body += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\n"
             f"startxref\n{xref}\n%%EOF\n").encode()
    return body


class FakeResponse:
    def __init__(self, body=b"", kind="text/html", status=200):
        self.body, self.status_code = body, status
        self.headers = {"content-type": kind}
        self.ok = status < 400
        self.text = body.decode("latin-1")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_content(self, size):
        yield self.body


class FakeSession:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, **_):
        return self.pages.get(url, FakeResponse(status=404))


def reader(pages):
    return finder.PageReader(FakeSession(pages), delay=0)


def test_reads_numbers_and_date_from_a_pdf():
    url = "https://a.example/notice.pdf"
    page = reader({url: FakeResponse(make_pdf("Acme HR Manager: Priya Sharma, 98765 43210"),
                                     "application/pdf")}).read(url)
    assert page[0] == "Campus notice"
    assert page[2] == "2026-03-15"
    found = finder.extract_numbers(page[1], page[0], ACME)
    assert found[0][0] == "+919876543210"
    assert found[0][4] == "Priya Sharma"


def test_pdf_detected_from_address_when_type_is_vague():
    url = "https://a.example/notice.PDF"
    page = reader({url: FakeResponse(make_pdf("Acme HR 98765 43210"), "application/octet-stream")}).read(url)
    assert "98765" in page[1]


def test_broken_pdf_is_skipped():
    url = "https://a.example/bad.pdf"
    assert reader({url: FakeResponse(b"not a pdf at all", "application/pdf")}).read(url) is None


def test_other_file_types_are_skipped():
    url = "https://a.example/file.zip"
    assert reader({url: FakeResponse(b"x", "application/zip")}).read(url) is None


def test_reads_published_date_from_html():
    url = "https://a.example/post"
    html = (b'<html><head><title>Job</title><meta property="article:published_time" '
            b'content="2026-02-01T09:00:00Z"></head><body>Acme HR</body></html>')
    assert reader({url: FakeResponse(html)}).read(url)[2] == "2026-02-01"


def test_robots_rules_still_apply_to_pdfs():
    url = "https://a.example/notice.pdf"
    pages = {
        "https://a.example/robots.txt": FakeResponse(b"User-agent: *\nDisallow: /"),
        url: FakeResponse(make_pdf("Acme HR 98765 43210"), "application/pdf"),
    }
    assert reader(pages).read(url) is None


def test_store_round_trip_and_do_not_call(tmp_path):
    path = tmp_path / "data.json"
    store = finder.Store(path)
    store.contacts[("Acme", "+919876543210")] = finder.Contact(
        "Acme", "+919876543210", "098765 43210", "mobile", names=["Priya"],
        urls=["https://a.example/1"], page_date="2026-01-01",
    )
    store.mark_searched("Acme", TODAY)
    store.save()

    loaded = finder.Store(path).load()
    assert loaded.was_searched("ACME")
    assert loaded.searched_on("acme") == "2026-11-07"
    assert finder.to_rows(loaded.contacts)[0]["hr_name"] == "Priya"

    loaded.remove_numbers({"+919876543210"})
    assert loaded.contacts == {}


def test_damaged_store_is_kept_not_overwritten(tmp_path):
    path = tmp_path / "data.json"
    path.write_text("{ not json")
    store = finder.Store(path).load()
    assert store.contacts == {}
    assert (tmp_path / "data.json.damaged").read_text() == "{ not json"


def test_failed_search_means_company_is_not_done():
    done = []

    def failing(query):
        raise finder.requests.ConnectionError("network down")

    finder.collect(
        [ACME], "Chennai", failing, {}.get, log=lambda *_: None,
        on_event=lambda kind, **d: done.append(d["complete"]) if kind == "company_done" else None,
    )
    assert done == [False]


def test_lines_with_no_name_and_repeats_are_ignored():
    companies = finder.parse_companies(["Acme", "|", " | ", "ACME | Acme Inc", "Beta"])
    assert [c.name for c in companies] == ["Acme", "Beta"]


@pytest.mark.parametrize("raw", [123, ["x"], {"a": 1}, "99999999999999 years ago"])
def test_strange_dates_are_ignored(raw):
    assert finder.parse_date_text(raw, TODAY) is None


def test_is_old_tolerates_a_bad_date():
    assert finder.is_old("not-a-date") is False


def api_response(status=200, body=None, text=""):
    response = FakeResponse(text.encode(), status=status)
    response.status_code = status
    response.json = lambda: body if body is not None else {}

    def raise_for_status():
        if status >= 400:
            raise finder.requests.HTTPError(f"{status}", response=response)
    response.raise_for_status = raise_for_status
    return response


class FakeApi:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def post(self, *_, **__):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def run_search(api):
    return finder.search(api, "key", "query", retry_delay=0)


def test_search_retries_a_brief_network_problem():
    api = FakeApi(finder.requests.ConnectionError(), api_response(body={"organic": [{"link": "x"}]}))
    assert run_search(api) == [{"link": "x"}]
    assert api.calls == 2


def test_search_retries_a_server_error():
    api = FakeApi(api_response(503), api_response(502), api_response(body={"organic": []}))
    assert run_search(api) == []
    assert api.calls == 3


def test_search_gives_up_after_its_tries():
    api = FakeApi(*[finder.requests.Timeout() for _ in range(finder.SEARCH_ATTEMPTS)])
    with pytest.raises(finder.requests.Timeout):
        run_search(api)
    assert api.calls == finder.SEARCH_ATTEMPTS


@pytest.mark.parametrize("status, text", [
    (401, "bad key"), (403, "forbidden"), (402, "pay"), (429, "slow down"), (400, "Not enough credits"),
])
def test_key_and_credit_problems_stop_without_retrying(status, text):
    api = FakeApi(api_response(status, text=text))
    with pytest.raises(finder.SearchStopped):
        run_search(api)
    assert api.calls == 1


def test_other_bad_requests_count_as_failed_searches():
    with pytest.raises(finder.requests.HTTPError):
        run_search(FakeApi(api_response(400, text="bad query")))


@pytest.mark.parametrize("body", [[], {"organic": "nope"}, {"organic": None}, {"other": 1}])
def test_unexpected_search_replies_mean_no_results(body):
    assert run_search(FakeApi(api_response(body=body))) == []


def test_strange_entries_in_results_are_dropped():
    assert run_search(FakeApi(api_response(body={"organic": ["x", 1, {"link": "y"}]}))) == [{"link": "y"}]


def test_one_bad_page_does_not_end_the_run():
    def read(url):
        if "boom" in url:
            raise RuntimeError("parser exploded")
        if "bad-shape" in url:
            return 42
        return ("", "Acme HR 98765 43210")

    statuses = []
    contacts = finder.collect(
        [ACME], "Chennai",
        lambda query: [{"link": "https://a.example/boom"}, {"link": "http://[broken"},
                       {"link": 7}, {"link": "https://a.example/bad-shape"},
                       {"link": "https://a.example/good"}],
        read, log=lambda *_: None,
        on_event=lambda kind, **d: statuses.append(d["status"]) if kind == "page" else None,
    )
    assert statuses.count("unreadable") == 3
    assert list(contacts) == [("Acme", "+919876543210")]


def test_stops_when_searches_keep_failing():
    calls = []

    def failing(query):
        calls.append(query)
        raise finder.requests.ConnectionError("network down")

    events = []
    finder.collect(
        [ACME, BETA, finder.parse_company("Gamma")], "Chennai", failing, {}.get, log=lambda *_: None,
        on_event=lambda kind, **d: events.append((kind, d)),
    )
    assert len(calls) == finder.MAX_FAILED_SEARCHES_IN_A_ROW
    assert any(kind == "stopped" and "keep failing" in d["reason"] for kind, d in events)


def test_a_success_resets_the_failure_count():
    count = {"n": 0}

    def flaky(query):
        count["n"] += 1
        if count["n"] % 2:
            raise finder.requests.ConnectionError("blip")
        return []

    events = []
    finder.collect([ACME, BETA], "Chennai", flaky, {}.get, log=lambda *_: None,
                   on_event=lambda kind, **d: events.append(kind))
    assert "stopped" not in events


def test_search_errors_are_short_and_plain():
    assert "timed out" in finder.describe_search_error(finder.requests.Timeout("x" * 500))
    assert "connect" in finder.describe_search_error(finder.requests.ConnectionError("x" * 500))


def test_damaged_store_gives_a_warning(tmp_path):
    path = tmp_path / "data.json"
    path.write_text("{ not json")
    store = finder.Store(path).load()
    assert "damaged" in store.warning
    store.save()  # a fresh list can be saved over the moved-aside file
    assert path.exists()


@pytest.mark.parametrize("content", ["[]", '{"contacts": 5, "searched": {}}', '{"contacts": []}',
                                     '{"contacts": [{"nope": 1}], "searched": {}}',
                                     '{"contacts": [], "searched": []}'])
def test_wrong_shape_store_counts_as_damaged(tmp_path, content):
    path = tmp_path / "data.json"
    path.write_text(content)
    assert "damaged" in finder.Store(path).load().warning


def test_bad_entries_in_store_are_dropped_and_good_ones_kept(tmp_path):
    good = {"company": "Acme", "phone_key": "+919876543210", "phone": "098765 43210",
            "line_type": "mobile", "urls": ["https://a.example"], "page_date": "garbage"}
    bad = dict(good, urls=[], phone_key="+919123456789")
    path = tmp_path / "data.json"
    path.write_text(finder.json.dumps({"contacts": [good, bad], "searched": {}}))
    store = finder.Store(path).load()
    rows = finder.to_rows(store.contacts)
    assert [r["phone"] for r in rows] == ["098765 43210"]
    assert rows[0]["page_date"] == ""


def test_unreadable_store_is_never_overwritten(tmp_path):
    store = finder.Store(tmp_path / "data.json")
    store.locked = True
    with pytest.raises(OSError):
        store.save()
    assert not (tmp_path / "data.json").exists()
