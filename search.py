"""
Web tools the research model gets to call:
  web_search(query) -> numbered results from Brave or SearXNG
  fetch_page(url)   -> readable text of a page, cut down to FETCH_MAX_CHARS
"""

import re
from html.parser import HTMLParser
from urllib.parse import urldefrag

import requests

import config

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) SupplementResearch/2.0"


class SearchError(RuntimeError):
    pass


def describe():
    if config.SEARCH_PROVIDER == "searxng":
        return "searxng @ " + config.SEARXNG_URL
    return "brave"


def check_config():
    if config.SEARCH_PROVIDER not in ("brave", "searxng"):
        raise SearchError("SEARCH_PROVIDER must be brave or searxng (got %r)"
                          % config.SEARCH_PROVIDER)
    if config.SEARCH_PROVIDER == "brave" and not config.BRAVE_API_KEY:
        raise SearchError("BRAVE_API_KEY is not set in .env")


def normalize_url(url):
    return urldefrag(url.strip())[0].rstrip("/").lower()


def web_search(query):
    """Return a list of {"title", "url", "snippet"} dicts."""
    if config.SEARCH_PROVIDER == "searxng":
        results = searxng(query)
    else:
        results = brave(query)
    return results[:config.SEARCH_RESULTS]


def brave(query):
    resp = requests.get(
        config.BRAVE_API_URL,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": config.BRAVE_API_KEY,
            "User-Agent": USER_AGENT,
        },
        params={
            "q": query,
            "count": config.SEARCH_RESULTS,
            "country": config.SEARCH_COUNTRY,
            "search_lang": config.SEARCH_LANG,
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        raise SearchError("Brave HTTP %s: %s" % (resp.status_code, resp.text[:300]))

    items = resp.json().get("web", {}).get("results", [])
    out = []
    for it in items:
        out.append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "snippet": strip_tags(it.get("description", "")),
        })
    return out


def searxng(query):
    url = config.SEARXNG_URL + "/search"
    resp = requests.get(
        url,
        params={"q": query, "format": "json", "language": config.SEARCH_LANG},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    if resp.status_code == 403:
        raise SearchError(
            "SearXNG returned 403: enable JSON output in settings.yml "
            "(search: formats: [html, json])"
        )
    if resp.status_code >= 400:
        raise SearchError("SearXNG HTTP %s: %s" % (resp.status_code, resp.text[:300]))

    items = resp.json().get("results", [])
    out = []
    for it in items:
        out.append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "snippet": it.get("content", ""),
        })
    return out


def format_results(results):
    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        lines.append("[%d] %s" % (i, r["title"]))
        lines.append("URL: " + r["url"])
        lines.append("Snippet: " + r["snippet"])
    return "\n\n".join(lines)


# --------------------------------------------------------------- fetching

# tags whose contents we never want to keep
SKIP_TAGS = ("script", "style", "noscript", "nav", "footer", "header",
             "aside", "svg", "template")

# tags that should force a line break so paragraphs don't run together
BLOCK_TAGS = ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5",
              "tr", "section", "article", "table")


class TextExtractor(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.parts = []
        self.skip_depth = 0
        self.title = ""
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip_depth:
            self.parts.append(data)

    def text(self):
        raw = "".join(self.parts)
        lines = []
        for line in raw.splitlines():
            line = " ".join(line.split())   # collapse runs of whitespace
            if line:
                lines.append(line)
        return "\n".join(lines)


def strip_tags(html):
    p = TextExtractor()
    p.feed(html or "")
    return p.text().replace("\n", " ")


# PubMed and PMC block plain scrapers, so read those through NCBI's free
# E-utilities API instead of hitting the page directly.

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.I)
PMC_RE = re.compile(r"(?:pmc\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pmc)/articles/PMC(\d+)", re.I)


def xml_section(xml, tag):
    m = re.search("<" + tag + r"[^>]*>(.*?)</" + tag + ">", xml, re.S)
    if not m:
        return ""
    plain = re.sub(r"<[^>]+>", " ", m.group(1))
    return " ".join(plain.split())


def fetch_ncbi(url):
    """Return (title, body) for a PubMed/PMC URL, or None if it's not one."""
    m = PUBMED_RE.search(url)
    if m:
        resp = requests.get(EUTILS, params={
            "db": "pubmed",
            "id": m.group(1),
            "rettype": "abstract",
            "retmode": "text",
        }, timeout=30)
        resp.raise_for_status()
        return "PubMed " + m.group(1), resp.text.strip()

    m = PMC_RE.search(url)
    if m:
        resp = requests.get(EUTILS, params={
            "db": "pmc",
            "id": m.group(1),
            "retmode": "xml",
        }, timeout=30)
        resp.raise_for_status()
        title = xml_section(resp.text, "article-title")
        abstract = xml_section(resp.text, "abstract")
        body = xml_section(resp.text, "body")
        return title, "ABSTRACT: " + abstract + "\n\nFULL TEXT: " + body

    return None


def fetch_page(url):
    if not url.lower().startswith(("http://", "https://")):
        raise SearchError("Only http(s) URLs can be fetched.")

    ncbi = fetch_ncbi(url)
    if ncbi:
        title, body = ncbi
    else:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        if resp.status_code >= 400:
            raise SearchError("HTTP %s fetching %s" % (resp.status_code, url))

        ctype = resp.headers.get("Content-Type", "").lower()
        if "pdf" in ctype:
            raise SearchError("That URL is a PDF, which can't be read. "
                              "Try the abstract/HTML page instead.")

        if "html" in ctype or not ctype:
            parser = TextExtractor()
            parser.feed(resp.text)
            title = " ".join(parser.title.split())
            body = parser.text()
        else:
            # plain text or something else - just take it as-is
            title, body = "", resp.text

    if len(body) > config.FETCH_MAX_CHARS:
        body = body[:config.FETCH_MAX_CHARS] + "\n...[truncated]"

    if title:
        return "Title: " + title + "\nURL: " + url + "\n\n" + body
    return "URL: " + url + "\n\n" + body
