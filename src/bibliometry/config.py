"""Run configuration: one ``config.yaml`` that every script and notebook reads.

The setup notebook writes it from a form; it can also be edited by hand (start from
``config.example.yaml``). The OpenAlex API key is a secret and is never written to
``config.yaml``: it lives in the ``OPENALEX_API_KEY`` environment variable or in a
``.env`` file next to the config, both of which are git-ignored.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path("config.yaml")
ENV_FILE = ".env"
API_KEY_VAR = "OPENALEX_API_KEY"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
INSTITUTION_ID_RE = re.compile(r"^I\d+$")


class ConfigError(ValueError):
    """The configuration is incomplete or inconsistent. The message says what to fix."""


@dataclass
class InstitutionSettings:
    id: str = ""
    name: str = ""
    ror: str = ""
    #: Regex over raw affiliation strings, for authorships where OpenAlex did not
    #: resolve the institution. Built automatically from OpenAlex's name variants.
    affiliation_pattern: str = ""


@dataclass
class RosterSettings:
    #: Local path or URL (http(s), including a Google Sheets share link) of a CSV/XLSX.
    path: str = ""
    #: Sheet names to read from a workbook; empty means all sheets.
    sheets: list[str] = field(default_factory=list)
    #: Manual column mapping {canonical name: header in the file}, overriding detection.
    #: Canonical names: last_name, first_name, full_name, orcid, openalex_id, lab, email.
    columns: dict[str, str] = field(default_factory=dict)
    #: Lab names to unify, {name in the file: name to use}, e.g. {"Old Lab Name": "New Lab Name"}.
    lab_aliases: dict[str, str] = field(default_factory=dict)
    #: Single-person mode: a name ("First Last" or "Last, First") instead of a file.
    person: str = ""
    #: Optional ORCID of that person, for a reliable match.
    person_orcid: str = ""


@dataclass
class Config:
    mailto: str = ""
    institution: InstitutionSettings = field(default_factory=InstitutionSettings)
    roster: RosterSettings = field(default_factory=RosterSettings)
    from_date: str = ""
    to_date: str = ""
    data_dir: str = "data"
    #: Where venue records and the world journal reference are cached; shared between
    #: runs (e.g. one per person). Empty = ``<data_dir>/raw``.
    cache_dir: str = ""
    #: Directory the config file lives in; relative paths resolve against it.
    root: Path = field(default=Path("."), repr=False, compare=False)

    # ------------------------------------------------------------------ paths
    @property
    def data_path(self) -> Path:
        return (self.root / self.data_dir).resolve()

    @property
    def raw_dir(self) -> Path:
        return self.data_path / "raw"

    @property
    def review_dir(self) -> Path:
        return self.data_path / "review"

    @property
    def tables_dir(self) -> Path:
        return self.data_path / "tables"

    @property
    def exports_dir(self) -> Path:
        return self.data_path / "exports"

    @property
    def cache_path(self) -> Path:
        return (self.root / self.cache_dir).resolve() if self.cache_dir else self.raw_dir

    @property
    def institution_store(self) -> Path:
        return self.raw_dir / "institution_works.jsonl.gz"

    @property
    def people_store(self) -> Path:
        return self.raw_dir / "people_works.jsonl.gz"

    @property
    def manifest_path(self) -> Path:
        return self.raw_dir / "manifest.json"

    @property
    def roster_table(self) -> Path:
        return self.review_dir / "roster.csv"

    @property
    def matches_review(self) -> Path:
        return self.review_dir / "author_matches.csv"

    @property
    def single_person(self) -> bool:
        """True when the config names one person instead of a roster file."""
        return bool(self.roster.person.strip())

    def roster_source(self) -> str:
        """Roster location, resolved against the config directory when it is a local path."""
        path = self.roster.path.strip()
        if not path or re.match(r"^https?://", path):
            return path
        candidate = Path(path).expanduser()
        return str(candidate if candidate.is_absolute() else (self.root / candidate).resolve())

    # ------------------------------------------------------------- validation
    def validate(self, need_roster: bool = True) -> None:
        """Raise :class:`ConfigError` listing every problem at once."""
        problems: list[str] = []
        if not EMAIL_RE.match(self.mailto or ""):
            problems.append("mailto: enter a valid email address (OpenAlex polite pool).")
        if not INSTITUTION_ID_RE.match(self.institution.id or ""):
            problems.append(
                "institution.id: must be an OpenAlex institution id (I followed by digits) "
                "(use the institution search in notebook 00)."
            )
        try:
            start = date.fromisoformat(self.from_date)
            end = date.fromisoformat(self.to_date)
            if start > end:
                problems.append("from_date is after to_date.")
        except (TypeError, ValueError):
            problems.append("from_date / to_date: use the YYYY-MM-DD format.")
        if need_roster and not self.single_person:
            source = self.roster_source()
            if not source:
                problems.append("roster.path: give the path or URL of the CSV/XLSX list of people.")
            elif not re.match(r"^https?://", source) and not Path(source).exists():
                problems.append(f"roster.path: file not found: {source}")
        if problems:
            raise ConfigError("Fix config.yaml:\n  - " + "\n  - ".join(problems))

    # ------------------------------------------------------------------- I/O
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("root")
        return data

    def save(self, path: Path | str = DEFAULT_CONFIG) -> Path:
        path = Path(path)
        header = (
            "# bibliometry run configuration — written by notebooks/00_setup.ipynb.\n"
            "# Safe to edit by hand. The OpenAlex API key is NOT stored here (see .env).\n"
        )
        path.write_text(
            header + yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG) -> Config:
        path = Path(path)
        if not path.exists():
            raise ConfigError(
                f"{path} not found. Run notebooks/00_setup.ipynb first, or copy "
                "config.example.yaml to config.yaml and fill it in."
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw, root=path.resolve().parent)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], root: Path = Path(".")) -> Config:
        known = {"mailto", "institution", "roster", "from_date", "to_date", "data_dir", "cache_dir"}
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(f"Unknown key(s) in config: {', '.join(sorted(unknown))}")
        roster = dict(raw.get("roster") or {})
        roster["sheets"] = list(roster.get("sheets") or [])
        roster["columns"] = dict(roster.get("columns") or {})
        roster["lab_aliases"] = {
            str(k): str(v) for k, v in (roster.get("lab_aliases") or {}).items()
        }
        return cls(
            mailto=str(raw.get("mailto") or "").strip(),
            institution=InstitutionSettings(**(raw.get("institution") or {})),
            roster=RosterSettings(**roster),
            from_date=str(raw.get("from_date") or ""),
            to_date=str(raw.get("to_date") or ""),
            data_dir=str(raw.get("data_dir") or "data"),
            cache_dir=str(raw.get("cache_dir") or ""),
            root=root,
        )

    # ---------------------------------------------------------------- secrets
    def api_key(self) -> str:
        """The OpenAlex API key from the environment or ``.env``; empty if none."""
        return load_api_key(self.root)


def _colab_secret() -> str:
    """``OPENALEX_API_KEY`` from Colab's Secrets panel (the key icon), if available."""
    if "google.colab" not in sys.modules:
        return ""
    try:
        from google.colab import userdata  # type: ignore[import-not-found]

        return (userdata.get(API_KEY_VAR) or "").strip()
    except Exception:  # noqa: BLE001 - secret missing or notebook access not granted
        return ""


def load_api_key(root: Path = Path(".")) -> str:
    """The OpenAlex API key: environment, then Colab Secrets, then ``.env``.

    ``.env`` is looked for in ``root`` and up to three parent folders, so a run in a
    sub-folder (single-person mode: ``people/<name>/``) finds the project's key.
    """
    value = os.environ.get(API_KEY_VAR, "").strip() or _colab_secret()
    if value:
        return value
    folder = Path(root).resolve()
    for candidate in [folder, *folder.parents[:3]]:
        env_file = candidate / ENV_FILE
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                key, _, val = line.partition("=")
                if key.strip() == API_KEY_VAR:
                    return val.strip().strip("'\"")
    return ""


def save_api_key(key: str, root: Path = Path(".")) -> Path:
    """Write (or replace) ``OPENALEX_API_KEY`` in ``root/.env``, keeping other lines."""
    env_file = Path(root) / ENV_FILE
    lines = []
    if env_file.exists():
        lines = [
            line
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if line.partition("=")[0].strip() != API_KEY_VAR
        ]
    lines.append(f"{API_KEY_VAR}={key.strip()}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        env_file.chmod(0o600)
    except OSError:
        pass
    return env_file
