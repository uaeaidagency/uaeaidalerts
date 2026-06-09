"""
UAE Aid Agency — Tier-2+ Alert Agent
World country recognition & description.

Lets the Telegram bot recognize ANY country on Earth, not just the ~25
monitored crises. For a country with no active crisis, the bot uses this
module to describe the country (region, capital, population, languages).

Recognition + enrichment uses the free restcountries.com API (no key),
with an embedded fallback table so the most-queried countries still work
if the API is unreachable.

DEPENDENCIES: Standard library only (urllib, json).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Optional

# Words stripped from a query so we can isolate the country name.
_FILLER = {
    "update", "updates", "on", "the", "whats", "what's", "what", "is", "are",
    "happening", "in", "crisis", "crises", "situation", "country", "tell",
    "me", "about", "give", "status", "of", "any", "current", "now", "report",
    "info", "information", "a", "an", "for", "please", "hey", "hi", "hello",
    "update", "/update", "/country", "to", "with", "and", "there", "do", "we",
    "have", "has", "anything", "whatabout", "how", "going",
}

# Aliases / informal names → the term we send to restcountries.
_ALIASES = {
    "uae": "United Arab Emirates",
    "usa": "United States",
    "us": "United States",
    "america": "United States",
    "uk": "United Kingdom",
    "britain": "United Kingdom",
    "great britain": "United Kingdom",
    "drc": "DR Congo",
    "dr congo": "Democratic Republic of the Congo",
    "congo": "Congo",
    "north korea": "North Korea",
    "south korea": "South Korea",
    "ivory coast": "Côte d'Ivoire",
    "cote d'ivoire": "Côte d'Ivoire",
    "burma": "Myanmar",
    "turkey": "Turkey",
    "czech republic": "Czechia",
    "swaziland": "Eswatini",
    "cape verde": "Cabo Verde",
    "russia": "Russia",
    "iran": "Iran",
    "syria": "Syria",
    "vietnam": "Vietnam",
    "laos": "Laos",
    "moldova": "Moldova",
    "bolivia": "Bolivia",
    "tanzania": "Tanzania",
    "venezuela": "Venezuela",
}

# Complete embedded country database: name → (region, capital).
# Covers every UN member state, observer states, and commonly-queried
# territories — so describe() NEVER returns None for a real country, even
# when restcountries.com is unreachable. The API is used only to enrich
# with population & languages when available.
_FALLBACK = {
    # ── Africa ──────────────────────────────────────────────────────────
    "algeria": ("Northern Africa", "Algiers"),
    "angola": ("Middle Africa", "Luanda"),
    "benin": ("Western Africa", "Porto-Novo"),
    "botswana": ("Southern Africa", "Gaborone"),
    "burkina faso": ("Western Africa", "Ouagadougou"),
    "burundi": ("Eastern Africa", "Gitega"),
    "cabo verde": ("Western Africa", "Praia"),
    "cape verde": ("Western Africa", "Praia"),
    "cameroon": ("Middle Africa", "Yaoundé"),
    "central african republic": ("Middle Africa", "Bangui"),
    "chad": ("Middle Africa", "N'Djamena"),
    "comoros": ("Eastern Africa", "Moroni"),
    "congo": ("Middle Africa", "Brazzaville"),
    "republic of the congo": ("Middle Africa", "Brazzaville"),
    "democratic republic of the congo": ("Middle Africa", "Kinshasa"),
    "dr congo": ("Middle Africa", "Kinshasa"),
    "côte d'ivoire": ("Western Africa", "Yamoussoukro"),
    "cote d'ivoire": ("Western Africa", "Yamoussoukro"),
    "ivory coast": ("Western Africa", "Yamoussoukro"),
    "djibouti": ("Eastern Africa", "Djibouti"),
    "egypt": ("Northern Africa", "Cairo"),
    "equatorial guinea": ("Middle Africa", "Malabo"),
    "eritrea": ("Eastern Africa", "Asmara"),
    "eswatini": ("Southern Africa", "Mbabane"),
    "swaziland": ("Southern Africa", "Mbabane"),
    "ethiopia": ("Eastern Africa", "Addis Ababa"),
    "gabon": ("Middle Africa", "Libreville"),
    "gambia": ("Western Africa", "Banjul"),
    "ghana": ("Western Africa", "Accra"),
    "guinea": ("Western Africa", "Conakry"),
    "guinea-bissau": ("Western Africa", "Bissau"),
    "kenya": ("Eastern Africa", "Nairobi"),
    "lesotho": ("Southern Africa", "Maseru"),
    "liberia": ("Western Africa", "Monrovia"),
    "libya": ("Northern Africa", "Tripoli"),
    "madagascar": ("Eastern Africa", "Antananarivo"),
    "malawi": ("Eastern Africa", "Lilongwe"),
    "mali": ("Western Africa", "Bamako"),
    "mauritania": ("Western Africa", "Nouakchott"),
    "mauritius": ("Eastern Africa", "Port Louis"),
    "morocco": ("Northern Africa", "Rabat"),
    "mozambique": ("Eastern Africa", "Maputo"),
    "namibia": ("Southern Africa", "Windhoek"),
    "niger": ("Western Africa", "Niamey"),
    "nigeria": ("Western Africa", "Abuja"),
    "rwanda": ("Eastern Africa", "Kigali"),
    "sao tome and principe": ("Middle Africa", "São Tomé"),
    "senegal": ("Western Africa", "Dakar"),
    "seychelles": ("Eastern Africa", "Victoria"),
    "sierra leone": ("Western Africa", "Freetown"),
    "somalia": ("Eastern Africa", "Mogadishu"),
    "south africa": ("Southern Africa", "Pretoria"),
    "south sudan": ("Eastern Africa", "Juba"),
    "sudan": ("Northern Africa", "Khartoum"),
    "tanzania": ("Eastern Africa", "Dodoma"),
    "togo": ("Western Africa", "Lomé"),
    "tunisia": ("Northern Africa", "Tunis"),
    "uganda": ("Eastern Africa", "Kampala"),
    "zambia": ("Eastern Africa", "Lusaka"),
    "zimbabwe": ("Eastern Africa", "Harare"),
    # ── Western & Central Asia / Middle East ────────────────────────────
    "afghanistan": ("Southern Asia", "Kabul"),
    "armenia": ("Western Asia", "Yerevan"),
    "azerbaijan": ("Western Asia", "Baku"),
    "bahrain": ("Western Asia", "Manama"),
    "cyprus": ("Western Asia", "Nicosia"),
    "georgia": ("Western Asia", "Tbilisi"),
    "iran": ("Southern Asia", "Tehran"),
    "iraq": ("Western Asia", "Baghdad"),
    "israel": ("Western Asia", "Jerusalem"),
    "jordan": ("Western Asia", "Amman"),
    "kazakhstan": ("Central Asia", "Astana"),
    "kuwait": ("Western Asia", "Kuwait City"),
    "kyrgyzstan": ("Central Asia", "Bishkek"),
    "lebanon": ("Western Asia", "Beirut"),
    "oman": ("Western Asia", "Muscat"),
    "palestine": ("Western Asia", "Ramallah"),
    "state of palestine": ("Western Asia", "Ramallah"),
    "qatar": ("Western Asia", "Doha"),
    "saudi arabia": ("Western Asia", "Riyadh"),
    "syria": ("Western Asia", "Damascus"),
    "syrian arab republic": ("Western Asia", "Damascus"),
    "tajikistan": ("Central Asia", "Dushanbe"),
    "turkey": ("Western Asia", "Ankara"),
    "türkiye": ("Western Asia", "Ankara"),
    "turkmenistan": ("Central Asia", "Ashgabat"),
    "united arab emirates": ("Western Asia", "Abu Dhabi"),
    "uzbekistan": ("Central Asia", "Tashkent"),
    "yemen": ("Western Asia", "Sana'a"),
    # ── South & East & Southeast Asia ───────────────────────────────────
    "bangladesh": ("Southern Asia", "Dhaka"),
    "bhutan": ("Southern Asia", "Thimphu"),
    "brunei": ("South-Eastern Asia", "Bandar Seri Begawan"),
    "cambodia": ("South-Eastern Asia", "Phnom Penh"),
    "china": ("Eastern Asia", "Beijing"),
    "india": ("Southern Asia", "New Delhi"),
    "indonesia": ("South-Eastern Asia", "Jakarta"),
    "japan": ("Eastern Asia", "Tokyo"),
    "laos": ("South-Eastern Asia", "Vientiane"),
    "malaysia": ("South-Eastern Asia", "Kuala Lumpur"),
    "maldives": ("Southern Asia", "Malé"),
    "mongolia": ("Eastern Asia", "Ulaanbaatar"),
    "myanmar": ("South-Eastern Asia", "Naypyidaw"),
    "burma": ("South-Eastern Asia", "Naypyidaw"),
    "nepal": ("Southern Asia", "Kathmandu"),
    "north korea": ("Eastern Asia", "Pyongyang"),
    "pakistan": ("Southern Asia", "Islamabad"),
    "philippines": ("South-Eastern Asia", "Manila"),
    "singapore": ("South-Eastern Asia", "Singapore"),
    "south korea": ("Eastern Asia", "Seoul"),
    "sri lanka": ("Southern Asia", "Sri Jayawardenepura Kotte"),
    "taiwan": ("Eastern Asia", "Taipei"),
    "thailand": ("South-Eastern Asia", "Bangkok"),
    "timor-leste": ("South-Eastern Asia", "Dili"),
    "east timor": ("South-Eastern Asia", "Dili"),
    "vietnam": ("South-Eastern Asia", "Hanoi"),
    # ── Europe ──────────────────────────────────────────────────────────
    "albania": ("Southern Europe", "Tirana"),
    "andorra": ("Southern Europe", "Andorra la Vella"),
    "austria": ("Western Europe", "Vienna"),
    "belarus": ("Eastern Europe", "Minsk"),
    "belgium": ("Western Europe", "Brussels"),
    "bosnia and herzegovina": ("Southern Europe", "Sarajevo"),
    "bulgaria": ("Eastern Europe", "Sofia"),
    "croatia": ("Southern Europe", "Zagreb"),
    "czechia": ("Central Europe", "Prague"),
    "czech republic": ("Central Europe", "Prague"),
    "denmark": ("Northern Europe", "Copenhagen"),
    "estonia": ("Northern Europe", "Tallinn"),
    "finland": ("Northern Europe", "Helsinki"),
    "france": ("Western Europe", "Paris"),
    "germany": ("Western Europe", "Berlin"),
    "greece": ("Southern Europe", "Athens"),
    "hungary": ("Central Europe", "Budapest"),
    "iceland": ("Northern Europe", "Reykjavík"),
    "ireland": ("Northern Europe", "Dublin"),
    "italy": ("Southern Europe", "Rome"),
    "kosovo": ("Southern Europe", "Pristina"),
    "latvia": ("Northern Europe", "Riga"),
    "liechtenstein": ("Western Europe", "Vaduz"),
    "lithuania": ("Northern Europe", "Vilnius"),
    "luxembourg": ("Western Europe", "Luxembourg"),
    "malta": ("Southern Europe", "Valletta"),
    "moldova": ("Eastern Europe", "Chișinău"),
    "monaco": ("Western Europe", "Monaco"),
    "montenegro": ("Southern Europe", "Podgorica"),
    "netherlands": ("Western Europe", "Amsterdam"),
    "north macedonia": ("Southern Europe", "Skopje"),
    "macedonia": ("Southern Europe", "Skopje"),
    "norway": ("Northern Europe", "Oslo"),
    "poland": ("Central Europe", "Warsaw"),
    "portugal": ("Southern Europe", "Lisbon"),
    "romania": ("Eastern Europe", "Bucharest"),
    "russia": ("Eastern Europe", "Moscow"),
    "san marino": ("Southern Europe", "San Marino"),
    "serbia": ("Southern Europe", "Belgrade"),
    "slovakia": ("Central Europe", "Bratislava"),
    "slovenia": ("Southern Europe", "Ljubljana"),
    "spain": ("Southern Europe", "Madrid"),
    "sweden": ("Northern Europe", "Stockholm"),
    "switzerland": ("Western Europe", "Bern"),
    "ukraine": ("Eastern Europe", "Kyiv"),
    "united kingdom": ("Northern Europe", "London"),
    "vatican city": ("Southern Europe", "Vatican City"),
    "holy see": ("Southern Europe", "Vatican City"),
    # ── Americas ────────────────────────────────────────────────────────
    "antigua and barbuda": ("Caribbean", "Saint John's"),
    "argentina": ("South America", "Buenos Aires"),
    "bahamas": ("Caribbean", "Nassau"),
    "barbados": ("Caribbean", "Bridgetown"),
    "belize": ("Central America", "Belmopan"),
    "bolivia": ("South America", "Sucre"),
    "brazil": ("South America", "Brasília"),
    "canada": ("North America", "Ottawa"),
    "chile": ("South America", "Santiago"),
    "colombia": ("South America", "Bogotá"),
    "costa rica": ("Central America", "San José"),
    "cuba": ("Caribbean", "Havana"),
    "dominica": ("Caribbean", "Roseau"),
    "dominican republic": ("Caribbean", "Santo Domingo"),
    "ecuador": ("South America", "Quito"),
    "el salvador": ("Central America", "San Salvador"),
    "grenada": ("Caribbean", "Saint George's"),
    "guatemala": ("Central America", "Guatemala City"),
    "guyana": ("South America", "Georgetown"),
    "haiti": ("Caribbean", "Port-au-Prince"),
    "honduras": ("Central America", "Tegucigalpa"),
    "jamaica": ("Caribbean", "Kingston"),
    "mexico": ("North America", "Mexico City"),
    "nicaragua": ("Central America", "Managua"),
    "panama": ("Central America", "Panama City"),
    "paraguay": ("South America", "Asunción"),
    "peru": ("South America", "Lima"),
    "saint kitts and nevis": ("Caribbean", "Basseterre"),
    "saint lucia": ("Caribbean", "Castries"),
    "saint vincent and the grenadines": ("Caribbean", "Kingstown"),
    "suriname": ("South America", "Paramaribo"),
    "trinidad and tobago": ("Caribbean", "Port of Spain"),
    "united states": ("North America", "Washington, D.C."),
    "uruguay": ("South America", "Montevideo"),
    "venezuela": ("South America", "Caracas"),
    # ── Oceania ─────────────────────────────────────────────────────────
    "australia": ("Oceania", "Canberra"),
    "fiji": ("Oceania", "Suva"),
    "kiribati": ("Oceania", "Tarawa"),
    "marshall islands": ("Oceania", "Majuro"),
    "micronesia": ("Oceania", "Palikir"),
    "nauru": ("Oceania", "Yaren"),
    "new zealand": ("Oceania", "Wellington"),
    "palau": ("Oceania", "Ngerulmud"),
    "papua new guinea": ("Oceania", "Port Moresby"),
    "samoa": ("Oceania", "Apia"),
    "solomon islands": ("Oceania", "Honiara"),
    "tonga": ("Oceania", "Nuku'alofa"),
    "tuvalu": ("Oceania", "Funafuti"),
    "vanuatu": ("Oceania", "Port Vila"),
}


def _clean(text: str) -> str:
    raw = text.lower().replace("?", " ").replace(",", " ").replace(".", " ")
    words = [w for w in raw.split() if w not in _FILLER]
    return " ".join(words).strip()


def _query_restcountries(name: str) -> Optional[dict]:
    try:
        url = (
            "https://restcountries.com/v3.1/name/"
            + urllib.parse.quote(name)
            + "?fields=name,region,subregion,capital,population,languages,borders,cca2"
        )
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if isinstance(data, list) and data:
            # Prefer an exact common-name match.
            for c in data:
                common = (c.get("name", {}) or {}).get("common", "").lower()
                if common == name.lower():
                    return c
            return data[0]
    except Exception:
        return None
    return None


def _humanize_population(pop) -> str:
    if not pop:
        return ""
    try:
        pop = int(pop)
    except (ValueError, TypeError):
        return ""
    if pop >= 1_000_000_000:
        return f"{pop/1_000_000_000:.2f}B"
    if pop >= 1_000_000:
        return f"{pop/1_000_000:.1f}M"
    if pop >= 1_000:
        return f"{pop/1_000:.0f}K"
    return str(pop)


# Proper display names for embedded keys whose .title() would be wrong.
_DISPLAY_NAME = {
    "dr congo": "Democratic Republic of the Congo",
    "côte d'ivoire": "Côte d'Ivoire",
    "cote d'ivoire": "Côte d'Ivoire",
    "ivory coast": "Côte d'Ivoire",
    "united arab emirates": "United Arab Emirates",
    "united states": "United States",
    "united kingdom": "United Kingdom",
    "north macedonia": "North Macedonia",
    "bosnia and herzegovina": "Bosnia and Herzegovina",
    "sao tome and principe": "São Tomé and Príncipe",
    "timor-leste": "Timor-Leste",
    "guinea-bissau": "Guinea-Bissau",
    "vatican city": "Vatican City",
    "holy see": "Holy See",
    "türkiye": "Türkiye",
    "syrian arab republic": "Syria",
    "state of palestine": "Palestine",
}


def _match_embedded(cleaned: str, query: str) -> Optional[str]:
    """Return the embedded-table key that best matches the cleaned query,
    or None. Tries exact match, then longest substring, then token-subset."""
    ql = (query or "").lower().strip()
    # 1) exact match on cleaned text or alias-resolved query
    if cleaned in _FALLBACK:
        return cleaned
    if ql in _FALLBACK:
        return ql
    # 2) substring either way (longest key wins → "south sudan" beats "sudan")
    candidates = [k for k in _FALLBACK if k in cleaned or cleaned in k]
    if candidates:
        candidates.sort(key=len, reverse=True)
        return candidates[0]
    # 3) token-subset: all words of a country key appear in the message
    words = set(cleaned.split())
    best = None
    for k in _FALLBACK:
        kw = set(k.split())
        if kw and kw.issubset(words):
            if best is None or len(k) > len(best):
                best = k
    return best


def describe(text: str) -> Optional[dict]:
    """Identify the country mentioned in `text` and return a description dict,
    or None if no country can be recognized.

    Recognition is driven by the COMPLETE embedded table (works offline for
    every country). The restcountries.com API is used only to ENRICH with
    population and languages when reachable.

    Returns: {name, region, capital, population, languages}
    """
    cleaned = _clean(text)
    if not cleaned:
        return None

    query = _ALIASES.get(cleaned, cleaned)

    # 1) Recognise against the embedded table first (reliable, offline).
    key = _match_embedded(cleaned, query)

    # 2) Enrich via API (population, languages, exact capital). Best-effort.
    api_query = _DISPLAY_NAME.get(key, query) if key else query
    info = _query_restcountries(api_query)
    if info:
        name = (info.get("name", {}) or {}).get("common") \
            or _DISPLAY_NAME.get(key, key.title() if key else query.title())
        region = info.get("subregion") or info.get("region") \
            or (_FALLBACK.get(key, ("", ""))[0] if key else "")
        capital_list = info.get("capital") or []
        capital = (capital_list[0] if capital_list
                   else (_FALLBACK.get(key, ("", ""))[1] if key else ""))
        population = _humanize_population(info.get("population"))
        langs = ", ".join((info.get("languages") or {}).values())
        return {
            "name": name,
            "region": region,
            "capital": capital,
            "population": population,
            "languages": langs,
        }

    # 3) Embedded fallback (API unreachable but country is recognised).
    if key:
        region, capital = _FALLBACK[key]
        return {
            "name": _DISPLAY_NAME.get(key, key.title()),
            "region": region,
            "capital": capital,
            "population": "",
            "languages": "",
        }
    return None


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "what is happening in norway"
    print(json.dumps(describe(q), indent=2, ensure_ascii=False))
