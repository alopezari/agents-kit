#!/usr/bin/env python3
"""The public site takes the decision page's palette, light by default and dark under prefers-color-scheme: dark: each
colour role resolves to CHANGE_STYLE's value for it, no colour of the old palette is left in site/src, and the inline
diagrams take every colour from the page's tokens."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import glob
import os
import re

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
SITE = os.path.join(KIT, "site", "src")
# Site token → the decision page's token for the same role.
ROLES = {"bg": "bg", "surface": "card", "line": "line", "text": "text", "body": "soft", "muted": "soft", "dim": "faint",
         "accent": "blue", "human": "amber", "pass": "pass"}
OLD_HEX = ("0E100F", "161917", "2A2E2B", "E8E6DF", "CFCDC5", "B5B3AB", "8A8F88", "C6EC5A", "E2F7A6", "FF9B73")
OLD_RGB = [tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) for h in OLD_HEX]
DIAGRAMS = ("flow.svg", "flow-narrow.svg", "architecture.svg", "loop.svg")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def tokens(block):
    return {name: value.upper() for name, value in re.findall(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})\b", block)}


def light_and_dark(css):
    """The custom properties of the first :root rule, and of the :root rule under prefers-color-scheme: dark."""
    light = re.search(r":root\s*\{([^}]*)\}", css)
    dark = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}", css)
    return tokens(light.group(1)) if light else {}, tokens(dark.group(1)) if dark else {}


def luminance(hex_colour):
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


source = open(os.path.join(KIT, "bin", "decision-report")).read()
change_style = re.search(r'^CHANGE_STYLE = """(.*?)"""', source, re.S | re.M).group(1)
page_light, page_dark = light_and_dark(change_style)
styles = open(os.path.join(SITE, "styles.css")).read()
site_light, site_dark = light_and_dark(styles)
failures = []

if not re.search(r":root\s*\{[^}]*color-scheme:\s*light dark", styles):
    failures.append("styles.css's :root doesn't declare color-scheme: light dark")
for theme, site, page in (("light", site_light, page_light), ("dark", site_dark, page_dark)):
    for role, page_role in ROLES.items():
        if site.get(role) != page.get(page_role):
            failures.append(f"{theme}: --{role} is {site.get(role, 'missing')}, the decision page's --{page_role} is {page.get(page_role)}")
    hover, accent, bg = site.get("accent-hover"), site.get("accent"), site.get("bg")
    if not (hover and accent and bg):
        failures.append(f"{theme}: --accent-hover, --accent or --bg is missing")
        continue
    if (luminance(hover) < luminance(accent)) != (theme == "light"):
        failures.append(f"{theme}: --accent-hover {hover} should be {'darker' if theme == 'light' else 'lighter'} than --accent {accent}")
    if contrast(hover, bg) < 4.5:
        failures.append(f"{theme}: --accent-hover {hover} on --bg {bg} is {contrast(hover, bg):.2f}:1, under AA's 4.5:1")

allowed = set(page_light.values()) | set(page_dark.values()) | {site_light.get("accent-hover"), site_dark.get("accent-hover")}
old_colour = re.compile("|".join([rf"#{h}\b" for h in OLD_HEX]
                                 + [rf"rgba?\(\s*{r}[\s,]+{g}[\s,]+{b}\b" for r, g, b in OLD_RGB]), re.I)
for path in sorted(glob.glob(os.path.join(SITE, "**", "*"), recursive=True)):
    if not os.path.isfile(path):
        continue
    name = os.path.relpath(path, KIT)
    for n, line in enumerate(re.sub(r'href="#[^"]*"', "", open(path).read()).splitlines(), 1):
        for found in old_colour.findall(line):
            failures.append(f"{name}:{n}: old colour {found}")
        for found in HEX.findall(line):
            if found.upper() not in allowed and not old_colour.search(found):
                failures.append(f"{name}:{n}: {found} is not a colour of the decision page's palette")

for diagram in DIAGRAMS:
    svg = open(os.path.join(SITE, "svg", diagram)).read()
    literal = HEX.findall(svg) + re.findall(r"\b(?:rgba?|hsla?)\(", svg)
    literal += [f'{attribute}="{value}"' for attribute, value in re.findall(r'\b(fill|stroke)="([^"]*)"', svg) if value != "none"]
    if literal:
        failures.append(f"site/src/svg/{diagram} has literal colours: {sorted(set(literal))}")

    for used in sorted(set(re.findall(r"\b((?:fill|stroke)-[\w-]+)", " ".join(re.findall(r'class="([^"]*)"', svg))))):
        if not re.search(rf"\.{used}\s*\{{", styles):
            failures.append(f"site/src/svg/{diagram} uses .{used}, which styles.css doesn't define")

favicon = open(os.path.join(SITE, "favicon.svg")).read()
favicon_light, _, favicon_dark = favicon.partition("prefers-color-scheme:dark")
for theme, part, site in (("light", favicon_light, site_light), ("dark", favicon_dark, site_dark)):
    for element, role in (("rect", "bg"), ("text", "accent")):
        found = re.search(rf"\b{element}\{{fill:(#[0-9a-fA-F]{{6}})", part)
        if not found or found.group(1).upper() != site.get(role):
            failures.append(f"favicon.svg's {theme} {element} is {found and found.group(1)}, the site's --{role} is {site.get(role)}")

for page in ("index.html", "docs.html"):
    markup = open(os.path.join(SITE, page)).read()
    for theme, site in (("light", site_light), ("dark", site_dark)):
        found = re.search(rf'<meta name="theme-color" content="(#[0-9a-fA-F]{{6}})" media="\(prefers-color-scheme: {theme}\)">', markup)
        if not found or found.group(1).upper() != site.get("bg"):
            failures.append(f"{page}'s {theme} theme-color is {found and found.group(1)}, the site's --bg is {site.get('bg')}")

docs = open(os.path.join(SITE, "docs.html")).read()
mermaid = re.search(r"mermaid\.initialize\((.*?)\);", docs, re.S).group(1)
if "getPropertyValue" not in docs:
    failures.append("docs.html's Mermaid setup doesn't read the page's tokens")
for needed in ('useGradient: false', 'dropShadow: "none"', 'look: "classic"'):
    if needed not in mermaid:
        failures.append(f"docs.html's Mermaid setup lacks {needed}")
for name in re.findall(r'token\("([\w-]+)"\)', mermaid):
    if name not in site_light or name not in site_dark:
        failures.append(f"docs.html's Mermaid setup reads --{name}, which styles.css doesn't define in both themes")
defined = set(re.findall(r"--([\w-]+)\s*:", styles))
for root, _, names in os.walk(SITE):
    for name in names:
        for used in sorted(set(re.findall(r"var\(--([\w-]+)", open(os.path.join(root, name), errors="replace").read()))):
            if used not in defined:
                failures.append(f"{name} uses --{used}, which styles.css doesn't define")
if HEX.search(mermaid):
    failures.append("docs.html's Mermaid setup copies colours instead of reading the page's tokens")

for failure in failures:
    print(f"FAIL {failure}")
if not failures:
    print("ok   the site's colour roles match the decision page's palette in light and dark, and no old colour is left")
raise SystemExit(1 if failures else 0)
