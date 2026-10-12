"""Command line: ``bibliometry <step>``. Same steps as the notebooks.

bibliometry init                 # interactive questions -> config.yaml (+ .env)
bibliometry run                  # every step below, in order
bibliometry roster               # check how the roster file is read
bibliometry harvest-institution  # all works of the institution
bibliometry match                # roster -> OpenAlex profiles (review file)
bibliometry harvest-people       # all works of the accepted profiles
bibliometry tables               # parquet tables + Excel export
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from . import pipeline
from .config import (
    DEFAULT_CONFIG,
    Config,
    ConfigError,
    save_api_key,
)
from .institution import describe_institution, find_institutions
from .openalex import BudgetExhaustedError, OpenAlexClient
from .roster import RosterError, summarise_roster

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)
ConfigOption = typer.Option(DEFAULT_CONFIG, "--config", "-c", help="Path to config.yaml.")


def _load(path: Path, need_roster: bool = True) -> Config:
    try:
        config = Config.load(path)
        config.validate(need_roster=need_roster)
    except ConfigError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    return config


@app.command()
def init(config_path: Path = ConfigOption) -> None:
    """Ask the questions and write config.yaml (the API key goes to .env)."""
    mailto = typer.prompt("Your email address (identifies you to OpenAlex)")
    api_key = typer.prompt(
        "OpenAlex API key (free at https://openalex.org/settings/api; Enter to skip)",
        default="",
        show_default=False,
        hide_input=True,
    )
    root = config_path.resolve().parent
    if api_key:
        save_api_key(api_key, root)
    query = typer.prompt("Institution (name, OpenAlex id or ROR)")
    with OpenAlexClient(mailto=mailto, api_key=api_key) as client:
        found = find_institutions(client, query)
    if not found:
        typer.secho("No institution found; try another spelling.", fg="red")
        raise typer.Exit(1)
    for index, record in enumerate(found, start=1):
        typer.echo(f"  {index}. {describe_institution(record)}")
    choice = typer.prompt("Which one?", default=1, type=int)
    record = found[choice - 1]
    roster = typer.prompt("Roster file (path or URL of a CSV/XLSX)")
    this_year = date.today().year
    from_date = typer.prompt("From date (YYYY-MM-DD)", default=f"{this_year - 5}-01-01")
    to_date = typer.prompt("To date (YYYY-MM-DD)", default=f"{this_year - 1}-12-31")
    config = pipeline.new_config(mailto, record, roster, from_date, to_date, root)
    try:
        config.validate()
    except ConfigError as exc:
        typer.secho(str(exc), fg="yellow")
    typer.echo(f"Wrote {config.save(config_path)}")


@app.command()
def roster(config_path: Path = ConfigOption) -> None:
    """Read the roster and show what was detected."""
    config = _load(config_path)
    try:
        table = pipeline.load_roster(config)
    except RosterError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    typer.echo(summarise_roster(table).to_string())
    typer.echo(f"Saved {config.roster_table}")


def _with_client(config: Config, action) -> None:
    with pipeline.make_client(config) as client:
        try:
            action(client)
        except BudgetExhaustedError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(2) from exc
        typer.echo(f"OpenAlex: {client.budget}")


@app.command("harvest-institution")
def harvest_institution(
    config_path: Path = ConfigOption,
    refresh: bool = typer.Option(False, help="Re-fetch even if already complete."),
) -> None:
    """Fetch every work of the institution in the period."""
    config = _load(config_path, need_roster=False)
    _with_client(
        config,
        lambda c: typer.echo(pipeline.harvest_institution_step(config, c, refresh, typer.echo)),
    )


@app.command()
def match(
    config_path: Path = ConfigOption,
    offline: bool = typer.Option(False, help="Use cached OpenAlex answers only."),
    no_search: bool = typer.Option(False, help="Skip the (costlier) name searches."),
) -> None:
    """Match roster people to OpenAlex profiles; writes data/review/author_matches.csv."""
    config = _load(config_path)

    def run(client: OpenAlexClient | None) -> None:
        _, consolidated = pipeline.match_step(
            config, client, search_missing=not no_search, progress=typer.echo
        )
        typer.echo(consolidated.match_status.value_counts().to_string())
        typer.echo(
            f"Review {config.matches_review}: set 'verdict' to ok/wrong where needed, "
            "then run `bibliometry match --offline` again."
        )

    if offline:
        run(None)
    else:
        _with_client(config, run)


@app.command("harvest-people")
def harvest_people(
    config_path: Path = ConfigOption,
    refresh: bool = typer.Option(False, help="Re-fetch even if already complete."),
) -> None:
    """Fetch every work in the period of the accepted profiles."""
    config = _load(config_path)
    _with_client(
        config, lambda c: typer.echo(pipeline.harvest_people_step(config, c, refresh, typer.echo))
    )


@app.command()
def tables(config_path: Path = ConfigOption) -> None:
    """Build parquet tables and the Excel export."""
    config = _load(config_path, need_roster=False)
    for name, frame in pipeline.tables_step(config, typer.echo).items():
        typer.echo(f"  {name}: {len(frame):,} rows")


@app.command("lists")
def lists_command(
    config_path: Path = ConfigOption,
    offline: bool = typer.Option(False, help="Use cached venue records only."),
) -> None:
    """The two publication lists (all OpenAlex fields), venues and indicators."""
    config = _load(config_path, need_roster=False)
    if offline:
        pipeline.lists_step(config, None, typer.echo)
    else:
        _with_client(config, lambda c: pipeline.lists_step(config, c, typer.echo))


@app.command()
def run(config_path: Path = ConfigOption) -> None:
    """All steps in order. Safe to re-run: finished steps are skipped."""
    config = _load(config_path)

    def steps(client: OpenAlexClient) -> None:
        typer.echo("== 1/5 institution works")
        typer.echo(pipeline.harvest_institution_step(config, client, progress=typer.echo))
        typer.echo("== 2/5 matching people")
        _, consolidated = pipeline.match_step(config, client, progress=typer.echo)
        typer.echo(consolidated.match_status.value_counts().to_string())
        typer.echo("== 3/5 people's works")
        typer.echo(pipeline.harvest_people_step(config, client, progress=typer.echo))

    _with_client(config, steps)
    typer.echo("== 4/5 tables")
    pipeline.tables_step(config, typer.echo)
    typer.echo("== 5/5 lists, venues and indicators")
    _with_client(config, lambda c: pipeline.lists_step(config, c, typer.echo))


if __name__ == "__main__":
    app()
