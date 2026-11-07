# HR Contact Finder

A small local tool that finds publicly listed phone numbers for HR and talent acquisition people, so a student team can invite them to mock placements. It was built for the student-run mock placements at SVCE.

Give it a list of companies, and it searches the web, reads public pages and PDFs, and builds a contact list you can filter, copy from or export.

> **Status:** tested with automated tests and fake data. It hasn't yet been run against live search results, so expect to tune the search phrases and filters after a first real run.

## How it works

1. Runs five searches for each company through the [Serper](https://serper.dev) search API.
2. Reads the top results, starting with recent pages. It handles HTML pages and text-based PDFs.
3. Keeps an Indian phone number only if the text around it mentions an HR role, and either that text or the page title names the company.
4. Picks up the HR person's name when it's written right beside the number. Otherwise the name stays blank.
5. Flags anything worth checking before you call, and saves everything for the next run.

## Getting started

You need Python 3 and a Serper API key. Create the key at [serper.dev](https://serper.dev), and check your account for the current free allowance.

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r requirements.txt
```

```bash
.venv/Scripts/python webapp.py
```

On macOS or Linux, use `.venv/bin/python` instead. The page opens at `http://127.0.0.1:5057`, and only your own computer can reach it.

Choose **New search**, list your companies one per line, paste your key, then choose **Start search**. If a company goes by other names, add them with `|`:

```text
TCS | Tata Consultancy Services
```

### Running without the page

```bash
export SERPER_API_KEY="your_key_here"
```

```bash
.venv/Scripts/python hr_phone_finder.py companies.txt --city Chennai --out hr_contacts.csv
```

Optional flags are `--do-not-call do_not_call.txt`, `--search-again` and `--store finder_data.json`.

## Output

Results are saved to `hr_contacts.csv`, which opens in Excel or Google Sheets.

| Column | Meaning |
|---|---|
| `company` | The company you searched for |
| `hr_name` | The name beside the number, blank if none was clear |
| `phone` | Indian format with a leading 0, such as `098765 43210` |
| `line_type` | `mobile`, `landline` or `unknown` |
| `page_date` | The newest date found for a page listing this number, if any |
| `sources_found` | How many pages list this number |
| `check_first` | Reasons to double-check it, such as a possible agency, conflicting names or an old page |
| `source_url` | The first page where it was found |

## Things to know

- **Search credits:** each new company uses 5 searches. Companies already searched are skipped, and **Search again** redoes a single company.
- **Results are kept between runs:** they're stored in `finder_data.json`, and new numbers are added to the list. Close the finder and delete that file to start over.
- **Stopped or failed searches:** that company isn't marked as done, so it's searched again next time and uses credits again. Numbers already found are kept.
- **Not every company will have a number:** it only finds numbers people or companies have published. Scanned PDFs can't be read.
- **Names and numbers can be wrong or out of date:** open `source_url` before calling, and start by checking you've reached the right person.

## Responsible use

These are real people's contact details, so please treat them with care:

- **Sources:** the finder follows each site's robots.txt. It doesn't read LinkedIn, other social media, major job sites, or contact-data resellers and caller-ID lookups.
- **Outreach:** say who you are and where you found the number, and stop contacting anyone who says no.
- **Opt-outs:** add their number to **Numbers to leave out**. It's removed from your saved results the next time you start a search.
- **Keep only what you need:** India's DPDP Act covers personal data like this, so delete the data of people who didn't engage once the season ends.
- **Never commit contact data:** `.gitignore` already excludes `finder_data.json`, `hr_contacts.csv` and `do_not_call.txt`. Your API key is never written to disk by the page.

## Customizing

- **Search phrases:** edit `SEARCH_PHRASES` in `hr_phone_finder.py`. Each phrase costs one credit per company.
- **Sites to skip:** edit `BLOCKED_DOMAINS` in the same file.
- **Companies:** the `companies.txt` in this repo holds examples only. Replace them with your own list.

## Project layout

| File | Purpose |
|---|---|
| `hr_phone_finder.py` | Search, page reading, number and name matching, saving, and the command-line version |
| `webapp.py` | The local web server |
| `ui.html` | The web page |
| `companies.txt` | Your list of companies |
| `test_hr_phone_finder.py`, `test_webapp.py` | Automated tests |

## Tests

Tests need `pytest`, which isn't in `requirements.txt`. They use fake data, so they don't need a key or use any credits.

```bash
.venv/Scripts/python -m pip install pytest
```

```bash
.venv/Scripts/python -m pytest
```
