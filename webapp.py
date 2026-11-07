#!/usr/bin/env python3
"""Local web page for running the HR phone finder and watching it work.

Usage:
    python webapp.py
Then open http://127.0.0.1:5057 (it opens by itself unless you pass --no-browser).
Only this computer can reach the page.
"""
import argparse
import csv
import io
import logging
import os
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, Response, abort, jsonify, request, send_file

import hr_phone_finder as finder

BASE = Path(__file__).resolve().parent
COMPANIES_FILE = BASE / "companies.txt"
DO_NOT_CALL_FILE = BASE / "do_not_call.txt"
OUTPUT_FILE = BASE / "hr_contacts.csv"
STORE_FILE = BASE / "finder_data.json"
PORT = 5057

app = Flask(__name__)
lock = threading.Lock()
stop_requested = threading.Event()
# Held in memory only, never written to disk.
api_key = os.environ.get("SERPER_API_KEY", "")
store = None


def get_store():
    global store
    if store is None:
        store = finder.Store(STORE_FILE).load()
    return store


def empty_run():
    return {"status": "idle", "events": [], "rows": [], "companies": [], "pages": 0, "searches": 0}


run = empty_run()


def make_backend(key):
    """Return the (search, read page) functions a run uses."""
    session = requests.Session()
    session.headers["User-Agent"] = finder.USER_AGENT
    reader = finder.PageReader(session)
    return (lambda query: finder.search(session, key, query)), reader.read


def clean_lines(text):
    return [s for s in (line.strip() for line in text.splitlines()) if s and not s.startswith("#")]


def read_text(path):
    return path.read_text(encoding="utf-8-sig") if path.exists() else ""


def rows_to_csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=finder.CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def add_event(level, text, url=None):
    run["events"].append({
        "time": datetime.now().strftime("%H:%M:%S"), "level": level, "text": text, "url": url,
    })


def count_found(entries, rows):
    found = {}
    for row in rows:
        found[row["company"]] = found.get(row["company"], 0) + 1
    for entry in entries:
        entry["found"] = found.get(entry["name"], 0)


def company_entries(companies, st, to_search):
    """The company list shown on the page; already-searched ones are marked as skipped."""
    searching = {c.name for c in to_search}
    return [{
        "name": c.name,
        "state": "waiting" if c.name in searching or not st.was_searched(c.name) else "skipped",
        "found": 0,
        "searched_on": st.searched_on(c.name),
    } for c in companies]


def saved_run():
    """What the page shows on opening: results and progress from earlier runs."""
    st = get_store()
    saved = empty_run()
    saved["rows"] = finder.to_rows(st.contacts)
    companies = [finder.parse_company(line) for line in clean_lines(read_text(COMPANIES_FILE))]
    saved["companies"] = company_entries(companies, st, [])
    count_found(saved["companies"], saved["rows"])
    return saved


def describe_event(kind, details, total):
    """Turn a finder progress event into a line for the activity log."""
    if kind == "company":
        return "heading", f"{details['name']} ({details['index']} of {total})", None
    if kind == "search":
        count = details["results"]
        return "info", f"Searched, {count} result{'s' if count != 1 else ''}: {details['query']}", None
    if kind == "search_failed":
        return "warn", f"Search failed: {details['error']}", None
    if kind == "stopped":
        return "warn", f"Run ended early: {details['reason']}", None
    # A page was handled.
    url = details["url"]
    if details["status"] == "skipped":
        return "muted", "Skipped, site is on the do-not-read list", url
    if details["status"] == "unreadable":
        return "muted", "Couldn't read, or the site doesn't allow bots", url
    if details["found"]:
        count = details["found"]
        dated = f" (page from {details['date']})" if details.get("date") else ""
        return "found", f"Found {count} HR number{'s' if count != 1 else ''}{dated}", url
    return "muted", "Read, no HR number", url


def worker(to_search, city, do_not_call, search_fn, read_fn, st):
    contacts = st.contacts
    numbers_before = len(contacts)
    outcome = "done"

    def entry_for(name):
        return next(e for e in run["companies"] if e["name"] == name)

    def on_event(kind, **details):
        nonlocal outcome
        if kind == "company_done":
            if details["complete"]:
                st.mark_searched(details["name"])
            st.save()  # a crash or closed window loses at most the current company
            with lock:
                entry = entry_for(details["name"])
                entry["state"] = "done" if details["complete"] else "incomplete"
                entry["searched_on"] = st.searched_on(details["name"])
            return
        if kind == "stopped":
            outcome = "stopped"
        level, text, url = describe_event(kind, details, len(to_search))
        rows = finder.to_rows(contacts)
        with lock:
            add_event(level, text, url)
            run["rows"] = rows
            count_found(run["companies"], rows)
            if kind == "company":
                entry_for(details["name"])["state"] = "running"
            elif kind == "search":
                run["searches"] += 1
            elif kind == "page" and details["status"] == "read":
                run["pages"] += 1

    try:
        finder.collect(
            to_search, city, search_fn, read_fn, do_not_call,
            log=lambda *_: None, on_event=on_event, should_stop=stop_requested.is_set,
            contacts=contacts,
        )
    except Exception as error:  # keep the page informed instead of hanging on "running"
        outcome = "failed"
        with lock:
            add_event("warn", f"Something went wrong: {error}")

    rows = finder.to_rows(contacts)
    new_numbers = len(contacts) - numbers_before
    summary = f"{new_numbers} new number{'s' if new_numbers != 1 else ''}, {len(rows)} saved in total"
    problems = []
    try:
        st.save()
    except OSError as error:
        problems.append(f"Couldn't save {st.path.name}: {error}")
    try:
        finder.write_csv(OUTPUT_FILE, rows)
    except OSError:  # usually the CSV is open in Excel, which locks it on Windows
        problems.append(f"Couldn't update {OUTPUT_FILE.name}. Close it if it's open, then use Download CSV.")
    with lock:
        run["rows"] = rows
        run["status"] = outcome
        count_found(run["companies"], rows)
        for entry in run["companies"]:
            if entry["state"] == "running":
                entry["state"] = "done" if outcome == "done" else "stopped"
        add_event("heading", f"Finished. {summary}.")
        for problem in problems:
            add_event("warn", problem)


@app.before_request
def only_this_computer():
    # Stops other websites reaching this page through a look-alike domain name.
    if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        abort(403)


@app.get("/")
def index():
    return send_file(BASE / "ui.html")


@app.get("/api/config")
def config():
    return jsonify(
        companies=read_text(COMPANIES_FILE),
        do_not_call=read_text(DO_NOT_CALL_FILE),
        has_key=bool(api_key),
    )


@app.post("/api/start")
def start():
    global api_key, run
    data = request.get_json()  # requires a JSON body, which other websites can't send here
    lines = clean_lines(data.get("companies", ""))
    city = data.get("city", "").strip() or "Chennai"
    key = data.get("api_key", "").strip() or api_key
    only = data.get("only")  # "Search again" on one company
    if not lines:
        return jsonify(error="Add at least one company."), 400

    dnc_text = data.get("do_not_call", "")
    do_not_call = {n for n in map(finder.e164, clean_lines(dnc_text)) if n}
    companies = [finder.parse_company(line) for line in lines]

    with lock:
        if run["status"] == "running":
            return jsonify(error="A run is already going."), 409
        st = get_store()
        if only:
            to_search = [c for c in companies if c.name == only]
            if not to_search:
                return jsonify(error=f"{only} isn't in the Companies box any more."), 400
        else:
            to_search = [c for c in companies if not st.was_searched(c.name)]
        if to_search and not key:
            return jsonify(error="Add your Serper API key."), 400
        if key:
            api_key = key
        stop_requested.clear()
        st.remove_numbers(do_not_call)
        run = empty_run()
        run["status"] = "running"
        run["companies"] = company_entries(companies, st, to_search)
        run["rows"] = finder.to_rows(st.contacts)
        count_found(run["companies"], run["rows"])
        skipped = [e["name"] for e in run["companies"] if e["state"] == "skipped"]
        if skipped and not only:
            add_event("muted", "Already searched, skipping: " + ", ".join(skipped))
        if not to_search:
            add_event("info", "Nothing new to search. Use Search again beside a company to redo it.")

    COMPANIES_FILE.write_text(data.get("companies", ""), encoding="utf-8")
    DO_NOT_CALL_FILE.write_text(dnc_text, encoding="utf-8")

    search_fn, read_fn = make_backend(key)
    threading.Thread(
        target=worker, args=(to_search, city, do_not_call, search_fn, read_fn, st), daemon=True
    ).start()
    return jsonify(ok=True)


@app.post("/api/stop")
def stop():
    request.get_json()
    stop_requested.set()
    return jsonify(ok=True)


@app.get("/api/state")
def state():
    since = request.args.get("since", default=0, type=int)
    with lock:
        return jsonify(
            status=run["status"],
            stopping=stop_requested.is_set() and run["status"] == "running",
            events=run["events"][since:],
            event_count=len(run["events"]),
            rows=run["rows"],
            companies=run["companies"],
            pages=run["pages"],
            searches=run["searches"],
        )


@app.get("/results.csv")
def results_csv():
    with lock:
        body = "﻿" + rows_to_csv(run["rows"])
    return Response(
        body,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={OUTPUT_FILE.name}"},
    )


def main():
    global run
    parser = argparse.ArgumentParser(description="Web page for the HR phone finder")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    args = parser.parse_args()
    run = saved_run()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)  # hide the per-second polling lines
    url = f"http://127.0.0.1:{PORT}"
    print(f"HR phone finder is running at {url} (press Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1, webbrowser.open, [url]).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
