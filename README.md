# research-script

Research dietary supplements and vitamins with an AI model plus live web
search, and get an evidence-graded summary in a spreadsheet.

For each supplement the script searches the web and reads the pages it
finds (including PubMed and PMC, via NCBI's free API). Then it fact-checks
its own notes against the sources it actually pulled, and writes one row
with doses, benefits (each with an evidence grade), side effects, drug
interactions and sources. Any citation it never actually saw gets marked
`NOT VERIFIED`.

> [!WARNING]
> **This is not medical advice.** It's an AI-assisted literature summary
> meant to help you prepare questions for your doctor or pharmacist. AI
> models make mistakes, so check the sources yourself before acting on
> anything.

## What it does

- **Local or hosted models.** LM Studio, Ollama, OpenAI (or anything
  OpenAI-compatible), and Claude.
- **Brave or SearXNG for search.** Self-hosted SearXNG needs no API key.
- **Evidence grades** on every benefit: Strong / Moderate / Limited /
  Preliminary / Not supported.
- **A review pass.** A second prompt checks the notes against the sources
  we actually retrieved and downgrades claims that aren't supported.
- **Invented-citation check.** If the model cites a URL we never fetched
  or saw in search results, that line gets flagged.
- **Excel or Grist output.** Re-running a supplement updates the existing
  row instead of piling up duplicates.
- **Resumable batches.** Finished supplements are skipped, so you can hit
  Ctrl+C and pick up where you left off.

## What you need

- Python 3.10 or newer
- An AI model: a local one through LM Studio or Ollama, or an OpenAI or
  Anthropic API key
- A search backend: a [Brave Search API](https://brave.com/search/api/)
  key, or a SearXNG instance with JSON output turned on

## Getting started

```bash
git clone https://github.com/isaiahboland-dotcom/ai-supplement-script.git
cd research-script

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env    # then fill in your model and search settings
