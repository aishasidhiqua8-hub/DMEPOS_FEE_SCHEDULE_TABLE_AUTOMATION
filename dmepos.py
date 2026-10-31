import argparse
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile, is_zipfile


CMS_PAGE_URL = (
    "https://www.cms.gov/medicare/payment/fee-schedules/dmepos/"
    "dmepos-fee-schedule"
)
DEFAULT_LAYOUT = {
    "year": datetime.now().year,
    "archive_link_regex": r"/DME(?P<year_suffix>\d{2})-(?P<quarter>[A-D])\.zip$",
    "quarter_order": ["A", "B", "C", "D"],
    "quarter_text_files": {
        "A": "DMEPOS{year_suffix}_JAN.txt",
        "B": "DMEPOS_APR.txt",
        "C": "DMEPOS_JUL.txt",
        "D": "DMEPOS_OCT.txt",
    },
    "record_delimiter": "~",
    "columns": {
        "PROCEDURE CD": 1,
        "STATE CD": 8,
        "PRICING YEAR": 0,
        "ALLOWANCE AMT": 12,
        "PROCD MOD CD1": 2,
        "PROCD MOD EFF DT1": None,
        "PROCD MOD CD2": 3,
        "PROCD MOD EFF DT2": None,
    },
    "key_columns": [
        "PROCEDURE CD",
        "STATE CD",
        "PRICING YEAR",
        "PROCD MOD CD1",
        "PROCD MOD CD2",
    ],
    "decimal_columns": ["ALLOWANCE AMT"],
}


class CmsLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() == "a":
            self.hrefs.extend(
                value
                for name, value in attrs
                if name.lower() == "href" and value is not None
            )


def discover_quarter_archives(
    page_url: str, layout: dict[str, object]
) -> dict[str, str]:
    request = Request(
        page_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; DMEPOS-fee-schedule/1.0)"},
    )
    with urlopen(request, timeout=60) as response:
        page = response.read().decode("utf-8", errors="replace")

    parser = CmsLinkParser()
    parser.feed(page)
    year = int(layout["year"])
    year_suffix = f"{year % 100:02d}"
    archive_pattern = re.compile(str(layout["archive_link_regex"]), re.IGNORECASE)
    quarter_order = layout["quarter_order"]
    if not isinstance(quarter_order, list) or not all(
        isinstance(quarter, str) for quarter in quarter_order
    ):
        raise ValueError("Layout 'quarter_order' must be a list of quarter labels.")
    archives: dict[str, str] = {}
    for href in parser.hrefs:
        url = urljoin(page_url, href)
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        page_host = (urlsplit(page_url).hostname or "").lower()
        if host != page_host and host != "cms.gov" and not host.endswith(".cms.gov"):
            continue
        match = archive_pattern.search(parts.path)
        if match is None:
            continue
        groups = match.groupdict()
        quarter = groups.get("quarter", "").upper()
        link_year_suffix = groups.get("year_suffix")
        link_year = groups.get("year")
        if link_year_suffix is not None and link_year_suffix != year_suffix:
            continue
        if link_year is not None and link_year != str(year):
            continue
        if quarter not in quarter_order:
            continue
        if quarter in archives and archives[quarter] != url:
            raise ValueError(
                f"CMS page lists multiple ZIP files for {year} quarter {quarter}."
            )
        archives[quarter] = url

    if not archives:
        raise ValueError(
            f"No DMEPOS quarterly ZIP links for {year} were found on {page_url}. "
            "Check the page or supply --zip-url-template."
        )
    return {
        quarter: archives[quarter] for quarter in quarter_order if quarter in archives
    }


def download_archive(url: str, archive_file: Path) -> None:
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; DMEPOS-fee-schedule/1.0)",
            "Accept": "application/zip, application/octet-stream",
        },
    )
    with tempfile.TemporaryDirectory(dir=archive_file.parent) as temp_dir:
        temporary_file = Path(temp_dir) / archive_file.name
        with urlopen(request, timeout=120) as response:
            with temporary_file.open("wb") as stream:
                shutil.copyfileobj(response, stream)

        if not is_zipfile(temporary_file):
            raise ValueError(f"CMS download is not a valid ZIP archive: {url}")
        try:
            with ZipFile(temporary_file) as archive:
                invalid_member = archive.testzip()
        except BadZipFile as error:
            raise ValueError(f"CMS download is a damaged ZIP archive: {url}") from error
        if invalid_member is not None:
            raise ValueError(
                f"CMS ZIP archive contains a damaged file "
                f"{invalid_member!r}: {url}"
            )
        temporary_file.replace(archive_file)


def extract_dmepos_text(
    archive_file: Path, destination_dir: Path, expected_name: str
) -> Path:
    try:
        with ZipFile(archive_file) as archive:
            matches = [
                member
                for member in archive.namelist()
                if Path(member).name.casefold() == expected_name.casefold()
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"{archive_file}: expected one {expected_name} text document, "
                    f"found {len(matches)}."
                )
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / expected_name
            with archive.open(matches[0]) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
    except BadZipFile as error:
        raise ValueError(f"Invalid ZIP archive: {archive_file}") from error
    return destination


def download_and_extract(
    source_dir: Path,
    page_url: str,
    layout: dict[str, object],
) -> dict[str, Path]:
    year = int(layout["year"])
    year_suffix = f"{year % 100:02d}"
    quarter_order = layout["quarter_order"]
    quarter_text_files = layout["quarter_text_files"]
    if not isinstance(quarter_order, list) or not isinstance(
        quarter_text_files, dict
    ):
        raise ValueError(
            "Layout must define 'quarter_order' and 'quarter_text_files'."
        )
    archives = discover_quarter_archives(page_url, layout)

    source_dir.mkdir(parents=True, exist_ok=True)
    extracted: dict[str, Path] = {}
    for quarter in quarter_order:
        if quarter not in archives:
            continue
        url = archives[quarter]
        text_template = quarter_text_files.get(quarter)
        if not isinstance(text_template, str):
            raise ValueError(
                f"Layout does not define a text filename for quarter {quarter}."
            )
        archive_name = Path(urlsplit(url).path).name
        if not archive_name:
            raise ValueError(f"Quarterly ZIP link has no filename: {url}")
        archive_file = source_dir / archive_name
        quarter_dir = source_dir / f"DME{year_suffix}-{quarter}"
        download_archive(url, archive_file)
        text_file = extract_dmepos_text(
            archive_file,
            quarter_dir,
            text_template.format(
                year=year, year_suffix=year_suffix, quarter=quarter
            ),
        )
        extracted[quarter] = text_file
    return extracted


def parse_record(
    line: str,
    delimiter: str,
    columns: dict[str, object],
    decimal_columns: set[str],
    line_number: int,
    source: Path,
) -> tuple[str, ...]:
    fields = line.rstrip("\r\n").split(delimiter)
    row: list[str] = []
    for column, field_index in columns.items():
        if field_index is None or field_index == "NA":
            row.append("NA")
            continue
        if not isinstance(field_index, int) or field_index < 0:
            raise ValueError(
                f"Layout field index for {column!r} must be a non-negative integer "
                "or null."
            )
        value = fields[field_index].strip() if field_index < len(fields) else ""
        if not value:
            row.append("NA")
            continue
        if column in decimal_columns:
            try:
                value = f"{Decimal(value):.2f}"
            except InvalidOperation as error:
                raise ValueError(
                    f"{source}: line {line_number}: invalid value for {column!r}: "
                    f"{value!r}."
                ) from error
        row.append(value)
    return tuple(row)


def merge_quarters(
    quarter_files: dict[str, Path], layout: dict[str, object]
) -> tuple[dict[tuple[str, ...], tuple[str, ...]], list[str]]:
    columns = layout["columns"]
    key_columns = layout["key_columns"]
    decimal_columns = set(layout.get("decimal_columns", []))
    delimiter = str(layout["record_delimiter"])
    quarter_order = layout["quarter_order"]
    if not isinstance(columns, dict) or not columns:
        raise ValueError("Layout 'columns' must be a non-empty object.")
    if not isinstance(key_columns, list) or not key_columns:
        raise ValueError("Layout 'key_columns' must be a non-empty list.")
    if not set(key_columns).issubset(columns):
        raise ValueError("Every key column must also be present in 'columns'.")
    if not isinstance(quarter_order, list):
        raise ValueError("Layout 'quarter_order' must be a list.")

    records: dict[tuple[str, ...], tuple[str, ...]] = {}
    summary: list[str] = []

    for quarter in quarter_order:
        source = quarter_files.get(quarter)
        if source is None:
            continue
        added = 0
        updated = 0
        with source.open("r", encoding="utf-8-sig", newline="") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip() or line.strip() == "\x1a":
                    continue
                row = parse_record(
                    line,
                    delimiter,
                    columns,
                    decimal_columns,
                    line_number,
                    source,
                )
                values = dict(zip(columns, row))
                key = tuple(values[column] for column in key_columns)
                if key in records:
                    updated += 1
                else:
                    added += 1
                records[key] = row

        summary.append(f"Quarter {quarter}: {added:,} new, {updated:,} updated")

    if not records:
        raise ValueError("No records were found in the downloaded quarterly files.")
    return records, summary


def sorted_rows(
    records: dict[tuple[str, ...], tuple[str, ...]],
    columns: dict[str, object],
    key_columns: list[str],
) -> list[tuple[str, ...]]:
    indexes = [list(columns).index(column) for column in key_columns]
    return sorted(
        records.values(),
        key=lambda row: tuple(row[index] for index in indexes),
    )


def write_csv(
    rows: list[tuple[str, ...]], columns: list[str], output_file: Path
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)


def write_html(
    rows: list[tuple[str, ...]],
    columns: list[str],
    output_file: Path,
    title: str,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8", newline="") as stream:
        stream.write(
            "<!doctype html>\n"
            '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f"<title>{escape(title)}</title>\n"
            "<style>\n"
            "body { font-family: Arial, sans-serif; margin: 1rem; }\n"
            "table { border-collapse: collapse; }\n"
            "th, td { border: 1px solid #555; padding: 5px 8px; "
            "text-align: left; white-space: nowrap; }\n"
            "th { background: #e8edf3; position: sticky; top: 0; }\n"
            "tbody tr:nth-child(even) { background: #f5f7f9; }\n"
            "</style>\n</head>\n<body>\n"
            f"<h1>{escape(title)}</h1>\n"
            "<table>\n<thead>\n<tr>"
        )
        for column in columns:
            stream.write(f"<th>{escape(column)}</th>")
        stream.write("</tr>\n</thead>\n<tbody>\n")
        for row in rows:
            stream.write("<tr>")
            for value in row:
                stream.write(f"<td>{escape(value)}</td>")
            stream.write("</tr>\n")
        stream.write(
            "</tbody>\n</table>\n"
            f"<p>Total records: {len(rows):,}</p>\n"
            "</body>\n</html>\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Download quarterly ZIP files from a web page, extract text records, "
            "merge updated records, and write an HTML and/or CSV table."
        )
    )
    parser.add_argument(
        "--url",
        default=CMS_PAGE_URL,
        help="Web page containing links to the quarterly ZIP files",
    )
    parser.add_argument(
        "--layout",
        type=Path,
        help="JSON file describing quarterly filenames, record fields, and table columns",
    )
    parser.add_argument(
        "--year",
        type=int,
        help="Year to process (overrides the year in the layout)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("DMEPOS_downloads"),
        help="Folder for downloaded ZIPs, extracted text files, and table output",
    )
    parser.add_argument(
        "--format",
        choices=("csv", "html", "both"),
        default="both",
        help="Output table format (default: both)",
    )
    args = parser.parse_args()

    layout = dict(DEFAULT_LAYOUT)
    if args.layout is not None:
        with args.layout.open("r", encoding="utf-8") as stream:
            user_layout = json.load(stream)
        if not isinstance(user_layout, dict):
            raise ValueError("The layout JSON must contain a JSON object.")
        layout.update(user_layout)
    if args.year is not None:
        layout["year"] = args.year

    required = (
        "year",
        "archive_link_regex",
        "quarter_order",
        "quarter_text_files",
        "record_delimiter",
        "columns",
        "key_columns",
    )
    missing = [key for key in required if key not in layout]
    if missing:
        raise ValueError(f"Layout is missing required settings: {', '.join(missing)}")
    if not re.search(
        r"\(\?P<(?:quarter|year|year_suffix)>",
        str(layout["archive_link_regex"]),
    ):
        raise ValueError(
            "The archive_link_regex must use a named 'quarter' group and a "
            "named 'year' or 'year_suffix' group."
        )

    work_dir = args.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    quarter_files = download_and_extract(work_dir, args.url, layout)
    records, summary = merge_quarters(quarter_files, layout)
    columns = list(layout["columns"])
    rows = sorted_rows(records, layout["columns"], layout["key_columns"])
    title = str(
        layout.get("table_title", f"DMEPOS {layout['year']} Yearly Fee Schedule")
    )
    output_base = work_dir / f"DMEPOS_{layout['year']}_yearly_table"

    if args.format in ("csv", "both"):
        write_csv(rows, columns, output_base.with_suffix(".csv"))
    if args.format in ("html", "both"):
        write_html(rows, columns, output_base.with_suffix(".html"), title)

    print("\n".join(summary))
    print(f"Final table rows: {len(rows):,}")
    print(f"Output folder: {work_dir.resolve()}")


if __name__ == "__main__":
    main()
