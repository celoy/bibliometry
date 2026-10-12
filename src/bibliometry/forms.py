"""The setup form shown in ``notebooks/00_setup.ipynb`` (ipywidgets).

Kept in the package rather than in the notebook so the notebook stays a few short
cells and the form logic can be read and fixed in one place.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import ipywidgets as w
from IPython.display import display

from .config import (
    Config,
    ConfigError,
    load_api_key,
    save_api_key,
)
from .institution import describe_institution, find_institutions
from .openalex import OpenAlexClient, institution_works_filter
from .pipeline import new_config

WIDE = w.Layout(width="95%")
STYLE = {"description_width": "180px"}


def _existing(root: Path) -> Config | None:
    try:
        return Config.load(root / "config.yaml")
    except ConfigError:
        return None


def setup_form(root: Path | str = ".") -> None:
    """Display the form; *Save* writes ``config.yaml`` (and ``.env`` for the API key)."""
    root = Path(root).resolve()
    previous = _existing(root)
    this_year = date.today().year

    email = w.Text(
        value=previous.mailto if previous else "",
        description="Email",
        placeholder="you@university.fr",
        layout=WIDE,
        style=STYLE,
    )
    api_key = w.Password(
        value=load_api_key(root),
        description="OpenAlex API key",
        placeholder="optional but recommended — free at openalex.org/settings/api",
        layout=WIDE,
        style=STYLE,
    )
    query = w.Text(
        value=previous.institution.id if previous else "",
        description="Institution",
        placeholder="name, OpenAlex id (I…) or ROR",
        layout=w.Layout(width="70%"),
        style=STYLE,
    )
    search = w.Button(description="Search", icon="search")
    choices = w.Dropdown(options=[], description="Pick one", layout=WIDE, style=STYLE)
    roster_path = w.Text(
        value=previous.roster.path if previous else "",
        description="List of people",
        placeholder="path (e.g. data/input/list.xlsx) or URL (incl. Google Sheets link)",
        layout=WIDE,
        style=STYLE,
    )
    upload = w.FileUpload(accept=".csv,.xlsx,.xls", multiple=False, description="…or upload")
    sheets = w.Text(
        value=", ".join(previous.roster.sheets) if previous else "",
        description="Sheets (optional)",
        placeholder="comma-separated sheet names; empty = all sheets",
        layout=WIDE,
        style=STYLE,
    )
    start = w.DatePicker(
        value=date.fromisoformat(previous.from_date) if previous else date(this_year - 5, 1, 1),
        description="From",
        style=STYLE,
    )
    end = w.DatePicker(
        value=date.fromisoformat(previous.to_date) if previous else date(this_year - 1, 12, 31),
        description="To",
        style=STYLE,
    )
    save = w.Button(description="Save and test", button_style="primary", icon="check")
    out = w.Output()
    found: dict[str, dict] = {}

    def client() -> OpenAlexClient:
        return OpenAlexClient(mailto=email.value.strip(), api_key=api_key.value.strip())

    def on_search(_button: w.Button) -> None:
        out.clear_output()
        with out, client() as c:
            try:
                records = find_institutions(c, query.value)
            except Exception as exc:  # noqa: BLE001 - show any API failure to the user
                print(f"Search failed: {exc}")
                return
            found.clear()
            for record in records:
                found[describe_institution(record)] = record
            choices.options = list(found)
            print(
                f"{len(records)} institution(s) found — pick one above."
                if records
                else "Nothing found."
            )

    def on_upload(change: dict) -> None:
        files = change["new"]
        if not files:
            return
        item = files[0] if isinstance(files, (list, tuple)) else next(iter(files.values()))
        name = (
            item["name"] if "name" in item else item.get("metadata", {}).get("name", "roster.xlsx")
        )
        target = root / "data" / "input" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(item["content"]))
        roster_path.value = str(target.relative_to(root))
        with out:
            print(f"Saved upload to {target}")

    def on_save(_button: w.Button) -> None:
        out.clear_output()
        with out:
            if not choices.value:
                on_search(_button)
                if not choices.value:
                    return
            record = found[choices.value]
            config = new_config(
                email.value,
                record,
                roster_path.value,
                start.value.isoformat() if start.value else "",
                end.value.isoformat() if end.value else "",
                root,
                sheets=[s.strip() for s in sheets.value.split(",") if s.strip()],
            )
            if api_key.value.strip():
                save_api_key(api_key.value, root)
            try:
                config.validate()
            except ConfigError as exc:
                print(exc)
                return
            print(f"Saved {config.save(root / 'config.yaml')}")
            with client() as c:
                n = c.count(
                    "/works",
                    institution_works_filter(
                        config.institution.id, config.from_date, config.to_date
                    ),
                )
                print(
                    f"Connection OK. {config.institution.name} has {n:,} works between "
                    f"{config.from_date} and {config.to_date}.\nOpenAlex: {c.budget}"
                )
                if not c.api_key:
                    print(
                        "Tip: without an API key the daily budget is small (~1000 requests); "
                        "a large run may have to be finished the next day."
                    )

    search.on_click(on_search)
    upload.observe(on_upload, names="value")
    save.on_click(on_save)
    if previous:
        on_search(search)

    display(
        w.VBox(
            [
                w.HTML("<b>1. Who you are</b>"),
                email,
                api_key,
                w.HTML("<b>2. Institution</b>"),
                w.HBox([query, search]),
                choices,
                w.HTML("<b>3. List of people</b>"),
                roster_path,
                upload,
                sheets,
                w.HTML("<b>4. Period</b>"),
                w.HBox([start, end]),
                save,
                out,
            ]
        )
    )
