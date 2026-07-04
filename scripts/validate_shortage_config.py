#!/usr/bin/env python3
"""Validate drug shortage monitoring configuration files.

Checks:
- therapeutic_categories.json required fields and schema constraints
- openfda_shortages.json required fields
- Cross-references disease keys against config/diseases/ directory

Usage:
    python scripts/validate_shortage_config.py
"""
import json
import os
import re
import sys
from pathlib import Path


def find_project_root() -> Path:
    """Locate the project root by walking up from the script location."""
    script_dir = Path(__file__).resolve().parent
    # Script lives in <root>/scripts/
    return script_dir.parent


def load_json(filepath: Path) -> dict:
    """Load and parse a JSON file."""
    with open(filepath, "r") as f:
        return json.load(f)


def validate_therapeutic_categories(root: Path) -> list[str]:
    """Validate config/shortage_monitoring/therapeutic_categories.json."""
    errors = []
    filepath = root / "config" / "shortage_monitoring" / "therapeutic_categories.json"

    if not filepath.exists():
        errors.append(f"MISSING: {filepath}")
        return errors

    try:
        data = load_json(filepath)
    except json.JSONDecodeError as e:
        errors.append(f"INVALID JSON in {filepath}: {e}")
        return errors

    categories = data.get("categories", [])
    if not categories:
        errors.append("therapeutic_categories.json: 'categories' array is empty or missing")
        return errors

    required_fields = [
        "category_key",
        "display_name",
        "priority_level",
        "relevant_diseases",
        "fda_classification_mapping",
    ]
    valid_priority_levels = {"HIGH", "MEDIUM", "LOW"}
    category_key_pattern = re.compile(r"^[a-z][a-z0-9_]*$")

    # Gather available disease keys from config/diseases/ directory
    diseases_dir = root / "config" / "diseases"
    available_diseases = set()
    if diseases_dir.exists():
        for f in diseases_dir.iterdir():
            if f.is_file() and f.suffix == ".json" and not f.name.startswith("_"):
                available_diseases.add(f.stem)

    for idx, category in enumerate(categories):
        prefix = f"categories[{idx}]"

        # Check required fields
        for field in required_fields:
            if field not in category:
                errors.append(f"{prefix}: missing required field '{field}'")

        # Validate category_key pattern
        key = category.get("category_key", "")
        if key and not category_key_pattern.match(key):
            errors.append(
                f"{prefix}: category_key '{key}' does not match pattern ^[a-z][a-z0-9_]*$"
            )

        # Validate priority_level
        priority = category.get("priority_level", "")
        if priority and priority not in valid_priority_levels:
            errors.append(
                f"{prefix}: priority_level '{priority}' must be one of HIGH/MEDIUM/LOW"
            )

        # Validate relevant_diseases reference existing disease configs
        diseases = category.get("relevant_diseases", [])
        if not isinstance(diseases, list):
            errors.append(f"{prefix}: relevant_diseases must be an array")
        else:
            for disease_key in diseases:
                if disease_key not in available_diseases:
                    errors.append(
                        f"{prefix}: disease_key '{disease_key}' not found in config/diseases/"
                    )

        # Validate fda_classification_mapping is a non-empty list
        mapping = category.get("fda_classification_mapping", [])
        if not isinstance(mapping, list) or len(mapping) == 0:
            errors.append(f"{prefix}: fda_classification_mapping must be a non-empty array")

    return errors


def validate_openfda_config(root: Path) -> list[str]:
    """Validate config/data_sources/openfda_shortages.json."""
    errors = []
    filepath = root / "config" / "data_sources" / "openfda_shortages.json"

    if not filepath.exists():
        errors.append(f"MISSING: {filepath}")
        return errors

    try:
        data = load_json(filepath)
    except json.JSONDecodeError as e:
        errors.append(f"INVALID JSON in {filepath}: {e}")
        return errors

    # Check top-level required fields
    required_top = ["source_name", "display_name", "api", "s3_storage"]
    for field in required_top:
        if field not in data:
            errors.append(f"openfda_shortages.json: missing required field '{field}'")

    # Check api section required fields
    api = data.get("api", {})
    if not isinstance(api, dict):
        errors.append("openfda_shortages.json: 'api' must be an object")
    else:
        if "base_url" not in api:
            errors.append("openfda_shortages.json: api.base_url is required")
        if "timeout_seconds" not in api:
            errors.append("openfda_shortages.json: api.timeout_seconds is required")
        else:
            timeout = api.get("timeout_seconds")
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                errors.append(
                    f"openfda_shortages.json: api.timeout_seconds must be a positive number, got '{timeout}'"
                )

    return errors


def main() -> int:
    root = find_project_root()
    all_errors = []

    print("=" * 60)
    print("Drug Shortage Configuration Validation")
    print("=" * 60)

    # Validate therapeutic categories
    print("\n[1/2] Validating therapeutic_categories.json ...")
    tc_errors = validate_therapeutic_categories(root)
    all_errors.extend(tc_errors)
    if tc_errors:
        for err in tc_errors:
            print(f"  FAIL: {err}")
    else:
        print("  OK")

    # Validate openFDA config
    print("\n[2/2] Validating openfda_shortages.json ...")
    fda_errors = validate_openfda_config(root)
    all_errors.extend(fda_errors)
    if fda_errors:
        for err in fda_errors:
            print(f"  FAIL: {err}")
    else:
        print("  OK")

    # Summary
    print("\n" + "=" * 60)
    if all_errors:
        print(f"FAIL: {len(all_errors)} error(s) found")
        return 1
    else:
        print("OK: All configuration files are valid")
        return 0


if __name__ == "__main__":
    sys.exit(main())
