#!/usr/bin/env python3
"""
Supplement research script.

For each supplement it does three passes:
  1. research  - the model searches the web and reads pages until it has
                 notes with numbered sources
  2. review    - a second pass checks the notes against the sources we
                 actually pulled and downgrades anything unsupported
  3. format    - turn the reviewed notes into the 8 spreadsheet columns

Results go to output/research/<name>.json, plus a row in Grist if
GRIST_URL is set, otherwise a row in the local Excel file.

Usage:
  python research.py "Magnesium glycinate"
  python research.py --batch supps.txt
  python research.py --batch supps.txt --focus "traumatic brain injury recovery"
  python research.py "Creatine" --force

Settings are in .env (copy .env.example to start).
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

import config
import llm
import search
import storage

DONE = "[RESEARCH_COMPLETE]"
NOT_FOUND = "Not enough reliable info found"

SEARCH_WORDS = ("search", "web_search", "brave_search")
FETCH_WORDS = ("fetch", "fetch_url", "open", "read")

DISCLAIMER = (
    "This is an AI-assisted literature summary, not medical advice. "
    "Use it to prepare questions for your doctor or pharmacist before "
    "starting any supplement."
)


# ---------------------------------------------------------------- prompts

RESEARCH_PROMPT = """You are a careful research assistant who evaluates dietary supplements and vitamins.
Your notes will help a person have an informed conversation with their doctor, so accuracy and honest
evidence strength matter more than sounding positive. Never invent studies, numbers or URLs.

TOOLS
You have two tools. To use one, reply with ONLY a single JSON object and nothing else:
  {"action": "search", "query": "<search terms>"}
  {"action": "fetch", "url": "<a URL from the search results>"}
Use one tool per reply. After each tool result, decide what to do next.

SOURCE QUALITY (best first)
  1. Systematic reviews, meta-analyses and randomized controlled trials (PubMed/PMC, Cochrane)
  2. Government/academic references: NIH Office of Dietary Supplements (ods.od.nih.gov), NCCIH,
     MedlinePlus, LiverTox, EFSA, Memorial Sloan Kettering "About Herbs"
  3. Reputable clinical/reference sites: Examine.com, Mayo Clinic, Cleveland Clinic, Drugs.com
  4. Everything else. Treat brand/vendor pages, affiliate reviews and forums as ANECDOTAL only.
Prefer fetching the full page of high-quality sources over relying on search snippets.
Useful search patterns: "<supplement> meta-analysis", "<supplement> randomized controlled trial <outcome>",
"<supplement> site:ods.od.nih.gov", "<supplement> drug interactions", "<supplement> tolerable upper intake level",
"<supplement> side effects adverse events", "<supplement> site:examine.com".

WHAT TO COVER
  - What it is and the common forms (and whether form affects absorption)
  - Each claimed benefit, with the evidence strength for it in humans
  - Doses used in the studies that showed an effect, and any upper limit
  - How long it was taken in studies; whether cycling or breaks are used
  - Side effects and adverse events, including rare serious ones
  - Drug interactions, medical conditions, pregnancy/breastfeeding, surgery, lab-test interference
  - Product quality issues (contamination, mislabeling, heavy metals) if known
  - Practical use: timing, with/without food, forms, what to combine or avoid combining with

EVIDENCE GRADES (use these words exactly)
  Strong       - several good human RCTs or a meta-analysis with consistent results
  Moderate     - some human RCTs, mostly positive, with limitations
  Limited      - few/small human studies, or mixed results
  Preliminary  - animal or cell studies only
  Not supported - human trials found no meaningful effect

FINAL ANSWER
When you have done enough research, stop using tools and write your notes with these headings:
  ## Overview
  ## Benefits            (one bullet per benefit: benefit - evidence grade - short explanation [source numbers])
  ## Dose
  ## Duration and Schedule
  ## Negative Effects
  ## Advisories          (interactions, who should avoid it, conditions, pregnancy, quality issues)
  ## Recommendations     (practical tips, plus 2-4 questions the person could ask their doctor)
  ## Sources             (numbered: [1] Title - URL; only URLs you actually saw in search results or fetched pages)
Cite sources inline with their numbers, like [1] or [2][4].
End the final answer with this exact marker on its own line: [RESEARCH_COMPLETE]
"""

RESEARCH_START = """Research this supplement: {name}
{focus}
Start by searching. You must run at least {min_searches} searches before writing the final answer."""

REVIEW_PROMPT = """You are reviewing supplement research notes before they go into a reference spreadsheet
that people will use to prepare questions for their doctor.

Supplement: {name}
{focus}
Below are the research notes, followed by the list of sources that were actually retrieved during research
(search result snippets and fetched page titles). Your job:
  - Check each claim against the sources and your knowledge. Fix factual errors and note corrections in (parentheses).
  - Make sure each benefit has an honest evidence grade (Strong / Moderate / Limited / Preliminary / Not supported).
    Downgrade claims that rest only on animal/cell data, vendor pages or anecdotes.
  - Make sure dose, duration, side effects, interactions and who should avoid it are covered. If something important
    is missing and you are confident about it from well-established knowledge, add it and label it "(general knowledge)".
  - Remove any source that is NOT in the retrieved list below. Keep the numbered [n] citations consistent.
  - Keep the same headings as the notes. Write prose/bullets, not JSON.

RESEARCH NOTES:
\"\"\"
{notes}
\"\"\"

RETRIEVED SOURCES:
{sources}
"""

FORMAT_PROMPT = """Convert these reviewed supplement notes into a JSON object for a spreadsheet.

Use EXACTLY these keys:
  "Supplement_Name": the common name of the supplement
  "Safe_Effective_Dose_Range": daily dose range with units that studies found effective, plus the upper limit if known
  "Safe_Use_Duration_Schedule": how long and how often it has been safely used (e.g. "Daily for 8-12 weeks in trials; long-term safety data limited")
  "Benefits": one line per benefit, formatted "- Benefit (Evidence grade): short explanation [n]"
  "Negative_Effects": side effects and adverse events, most common first, serious ones clearly marked, with [n] citations
  "Advisories": drug interactions, who should avoid it, conditions, pregnancy/breastfeeding, quality concerns
  "Recommendations_for_Effectiveness": practical tips (form, timing, food), then a line "Ask your doctor:" followed by 2-4 questions
  "Sources": one per line, formatted "[n] Title - URL", keeping the same numbers used in the citations

Rules:
  - Every value is a single string. Use "\\n" line breaks inside strings for lists.
  - If a field has no information, use exactly: "{not_found}"
  - Do not add keys. Return ONLY the JSON object, no commentary and no code fences.

REVIEWED NOTES:
\"\"\"
{notes}
\"\"\"
"""


# ---------------------------------------------------------------- helpers

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_logfile(name):
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.LOG_DIR / (storage.sanitize_filename(name) + "_" + stamp + ".log")
    return path


def write_log(path, header, body):
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + "=" * 80 + "\n")
        f.write(header + " - " + now() + "\n")
        f.write("=" * 80 + "\n")
        f.write(body + "\n")


def json_objects(text):
    """Pull every JSON value that starts with { out of some text.

    The model likes to wrap things in prose or code fences, so we just
    scan for braces and try to decode from each one.
    """
    decoder = json.JSONDecoder()
    out = []
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
            out.append(obj)
            i = text.find("{", end)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
    return out


def get_action(text):
    for obj in json_objects(text):
        if isinstance(obj, dict) and isinstance(obj.get("action"), str):
            return obj
    return None


def get_fields(text):
    # the model sometimes returns extra junk, so pick the object that
    # looks most like the spreadsheet row we asked for
    cands = [o for o in json_objects(text) if isinstance(o, dict)]
    cands.sort(key=lambda o: sum(k in o for k in storage.COLUMNS), reverse=True)
    if cands and any(k in cands[0] for k in storage.COLUMNS):
        return cands[0]
    return None


def focus_text(focus):
    if not focus:
        return ""
    return ("Special focus: pay extra attention to evidence relevant to " + focus +
            ", while still covering general safety and dosing.\n")


# ---------------------------------------------------------------- sources

class SourceList:
    """Keeps track of everything the model actually saw, so we can
    double check citations at the end."""

    def __init__(self):
        self.items = {}

    def add(self, url, title="", snippet="", fetched=False):
        if not url:
            return
        key = search.normalize_url(url)
        if key not in self.items:
            self.items[key] = {"url": url, "title": title, "snippet": snippet, "fetched": False}
        entry = self.items[key]
        if not entry["title"]:
            entry["title"] = title
        if fetched:
            entry["fetched"] = True

    def has(self, url):
        return search.normalize_url(url) in self.items

    def as_text(self):
        if not self.items:
            return "(none)"
        lines = []
        for e in self.items.values():
            tag = " (full page read)" if e["fetched"] else ""
            lines.append("- " + (e["title"] or "(untitled)") + tag)
            lines.append("  " + e["url"])
            lines.append("  " + e["snippet"][:300])
        return "\n".join(lines)

    def to_list(self):
        return list(self.items.values())


# ---------------------------------------------------------------- research

def do_research(name, focus, logfile, sources):
    messages = [
        {"role": "system", "content": RESEARCH_PROMPT},
        {"role": "user", "content": RESEARCH_START.format(
            name=name, focus=focus_text(focus), min_searches=config.MIN_SEARCHES)},
    ]
    queries = []
    fetched = []
    best = ""
    tools_open = True

    for step in range(1, config.MAX_RESEARCH_STEPS + 1):
        # give the model one last nudge to actually write the answer
        if step >= config.MAX_RESEARCH_STEPS - 1 and tools_open:
            tools_open = False
            messages.append({"role": "user", "content":
                "You are out of research steps. Do not use any more tools. Write the complete "
                "final answer now with all the headings, and end with " + DONE})

        print("   step %2d/%d " % (step, config.MAX_RESEARCH_STEPS), end="", flush=True)
        reply = llm.chat(messages)
        write_log(logfile, "MODEL step %d" % step, reply)

        if not reply:
            print("(empty reply, retrying)")
            messages.append({"role": "user", "content": "Your reply was empty. Continue the research."})
            continue

        action = get_action(reply)

        if action and DONE not in reply:
            messages.append({"role": "assistant", "content": reply})
            if not tools_open:
                print("(tool request after tools closed)")
                messages.append({"role": "user", "content":
                    "Tools are closed. Write the final answer now and end with " + DONE})
                continue
            messages.append({"role": "user", "content":
                use_tool(action, queries, fetched, sources, logfile)})
            continue

        messages.append({"role": "assistant", "content": reply})
        if len(reply) > len(best):
            best = reply

        if DONE in reply:
            if len(queries) < config.MIN_SEARCHES and tools_open:
                print("(finished early after %d searches, asking for more)" % len(queries))
                messages.append({"role": "user", "content":
                    "You have only run %d searches; at least %d are required. "
                    "Search for what is still weak or missing (e.g. drug interactions, upper limits, "
                    "meta-analyses for the main benefits), then rewrite the full final answer."
                    % (len(queries), config.MIN_SEARCHES)})
                continue
            print("done")
            return {"notes": reply.replace(DONE, "").strip(),
                    "queries": queries, "fetched": fetched, "complete": True}

        print("(partial answer, asking to continue)")
        messages.append({"role": "user", "content":
            "Continue. Either use a tool (JSON only), or write the complete final answer with all "
            "headings and end with " + DONE})

    print("   ! step limit reached; using the longest draft")
    write_log(logfile, "WARNING", "Step limit reached without completion marker.")
    return {"notes": best.replace(DONE, "").strip(),
            "queries": queries, "fetched": fetched, "complete": False}


def use_tool(action, queries, fetched, sources, logfile):
    name = action.get("action", "").lower()

    if name in SEARCH_WORDS:
        query = str(action.get("query", "")).strip()
        if not query:
            print("(empty search)")
            return 'The search had no "query". Send {"action": "search", "query": "..."}'
        if query.lower() in [q.lower() for q in queries]:
            print("(repeat search: %s)" % query)
            return 'You already searched for "%s". Use a different query or a different tool.' % query
        print("search: %s" % query)
        queries.append(query)
        try:
            results = search.web_search(query)
        except Exception as e:
            # network hiccup - tell the model and let it move on
            write_log(logfile, "SEARCH ERROR", query + "\n" + str(e))
            return "Search failed (%s). Try another query, or continue with what you have." % e
        for r in results:
            sources.add(r["url"], r["title"], r["snippet"])
        text = search.format_results(results)
        write_log(logfile, "SEARCH " + query, text)
        return ("Search results for: " + query + "\n\n" + text + "\n\n"
                "Fetch the most useful high-quality pages, search again, or write the final answer.")

    if name in FETCH_WORDS:
        url = str(action.get("url", "")).strip()
        if not url:
            print("(empty fetch)")
            return 'The fetch had no "url". Send {"action": "fetch", "url": "..."}'
        print("fetch:  %s" % url)
        try:
            page = search.fetch_page(url)
        except Exception as e:
            write_log(logfile, "FETCH ERROR", url + "\n" + str(e))
            return "Could not read that page (%s). Try a different source." % e
        title = ""
        if page.startswith("Title: "):
            title = page.split("\n", 1)[0][len("Title: "):]
        sources.add(url, title, fetched=True)
        fetched.append(url)
        write_log(logfile, "FETCH " + url, page)
        return "Page content:\n\n" + page + "\n\nContinue researching or write the final answer."

    print("(unknown action %r)" % name)
    return 'Unknown action. Use {"action": "search", "query": "..."} or {"action": "fetch", "url": "..."}'


# ---------------------------------------------------------------- review + format

def do_review(name, focus, notes, sources, logfile):
    prompt = REVIEW_PROMPT.format(
        name=name, focus=focus_text(focus),
        notes=notes, sources=sources.as_text())
    reviewed = llm.chat([{"role": "user", "content": prompt}])
    write_log(logfile, "REVIEWED NOTES", reviewed)
    return reviewed or notes


def do_format(notes, logfile):
    messages = [{"role": "user", "content":
        FORMAT_PROMPT.format(notes=notes, not_found=NOT_FOUND)}]
    for attempt in (1, 2):
        raw = llm.chat(messages, temperature=0.0)
        write_log(logfile, "FORMAT attempt %d" % attempt, raw)
        parsed = get_fields(raw)
        if parsed is not None:
            return parsed
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content":
            "That was not a valid JSON object. Return ONLY the JSON object with the exact keys."})
    raise RuntimeError("Model did not return valid JSON for the spreadsheet fields.")


def tidy_fields(parsed, name):
    # be forgiving about key casing / spacing
    lower = {k.lower().replace(" ", "_"): v for k, v in parsed.items()}
    fields = {}
    for col in storage.COLUMNS:
        val = parsed.get(col, lower.get(col.lower()))
        if isinstance(val, list):
            val = "\n".join(str(v) for v in val)
        elif isinstance(val, dict):
            val = "\n".join("%s: %s" % (k, v) for k, v in val.items())
        val = str(val).strip() if val is not None else ""
        fields[col] = val or NOT_FOUND
    # keep the name the user asked for so reruns hit the same row
    fields["Supplement_Name"] = name
    return fields


URL_RE = re.compile(r"https?://[^\s)\]>\"']+")


def check_sources(text, sources):
    """Flag any cited URL we never actually saw during research."""
    lines = []
    flagged = 0
    for line in text.splitlines():
        urls = URL_RE.findall(line)
        if urls and not any(sources.has(u.rstrip(".,;")) for u in urls):
            line += "  (NOT VERIFIED - not seen during research)"
            flagged += 1
        lines.append(line)
    return "\n".join(lines), flagged


# ---------------------------------------------------------------- pipeline

def process(name, focus):
    logfile = make_logfile(name)
    sources = SourceList()
    started = time.time()
    write_log(logfile, "START",
              "Supplement: %s\nFocus: %s\nLLM: %s\nSearch: %s"
              % (name, focus or "-", llm.describe(), search.describe()))

    print(" 1/3 researching")
    research = do_research(name, focus, logfile, sources)
    if not research["notes"]:
        raise RuntimeError("The model produced no research notes.")

    print(" 2/3 reviewing")
    reviewed = do_review(name, focus, research["notes"], sources, logfile)

    print(" 3/3 formatting")
    fields = tidy_fields(do_format(reviewed, logfile), name)
    fields["Sources"], unverified = check_sources(fields["Sources"], sources)

    record = {
        "supplement": name,
        "focus": focus,
        "status": "complete",
        "timestamp": now(),
        "duration_seconds": round(time.time() - started),
        "llm": llm.describe(),
        "search": search.describe(),
        "research_finished_cleanly": research["complete"],
        "queries": research["queries"],
        "fetched_pages": research["fetched"],
        "unverified_source_count": unverified,
        "fields": fields,
        "research_notes": research["notes"],
        "reviewed_notes": reviewed,
        "retrieved_sources": sources.to_list(),
        "disclaimer": DISCLAIMER,
    }

    # try Grist first if it's set up, otherwise (or on failure) Excel
    record["saved_to"] = "excel"
    if config.GRIST_ENABLED:
        try:
            storage.upsert_grist(fields)
            record["saved_to"] = "grist"
        except Exception as e:
            print(" ! Grist upload failed, saving to Excel instead: %s" % e)
            write_log(logfile, "GRIST ERROR", str(e))
    if record["saved_to"] == "excel":
        storage.upsert_excel(fields)

    # saved last so it marks the supplement as done
    storage.save_record(name, record)

    msg = " saved (%d searches, %d pages read" % (len(research["queries"]), len(research["fetched"]))
    if unverified:
        msg += ", %d unverified source(s) flagged" % unverified
    print(msg + ")")
    write_log(logfile, "DONE", json.dumps(fields, indent=2, ensure_ascii=False))
    return fields


def read_list(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def main():
    parser = argparse.ArgumentParser(description="Research supplements with an LLM + web search.")
    parser.add_argument("supplement", nargs="*", help="Supplement name(s)")
    parser.add_argument("--batch", help="Text file with one supplement per line")
    parser.add_argument("--focus", default="", help='Optional focus, e.g. "sleep" or "traumatic brain injury"')
    parser.add_argument("--force", action="store_true", help="Redo supplements that are already done")
    args = parser.parse_args()

    try:
        llm.check_config()
        search.check_config()
    except (llm.LLMError, search.SearchError) as e:
        sys.exit("Config error: %s\nEdit .env (see .env.example)." % e)

    names = list(args.supplement)
    if args.batch:
        names += read_list(args.batch)

    if not names:
        name = input("Supplement to research: ").strip()
        if not name:
            sys.exit("Nothing to do.")
        names = [name]

    # dedupe, keep order
    unique = list(dict.fromkeys(names))

    todo = []
    for n in unique:
        if args.force or not storage.is_done(n):
            todo.append(n)
    skipped = len(unique) - len(todo)

    print("LLM:    %s" % llm.describe())
    print("Search: %s" % search.describe())
    print("Output: %s" % ("Grist (Excel if it fails)" if config.GRIST_ENABLED else config.EXCEL_FILE))
    if skipped:
        print("Skipping %d already-researched supplement(s) (use --force to redo)." % skipped)
    if not todo:
        return

    failed = []
    for i, name in enumerate(todo, 1):
        print("\n[%d/%d] %s" % (i, len(todo), name))
        try:
            process(name, args.focus.strip())
        except KeyboardInterrupt:
            print("\nStopped. Finished supplements are saved; rerun to continue.")
            sys.exit(130)
        except Exception as e:
            print(" ! failed: %s" % e)
            failed.append(name)

    print("\nDone. Output: %s (Excel fallback: %s)"
          % ("Grist" if config.GRIST_ENABLED else config.EXCEL_FILE, config.EXCEL_FILE))
    if failed:
        print("Failed (%d): %s; rerun to retry them. Logs: %s"
              % (len(failed), ", ".join(failed), config.LOG_DIR))
    print(DISCLAIMER)


if __name__ == "__main__":
    main()
