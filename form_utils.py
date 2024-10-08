from collections.abc import Iterable
from typing import Any

from schemas import get_all_required_reports, get_schema


def load_report_form_schema(
    country: str, reports: Iterable[str]
) -> list[dict[str, Any]]:
    """Load reports schemas from local definitions to be used in the creation of
    the precandidate form schema.

    Parameters
    ----------
    country:
        Country code
    reports:
        Iterable containing the names of the reports to be filled by the precandidate

    Returns
    -------
        Schema defining the reports of the precandidate form
    """
    schema = get_schema(country)
    schema_reports = schema.get("reports", {})
    if not schema_reports:
        return []

    required_reports = get_all_required_reports(schema_reports, reports)
    report_schemas = []
    for report_name in required_reports:
        # Important to copy to prevent mutations of the schema
        report = schema_reports.get(report_name, {}).copy()
        if not report:
            continue
        report["name"] = report_name
        report_schemas.append(report)

    return report_schemas


def get_precandidate_form_schema(
    country: str,
    source_reports: Iterable[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the precandidate form schema which will be used for rendering the UI.

    Parameters
    ----------
    country:
        Country code
    source_reports:
        Iterable containing the names of the reports to be filled by the precandidate

    Returns
    -------
    tuple[list[dict[str, Any]], list[dict[str, Any]]]
        Fields and groups schemas used for rendering the form in the UI
    """
    schema = get_schema(country)
    properties = schema["properties"]

    fields: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    reports = load_report_form_schema(country, source_reports)

    for report in reports:
        required = report.get("required", [])
        optional = report.get("optional", [])

        for field_name in [*required, *optional]:
            # Important to copy to prevent mutations of the schema
            field = properties[field_name].copy()
            field["name"] = field_name
            if field not in fields:
                fields.append(field)

        groups.append(
            {
                "name": report["name"],
                "rules": report.get("rules", []),
                "required": required,
                "optional": optional,
            }
        )

    return fields, groups


def build_precandidate_schema(incoming_data: dict[str, Any]) -> dict[str, Any]:
    """Build precandidate's form schema.

    Parameters
    ----------
    incoming_data : dict[str, Any]
        Incoming data from the precandidate creation request

    Returns
    -------
    dict[str, Any]
        Schema defining the precandidate form
    """
    schema = incoming_data.pop("schema")
    schema["version"] = 2
    fields, groups = get_precandidate_form_schema(
        schema["country"],
        schema["reports"],
    )
    schema["fields"] = fields
    schema["groups"] = groups

    return schema
