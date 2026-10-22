import os
import re
import json
import gzip
import hashlib
import mimetypes
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag, unquote

import requests
from bs4 import BeautifulSoup

START_URL = "https://storage.googleapis.com/yubico/study.html"
OUTPUT_DIR = "mirror"
MAX_URLS = 3000
MAX_BYTES_PER_FILE = 25 * 1024 * 1024
TIMEOUT = 25

# Crawl pages only within the starting site's directory.
# Download referenced assets from other hosts as dependencies,
# but never recursively crawl unrelated external websites.
START = urlparse(START_URL)
START_PREFIX = START.path.rsplit("/", 1)[0] + "/"
START_HOST = START.hostname

REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
REPO_NAME = REPOSITORY.split("/")[-1] if REPOSITORY else ""

if REPO_NAME.endswith(".github.io"):
    BASE_PATH = ""
else:
    BASE_PATH = f"/{REPO_NAME}" if REPO_NAME else ""

session = requests.Session()
session.headers["User-Agent"] = "WebsiteMirror/1.0"

pages_queue = deque([START_URL])
asset_queue = deque()
sitemap_queue = deque()
seen_pages = set()
seen_assets = set()
seen_sitemaps = set()

url_to_path = {}
content_by_url = {}
type_by_url = {}
failed_urls = []
external_domains = set()
discovered_urls = set()

# Common paths probed even if never linked anywhere.
COMMON_PATHS = [
    "robots.txt", "sitemap.xml", "sitemap_index.xml",
    "sitemap-index.xml", "wp-sitemap.xml", "feed.xml",
    "atom.xml", "rss.xml", "ads.txt", "app-ads.txt",
    "favicon.ico", "favicon.svg", "favicon-32x32.png",
    "favicon-16x16.png", "apple-touch-icon.png",
    "apple-touch-icon-precomposed.png", "manifest.json",
    "site.webmanifest", "humans.txt", "security.txt",
    ".well-known/security.txt", "crossdomain.xml",
    "browserconfig.xml", "opensearch.xml", "404.html",
    "offline.html", "sw.js", "service-worker.js",
    "index.html", "index.htm", "about.html", "contact.html",
    "privacy.html", "terms.html", "home.html", "main.html",
    "menu.html", "blog.html", "posts.html", "news.html",
    "store.html", "shop.html", "products.html",
    "support.html", "help.html", "faq.html", "login.html",
    "signup.html", "search.html", "archive.html", "tags.html",
    "app.js", "main.js", "bundle.js", "styles.css",
    "style.css", "main.css", "screen.css",
]

COMMON_DIRS = [
    "assets", "static", "images", "img", "js", "css",
    "media", "files", "downloads", "docs", "blog", "posts",
    "news", "shop", "store", "products", "about", "contact",
    "help", "support", "faq", "search", "api", "data",
    "fonts", "icons", "pdf", "video", "audio",
]

COMMON_EXTS = [
    ".html", ".htm", ".xml", ".json", ".txt", ".pdf", ".zip",
    ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".webm", ".mp3", ".csv", ".md",
]


def canonical(url):
    """Normalize a URL and discard its fragment."""
    url, _ = urldefrag(url)
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return None

    return parsed.geturl()


def is_internal_page(url):
    """Allow page crawling only within the original site directory."""
    parsed = urlparse(url)
    path = unquote(parsed.path)

    return (
        parsed.hostname == START_HOST
        and path.startswith(START_PREFIX)
    )


def safe_local_path(url, content_type=""):
    """Store each host separately to avoid filename collisions."""
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    path = unquote(parsed.path).lstrip("/")

    if not path or path.endswith("/"):
        path += "index.html"

    parts = path.split("/")
    if any(part in ("..", ".") for part in parts):
        raise ValueError("Unsafe URL path")

    # Different query strings can represent different resources.
    if parsed.query:
        root, ext = os.path.splitext(path)
        suffix = hashlib.sha256(
            parsed.query.encode()
        ).hexdigest()[:10]
        path = f"{root}_{suffix}{ext}"

    if not os.path.splitext(path)[1]:
        ext = mimetypes.guess_extension(content_type or "")
        if ext:
            path += ext

    return f"_sites/{host}/{path}"


def site_url(path):
    """Build a URL that works on GitHub Pages project sites."""
    return f"{BASE_PATH}/{path}" if BASE_PATH else f"/{path}"


def save(path, data):
    full = os.path.join(OUTPUT_DIR, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)

    with open(full, "wb") as f:
        f.write(data)


def fetch(url):
    try:
        response = session.get(
            url,
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()

        final_url = canonical(response.url)
        if not final_url:
            response.close()
            return None

        content_type = response.headers.get(
            "Content-Type", ""
        ).split(";")[0].strip().lower()

        chunks = []
        total = 0

        for chunk in response.iter_content(65536):
            if not chunk:
                continue

            total += len(chunk)
            if total > MAX_BYTES_PER_FILE:
                response.close()
                print("Too large, skipped:", url)
                return None

            chunks.append(chunk)

        response.close()
        return final_url, b"".join(chunks), content_type

    except requests.RequestException as exc:
        status = getattr(
            getattr(exc, "response", None), "status_code", None
        )
        entry = {"url": url, "error": str(exc)}
        if status is not None:
            entry["status"] = status
            entry["probed_404"] = status == 404
        failed_urls.append(entry)
        print("Failed:", url, exc)
        return None


def is_html(content_type, path):
    return (
        "html" in content_type
        or path.lower().endswith((".html", ".htm"))
    )


def is_css(content_type, path):
    return (
        "css" in content_type
        or path.lower().endswith(".css")
    )


def is_js(content_type, path):
    return (
        "javascript" in content_type
        or "ecmascript" in content_type
        or path.lower().endswith((".js", ".mjs", ".cjs"))
    )


def add_page(url):
    url = canonical(url)
    if url and is_internal_page(url):
        discovered_urls.add(url)
        if url not in seen_pages:
            pages_queue.append(url)


def add_asset(url):
    url = canonical(url)
    if not url:
        return

    parsed = urlparse(url)
    if parsed.hostname != START_HOST:
        external_domains.add(parsed.hostname or "")

    discovered_urls.add(url)

    if url not in seen_assets and url not in seen_pages:
        asset_queue.append(url)


def extract_srcset(value, base_url):
    for item in value.split(","):
        candidate = item.strip().split()
        if candidate:
            yield urljoin(base_url, candidate[0])


def html_references(soup, page_url):
    """Discover navigable pages and directly referenced resources."""

    for tag in soup.find_all("a", href=True):
        url = canonical(urljoin(page_url, tag["href"]))
        if url and is_internal_page(url):
            add_page(url)

    for tag_name in ("iframe", "frame"):
        for tag in soup.find_all(tag_name, src=True):
            url = canonical(urljoin(page_url, tag["src"]))
            if url and is_internal_page(url):
                add_page(url)
            elif url:
                add_asset(url)

    attributes = [
        ("script", "src"), ("img", "src"), ("source", "src"),
        ("video", "src"), ("audio", "src"), ("input", "src"),
        ("embed", "src"), ("object", "data"), ("track", "src"),
        ("use", "href"), ("image", "href"), ("feImage", "href"),
        ("area", "href"), ("a", "download"),
    ]

    for tag_name, attr in attributes:
        for tag in soup.find_all(tag_name):
            value = tag.get(attr)
            if value:
                add_asset(urljoin(page_url, value))

    for tag in soup.find_all(srcset=True):
        for url in extract_srcset(tag["srcset"], page_url):
            add_asset(url)

    for tag in soup.find_all(poster=True):
        add_asset(urljoin(page_url, tag["poster"]))

    # Lazy-loading and data attributes on any tag.
    for tag in soup.find_all(True):
        for attr in (
            "data-src", "data-srcset", "data-background",
            "data-href", "data-url", "data-image", "data-lazy",
            "background",
        ):
            value = tag.get(attr)
            if value:
                add_asset(urljoin(page_url, value))

    for tag in soup.find_all("link", href=True):
        rel = [str(x).lower() for x in tag.get("rel", [])]

        if any(x in rel for x in (
            "stylesheet", "icon", "shortcut", "preload",
            "modulepreload", "prefetch", "apple-touch-icon",
            "manifest", "image_src"
        )):
            add_asset(urljoin(page_url, tag["href"]))

    # Inline CSS.
    for tag in soup.find_all(style=True):
        for url in extract_css_references(tag["style"], page_url):
            add_asset(url)

    for tag in soup.find_all("style"):
        if tag.string:
            for url in extract_css_references(tag.string, page_url):
                add_asset(url)

    # Inline scripts: discover statically identifiable references.
    for tag in soup.find_all("script"):
        if tag.get("src"):
            continue

        text = tag.string or tag.get_text()
        for url in extract_js_references(text, page_url):
            add_asset(url)


def parse_sitemap(xml_text):
    """Extract URLs from a sitemap or sitemap index file."""
    soup = BeautifulSoup(xml_text, "html.parser")

    for loc in soup.find_all("loc"):
        if not loc.string:
            continue
        url = loc.string.strip()
        if url.endswith(".xml") and "sitemap" in url.lower():
            # Sitemap index: queue the child sitemap.
            sitemap_queue.append(url)
        else:
            add_page(url)
            add_asset(url)


def discover_sitemaps():
    """Fetch robots.txt and known sitemap locations, parse them."""
    base = START._replace(path="/", query="").geturl()

    robots_url = urljoin(base, "robots.txt")
    result = fetch(robots_url)
    if result:
        _, data, _ = result
        text = data.decode("utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                candidate = line.split(":", 1)[1].strip()
                sitemap_queue.append(urljoin(base, candidate))

    sitemap_queue.append(urljoin(base, "sitemap.xml"))
    for extra in (
        "sitemap_index.xml", "sitemap-index.xml",
        "wp-sitemap.xml", "sitemap/sitemap.xml", "sitemap.xml.gz",
    ):
        sitemap_queue.append(urljoin(base, extra))

    while sitemap_queue:
        sm_url = sitemap_queue.popleft()
        if sm_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sm_url)

        result = fetch(sm_url)
        if not result:
            continue
        _, data, ctype = result

        if "gzip" in ctype or sm_url.endswith(".gz"):
            try:
                data = gzip.decompress(data)
            except OSError:
                continue

        if "xml" in ctype or sm_url.rstrip("/").endswith((".xml", ".gz")):
            parse_sitemap(data.decode("utf-8", errors="replace"))


def probe_common_paths():
    """HEAD-check likely locations; queue only ones that exist.
    Probes do not count against MAX_URLS."""
    base = START._replace(path="/", query="").geturl()
    directory = START_PREFIX

    candidates = set()

    # Probe only inside the starting directory for bucket/CDN hosts,
    # plus the bucket root for well-known files that must live there.
    for name in COMMON_PATHS:
        candidates.add(urljoin(base, directory + name.lstrip("/")))

    for d in COMMON_DIRS:
        dir_url = urljoin(base, directory + d + "/")
        for ext in COMMON_EXTS:
            candidates.add(urljoin(dir_url, "index" + ext))
        for name in (
            "main", "app", "bundle", "chunk", "vendor",
            "index", "all", "styles", "script", "config",
        ):
            for ext in (".js", ".css", ".json", ".min.js", ".min.css"):
                candidates.add(urljoin(dir_url, name + ext))

    # Filter out anything already seen/discovered.
    candidates = {
        c for c in candidates
        if c not in seen_pages and c not in seen_assets
    }

    print(f"Probing {len(candidates)} candidate paths with HEAD...")
    found = 0

    for url in sorted(candidates):
        try:
            response = session.head(
                url, timeout=TIMEOUT, allow_redirects=True
            )

            # GCS sometimes rejects HEAD; fall back to ranged GET.
            if response.status_code == 403:
                response = session.get(
                    url, timeout=TIMEOUT,
                    headers={"Range": "bytes=0-0"}, stream=True,
                )
                response.close()

            if response.status_code == 200:
                found += 1
                add_asset(url)

        except requests.RequestException:
            pass

    print(f"Probe found {found} existing paths.")


def extract_css_references(css, base_url):
    pattern = re.compile(
        r"""url\(\s*(['"]?)(.*?)\1\s*\)"""
        r"""|@import\s+(['"])(.*?)\3""",
        re.IGNORECASE,
    )

    for match in pattern.finditer(css):
        value = (
            match.group(2)
            if match.group(2) is not None
            else match.group(4)
        )
        value = value.strip()

        if not value or value.startswith(
            ("data:", "blob:", "#", "javascript:")
        ):
            continue

        url = canonical(urljoin(base_url, value))
        if url:
            yield url


def extract_js_references(js, base_url):
    """Find common static JS module and source-map references."""
    patterns = [
        r"""(?:import|export)\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""",
        r"""import\(\s*['"]([^'"]+)['"]\s*\)""",
        r"""new\s+URL\(\s*['"]([^'"]+)['"]\s*,\s*import\.meta\.url\s*\)""",
        r"""sourceMappingURL=([^\s*]+)""",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, js):
            value = match.group(1).strip()

            if not value or value.startswith(
                ("data:", "blob:", "#")
            ):
                continue

            if not value.startswith((".", "/", "http://", "https://")):
                continue

            url = canonical(urljoin(base_url, value))
            if url:
                yield url

    # Absolute URLs in JS pointing at likely static files.
    for match in re.finditer(
        r"""['"](https?://[^'"\s<>{}\\]+)['"]""", js
    ):
        value = match.group(1).strip()
        if value.lower().endswith((
            ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
            ".ico", ".css", ".js", ".mjs", ".json", ".xml",
            ".pdf", ".zip", ".mp4", ".webm", ".mp3", ".woff",
            ".woff2", ".ttf", ".eot", ".csv", ".txt",
            ".webmanifest",
        )):
            yield value

    # Root-relative paths with a file extension (SPA fetch targets).
    for match in re.finditer(
        r"""['"](/[a-zA-Z0-9_\-./]+\.[a-zA-Z0-9]{1,6})['"]""", js
    ):
        value = match.group(1)
        if not value.startswith("//"):
            yield urljoin(base_url, value)


def rewrite_reference(original, base_url):
    absolute = canonical(urljoin(base_url, original))
    path = url_to_path.get(absolute)

    if path:
        return site_url(path)

    return original


def rewrite_css(css, base_url):
    pattern = re.compile(
        r"""url\(\s*(['"]?)(.*?)\1\s*\)"""
        r"""|@import\s+(['"])(.*?)\3""",
        re.IGNORECASE,
    )

    def replace(match):
        value = (
            match.group(2)
            if match.group(2) is not None
            else match.group(4)
        )

        replacement = rewrite_reference(value.strip(), base_url)

        if match.group(2) is not None:
            return f'url("{replacement}")'

        return f'@import "{replacement}"'

    return pattern.sub(replace, css)


def add_mirror_notice(soup, original_url):
    banner = f"""
<div style="all: initial; position: fixed; bottom: 0; left: 0; right: 0;
            z-index: 2147483647; background: #fff3cd; color: #333;
            font-family: system-ui, sans-serif; font-size: 14px;
            padding: 12px 16px; border-top: 2px solid #d4a017;
            text-align: center;">
  <strong>Unofficial mirror / archive.</strong>
  This is NOT an official page. Original source:
  <a href="{original_url}">{original_url}</a>
</div>
"""
    soup.body.insert(0, BeautifulSoup(banner, "html.parser"))


def rewrite_html(data, page_url):
    soup = BeautifulSoup(data, "html.parser")

    # A base href can cause local paths to resolve against the origin.
    for tag in soup.find_all("base"):
        tag.decompose()

    # Keep navigation pointing to the original site.
    for tag in soup.find_all("a", href=True):
        abs_url = canonical(urljoin(page_url, tag["href"]))
        if abs_url and is_internal_page(abs_url):
            tag["href"] = abs_url
        else:
            tag["href"] = rewrite_reference(tag["href"], page_url)

    attrs = [
        ("script", "src"), ("img", "src"), ("source", "src"),
        ("video", "src"), ("audio", "src"), ("input", "src"),
        ("embed", "src"), ("object", "data"), ("iframe", "src"),
    ]

    for tag_name, attr in attrs:
        for tag in soup.find_all(tag_name):
            if tag.get(attr):
                tag[attr] = rewrite_reference(tag[attr], page_url)

    for tag in soup.find_all("link", href=True):
        tag["href"] = rewrite_reference(tag["href"], page_url)

    for tag in soup.find_all(srcset=True):
        candidates = []
        for item in tag["srcset"].split(","):
            bits = item.strip().split()
            if bits:
                bits[0] = rewrite_reference(bits[0], page_url)
                candidates.append(" ".join(bits))
        tag["srcset"] = ", ".join(candidates)

    for tag in soup.find_all(poster=True):
        tag["poster"] = rewrite_reference(tag["poster"], page_url)

    for tag in soup.find_all(style=True):
        tag["style"] = rewrite_css(tag["style"], page_url)

    for tag in soup.find_all("style"):
        if tag.string:
            tag.string.replace_with(
                rewrite_css(tag.string, page_url)
            )

    # Don't present a third party's identity as our own.
    title = soup.find("title")
    if title and title.string:
        title.string.replace_with(
            f"[Mirror] {title.string} — unofficial copy"
        )

    for tag in soup.find_all(["meta", "link"]):
        name = (tag.get("name") or tag.get("property") or "").lower()
        if name in ("description", "og:title", "og:site_name",
                    "og:image", "twitter:title"):
            tag.decompose()
        rels = [str(x).lower() for x in tag.get("rel", [])]
        if "canonical" in rels:
            tag.decompose()

    if soup.body:
        add_mirror_notice(soup, START_URL)

    return soup.encode("utf-8")


def rewrite_js(js, base_url):
    patterns = [
        r"""((?:import|export)\s+(?:[^'"]*?\s+from\s+)?['"])([^'"]+)(['"])""",
        r"""(import\(\s*['"])([^'"]+)(['"]\s*\))""",
        r"""(new\s+URL\(\s*['"])([^'"]+)(['"]\s*,\s*import\.meta\.url\s*\))""",
    ]

    for pattern in patterns:
        def replace(match):
            value = match.group(2)

            if not value.startswith(
                (".", "/", "http://", "https://")
            ):
                return match.group(0)

            new_value = rewrite_reference(value, base_url)
            return match.group(1) + new_value + match.group(3)

        js = re.sub(pattern, replace, js)

    return js


os.makedirs(OUTPUT_DIR, exist_ok=True)

# Phase 0: sitemap and common-path discovery before the main crawl.
discover_sitemaps()
probe_common_paths()

# Phase 1: crawl internal HTML pages and recursively discover assets.
while (pages_queue or asset_queue) and (
    len(seen_pages) + len(seen_assets) < MAX_URLS
):
    is_page = bool(pages_queue)
    url = canonical(
        pages_queue.popleft() if is_page else asset_queue.popleft()
    )

    if not url:
        continue

    if is_page:
        if url in seen_pages or not is_internal_page(url):
            continue
        seen_pages.add(url)
    else:
        if url in seen_assets or url in seen_pages:
            continue
        seen_assets.add(url)

    result = fetch(url)
    if result is None:
        continue

    final_url, data, content_type = result
    path = safe_local_path(final_url, content_type)

    url_to_path[url] = path
    url_to_path[final_url] = path
    content_by_url[final_url] = data
    type_by_url[final_url] = content_type

    if is_html(content_type, path):
        if is_internal_page(final_url):
            soup = BeautifulSoup(data, "html.parser")
            html_references(soup, final_url)

    elif is_css(content_type, path):
        css = data.decode("utf-8", errors="replace")
        for ref in extract_css_references(css, final_url):
            add_asset(ref)

    elif is_js(content_type, path):
        js = data.decode("utf-8", errors="replace")
        for ref in extract_js_references(js, final_url):
            add_asset(ref)

    print("Collected:", final_url, "->", path)

# Phase 2: write files after discovery so references can be rewritten.
for url, data in content_by_url.items():
    path = url_to_path[url]
    content_type = type_by_url[url]

    if is_html(content_type, path):
        if is_internal_page(url):
            data = rewrite_html(data, url)

    elif is_css(content_type, path):
        css = data.decode("utf-8", errors="replace")
        data = rewrite_css(css, url).encode("utf-8")

    elif is_js(content_type, path):
        js = data.decode("utf-8", errors="replace")
        data = rewrite_js(js, url).encode("utf-8")

    save(path, data)

# Create a root entry point for GitHub Pages.
start_path = url_to_path.get(START_URL)

if not start_path:
    raise RuntimeError(
        "The starting page could not be downloaded. "
        "Check the source URL and crawler logs."
    )

start_content = content_by_url.get(START_URL)

if start_content is None:
    start_content = next(
        (
            content_by_url[u]
            for u, p in url_to_path.items()
            if p == start_path and u in content_by_url
        ),
        None,
    )

if start_content is None:
    raise RuntimeError("Could not find the starting HTML content.")

start_content = rewrite_html(start_content, START_URL)
save("index.html", start_content)

# Reports.
with open(os.path.join(OUTPUT_DIR, "crawl-report.json"), "w") as f:
    json.dump(
        {
            "start_url": START_URL,
            "pages_discovered": len(seen_pages),
            "assets_discovered": len(seen_assets),
            "total_files_saved": len(content_by_url),
            "max_urls": MAX_URLS,
            "external_domains": sorted(
                x for x in external_domains if x
            ),
            "failed_downloads": failed_urls,
            "files": sorted(set(url_to_path.values())),
        },
        f,
        indent=2,
    )

with open(os.path.join(OUTPUT_DIR, "urls.txt"), "w") as f:
    f.write("\n".join(sorted(discovered_urls)))

print()
print("Crawl complete.")
print("Pages visited:", len(seen_pages))
print("Assets visited:", len(seen_assets))
print("Failed downloads:", len(failed_urls))
print("Entry point: mirror/index.html")
print("Report: mirror/crawl-report.json")
