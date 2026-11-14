"""Client for the City of Cambridge Towed Vehicle Lookup (an ASP.NET WebForms page).

Form flow, learned in Step 0 (Nov 2026):
- GET the page; it carries __VIEWSTATE, __VIEWSTATEGENERATOR and __EVENTVALIDATION.
- POST those back with txtPlateNum (>= 3 chars, "contains" match) or ddlMake, plus the
  btnSearchTowLog button. Selecting a make alone and searching returns every tow for that make,
  so a model is not needed. Posting a value for the disabled ddlModel causes HTTP 500.
- The response is one of: a results grid (#gvSearchResults: Plate, Make/Model, Tow Date with
  seconds), a single-tow detail view (#tdTowDate etc., minutes only) when exactly one record
  matches, or the form again with "No records were found ...".
- Each grid row links to GET ?plate=<STATE>_<PLATE>, which shows the detail view (404 if gone).
- Some makes in the dropdown return HTTP 404/500 on search (seen: BENT, CHRYSLER).
- Times have no zone; they are taken as America/New_York.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .. import ET
from ..net import DataSourceError, PoliteSession

LOOKUP_URL = "https://www.cambridgema.gov/departments/emergencycommunications/towedvehicles"
P = "bodycontent_0$postcontent_0$"
ID = "bodycontent_0_postcontent_0_"
HIDDEN = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
NO_RESULTS_RE = re.compile(r"No records were found", re.I)


class SiteFormatError(DataSourceError):
    """The lookup returned an HTTP error or a page this client does not recognise."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status for HTTP errors; None for unrecognised pages


def normalize_plate(p: str) -> str:
    return re.sub(r"[\s\-]", "", p or "").upper()


def parse_tow_time(text: str) -> datetime:
    """'11/8/2026 7:27:26 PM' or '10/30/2026 12:17 PM' -> aware ET datetime truncated to minute.

    Truncating makes grid rows (seconds) and detail views (minutes) of one tow compare equal.
    """
    t = " ".join(text.split())
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p"):
        try:
            dt = datetime.strptime(t, fmt)
        except ValueError:
            continue
        return dt.replace(second=0, tzinfo=ET)
    raise SiteFormatError(f"unrecognised tow date {text!r}")


@dataclass
class TowRecord:
    plate: str
    tow_datetime: datetime
    make: str | None = None
    model: str | None = None
    tow_company: str | None = None
    reason: str | None = None
    location: str | None = None
    tracking_no: str | None = None
    detail_url: str | None = None
    is_detail: bool = False

    @property
    def plate_norm(self) -> str:
        return normalize_plate(self.plate)


@dataclass
class SearchResult:
    # "table" | "detail" | "none" | "unplated" (a single tow whose record has no plate; the
    # city's detail page cannot display it, so nothing about it is readable)
    kind: str
    records: list[TowRecord]
    models: list[str]


def _split_make_model(text: str, makes: list[str]) -> tuple[str | None, str | None]:
    text = " ".join(text.split())
    for mk in sorted(makes, key=len, reverse=True):
        if text.upper().startswith(mk.upper() + " ") or text.upper() == mk.upper():
            return mk, text[len(mk) :].strip() or None
    head, _, tail = text.partition(" ")
    return head or None, tail or None


def parse_hidden_fields(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if form is None:
        raise SiteFormatError("no <form> on lookup page")
    fields: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name or inp.has_attr("disabled") or inp.get("type") in ("submit", "button", "image"):
            continue
        fields[name] = inp.get("value", "")
    missing = [h for h in HIDDEN if h not in fields]
    if missing:
        raise SiteFormatError(f"lookup page is missing hidden field(s): {', '.join(missing)}")
    if P + "txtPlateNum" not in fields or form.find("select", attrs={"name": P + "ddlMake"}) is None:
        raise SiteFormatError("lookup page is missing the plate box or make dropdown")
    return fields


def parse_makes(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", attrs={"name": P + "ddlMake"})
    if sel is None:
        raise SiteFormatError("make dropdown not found")
    return [
        o.get("value", "")
        for o in sel.find_all("option")
        if o.get("value") not in (None, "", "Please select a make")
    ]


def parse_results(html: str, makes: list[str] | None = None, base_url: str = LOOKUP_URL) -> SearchResult:
    soup = BeautifulSoup(html, "html.parser")
    panel = soup.find(id="TowLookup")
    if panel is None:
        raise SiteFormatError("response has no #TowLookup block")
    msel = panel.find("select", attrs={"name": P + "ddlModel"})
    models = [o.get("value", "") for o in msel.find_all("option")] if msel else []

    grid = panel.find(id=ID + "gvSearchResults")
    if grid is not None:
        recs = []
        for tr in grid.find_all("tr"):
            pcell = tr.find("td", attrs={"title": "Plate"})
            dcell = tr.find("td", attrs={"title": "Tow Date"})
            if pcell is None or dcell is None:
                continue  # header or pager row
            mm = tr.find("td", attrs={"title": "Make/Model"})
            make, model = _split_make_model(mm.get_text(" ", strip=True) if mm else "", makes or [])
            a = pcell.find("a")
            recs.append(
                TowRecord(
                    plate=pcell.get_text(strip=True),
                    tow_datetime=parse_tow_time(dcell.get_text(" ", strip=True)),
                    make=make,
                    model=model,
                    detail_url=urljoin(base_url, a["href"]) if a and a.get("href") else None,
                )
            )
        if not recs:
            raise SiteFormatError("results grid present but no rows could be parsed")
        return SearchResult("table", recs, models)

    date_cell = panel.find(id=ID + "tdTowDate")
    if date_cell is not None:

        def cell(name: str) -> str | None:
            el = panel.find(id=ID + name)
            txt = " ".join(el.get_text(" ", strip=True).split()) if el else ""
            return txt or None

        plate = cell("tdPlate")
        if not plate:
            raise SiteFormatError("detail view without a plate")
        rec = TowRecord(
            plate=plate,
            tow_datetime=parse_tow_time(date_cell.get_text(" ", strip=True)),
            make=cell("tdMake"),
            model=cell("tdModel"),
            tow_company=cell("tdTowCompany"),
            reason=cell("tdTowReason"),
            location=cell("tdLocation"),
            tracking_no=cell("tdTrackingNum"),
            is_detail=True,
        )
        return SearchResult("detail", [rec], models)

    if NO_RESULTS_RE.search(panel.get_text(" ")):
        return SearchResult("none", [], models)
    raise SiteFormatError("response has no results grid, detail view or 'no records' message")


class TowClient:
    def __init__(self, http: PoliteSession, url: str = LOOKUP_URL):
        self.http = http
        self.url = url
        self._makes: list[str] | None = None

    def _get_form(self) -> tuple[str, dict[str, str]]:
        r = self.http.get(self.url)
        if r.status_code != 200:
            raise SiteFormatError(f"GET lookup page: HTTP {r.status_code}", status=r.status_code)
        fields = parse_hidden_fields(r.text)
        if self._makes is None:
            self._makes = parse_makes(r.text)
        return r.text, fields

    def _search(self, **controls: str) -> SearchResult:
        _, fields = self._get_form()
        for k, v in controls.items():
            fields[P + k] = v
        fields[P + "btnSearchTowLog"] = "Search"
        r = self.http.post(self.url, data=fields, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307):
            # A single match redirects to its detail page, ?plate=<STATE>_<PLATE>. Records with
            # no plate redirect to "?plate=_" or "?plate=MA_", which the city's site cannot render.
            location = urljoin(self.url, (getattr(r, "headers", None) or {}).get("Location", ""))
            key = unquote(parse_qs(urlparse(location).query).get("plate", [""])[0])
            if not key.partition("_")[2].strip():
                return SearchResult("unplated", [], [])
            r = self.http.get(location)
        if r.status_code != 200:
            raise SiteFormatError(f"POST search {controls}: HTTP {r.status_code}", status=r.status_code)
        return parse_results(r.text, self._makes, self.url)

    def list_makes(self) -> list[str]:
        if self._makes is None:
            self._get_form()
        return list(self._makes or [])

    def search_make(self, make: str) -> SearchResult:
        return self._search(ddlMake=make)

    def list_models(self, make: str) -> list[str]:
        """Models offered after a make search (empty when the make has a single tow)."""
        return self.search_make(make).models

    def search_make_model(self, make: str, model: str | None = None) -> SearchResult:
        if model is None:
            return self.search_make(make)
        # The model dropdown is only enabled on the page returned by a make search, and only
        # when that make has more than one tow.
        _, fields = self._get_form()
        fields[P + "ddlMake"] = make
        fields[P + "btnSearchTowLog"] = "Search"
        r = self.http.post(self.url, data=fields)
        if r.status_code != 200:
            raise SiteFormatError(f"POST make {make}: HTTP {r.status_code}", status=r.status_code)
        fields2 = parse_hidden_fields(r.text)
        fields2[P + "ddlMake"] = make
        fields2[P + "ddlModel"] = model
        fields2[P + "btnSearchTowLog"] = "Search"
        r2 = self.http.post(self.url, data=fields2)
        if r2.status_code != 200:
            raise SiteFormatError(f"POST model {make}/{model}: HTTP {r2.status_code}", status=r2.status_code)
        return parse_results(r2.text, self._makes, self.url)

    def search_plate(self, text: str) -> SearchResult:
        q = normalize_plate(text)
        if len(q) < 3:
            raise ValueError("plate search needs at least 3 characters")
        return self._search(txtPlateNum=q)

    def get_detail(self, url: str) -> TowRecord | None:
        """Follow a grid row's ?plate= link. None when the record is gone (HTTP 404)."""
        r = self.http.get(url)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise SiteFormatError(f"GET detail: HTTP {r.status_code}", status=r.status_code)
        res = parse_results(r.text, self._makes, self.url)
        return res.records[0] if res.kind == "detail" else None
