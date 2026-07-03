"""Geographic Affinity — Config-driven metro→county mapping.

Reads county→metro affinity weights from state configs.
No hardcoded values — all mappings come from config/states/*.json.
"""
import json
import os
import sys
import logging
from typing import Any

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared.config_loader import (
    get_system_config,
    get_subscribing_counties,
    get_all_sentinel_metros,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

system = get_system_config()
COUNTY_CONFIG_TABLE = os.environ.get(
    "COUNTY_CONFIG_TABLE", system["dynamodb_tables"]["county_configs"]
)

dynamodb = boto3.resource("dynamodb")
county_table = dynamodb.Table(COUNTY_CONFIG_TABLE)


def lambda_handler(event: dict, context: Any) -> dict:
    """Map a leader detection to affected rural counties.

    Input event (from leader_detection):
    {
        "disease": "influenza",
        "season": "2026-27",
        "leader": {"msa_code": "26420", "value": 3.2, "metro_name": "Houston", ...},
        "week": "202645"
    }

    Returns:
        List of affected counties with their affinity weights.
    """
    leader_msa = event.get("leader", {}).get("msa_code")
    disease = event.get("disease")
    week = event.get("week")
    season = event.get("season")

    if not leader_msa:
        return {"affected_counties": [], "reason": "No leader MSA provided"}

    # Get all subscribing counties from config
    all_counties = get_subscribing_counties()
    all_metros = get_all_sentinel_metros()

    leader_info = all_metros.get(leader_msa, {})

    # Find counties with affinity to the leader metro
    affected_counties = []
    for county in all_counties:
        affinity_weights = county.get("affinity_weights", {})

        if leader_msa in affinity_weights:
            weight = affinity_weights[leader_msa]
            affected_counties.append({
                "county_fips": county["county_fips"],
                "county_name": county["county_name"],
                "population": county.get("population", 0),
                "state_key": county.get("_state_key", "unknown"),
                "affinity_weight": weight,
                "is_primary_affinity": county.get("primary_metro_affinity") == leader_msa,
                "leader_metro": {
                    "msa_code": leader_msa,
                    "name": leader_info.get("short_name", leader_msa),
                },
                "delivery_preferences": county.get("delivery_preferences", {}),
                "contacts": county.get("contacts", {}),
            })

    # Sort by affinity weight (strongest relationship first)
    affected_counties.sort(key=lambda x: x["affinity_weight"], reverse=True)

    logger.info(
        f"Leader {leader_info.get('short_name', leader_msa)} ({leader_msa}) "
        f"affects {len(affected_counties)} counties for {disease}"
    )

    return {
        "disease": disease,
        "week": week,
        "season": season,
        "leader_msa": leader_msa,
        "leader_name": leader_info.get("short_name", leader_msa),
        "affected_counties": affected_counties,
        "total_affected_population": sum(
            c.get("population", 0) for c in affected_counties
        ),
    }
