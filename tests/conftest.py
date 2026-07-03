"""
Root conftest — provides shared fixtures and a handler-loader utility
that loads a specific Lambda handler.py without polluting sys.path.
"""
import sys
import os
import importlib
import importlib.util
import types
import pytest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDAS = os.path.join(ROOT, "lambdas")
SHARED = os.path.join(LAMBDAS, "shared")


# ── shared path always available ────────────────────────────────────────────
if SHARED not in sys.path:
    sys.path.insert(0, SHARED)
if LAMBDAS not in sys.path:
    sys.path.insert(0, LAMBDAS)


# ── AWS env vars so boto3 never tries real credentials ───────────────────────
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("TOKEN_SECRET", "healthsignals-unit-test-secret-key-32ch")


def load_handler(handler_dir: str, extra_patches: dict = None):
    """
    Load lambdas/<handler_dir>/handler.py as an isolated module.

    Parameters
    ----------
    handler_dir : str
        Relative path from lambdas/ root, e.g. "ingestion/delphi_fetcher".
    extra_patches : dict
        Additional {dotted_name: mock_value} to patch *before* the module
        is imported (for module-level side-effects like boto3.resource calls).

    Returns
    -------
    module : types.ModuleType
    """
    handler_path = os.path.join(LAMBDAS, handler_dir, "handler.py")

    # Give the module a unique name so repeated loads don't collide
    module_name = "handler_" + handler_dir.replace("/", "_")

    # Remove stale cached version if present
    sys.modules.pop(module_name, None)
    sys.modules.pop("handler", None)

    # Make the handler's own directory first on path so its local imports work
    handler_full_dir = os.path.join(LAMBDAS, handler_dir)
    old_path = sys.path.copy()
    sys.path.insert(0, handler_full_dir)

    spec = importlib.util.spec_from_file_location(module_name, handler_path)
    module = types.ModuleType(module_name)
    module.__spec__ = spec
    module.__file__ = handler_path
    module.__package__ = module_name

    patches = extra_patches or {}
    active = []
    try:
        for target, value in patches.items():
            p = patch(target, value)
            p.start()
            active.append(p)
        spec.loader.exec_module(module)
    finally:
        for p in active:
            try:
                p.stop()
            except RuntimeError:
                pass
        sys.path = old_path

    # Register under "handler" so `from handler import X` in the module works
    sys.modules["handler"] = module
    sys.modules[module_name] = module
    return module


# ── Shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def sample_delphi_response():
    import json
    fixture_path = os.path.join(ROOT, "tests", "data", "sample_delphi_response.json")
    with open(fixture_path) as f:
        return json.load(f)


@pytest.fixture
def sample_county():
    return {
        "county_fips": "48143",
        "county_name": "Erath County",
        "population": 42698,
        "state_key": "texas",
        "affinity_weights": {"26420": 0.75, "19100": 0.25},
        "primary_metro_affinity": "26420",
        "contacts": {
            "health_officer": {
                "name": "Dr. Jane Smith",
                "email": "jane.smith@erathcounty.gov",
                "phone": "+12545551234",
            }
        },
        "delivery_preferences": {"channels": ["email"], "alert_threshold": "MODERATE"},
    }


@pytest.fixture
def sample_subscription():
    return {
        "county_fips": "48143",
        "subscription_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "county_name": "Erath County",
        "state": "texas",
        "contact_name": "Dr. Jane Smith",
        "contact_email": "jane.smith@erathcounty.gov",
        "contact_phone": "+15551234567",
        "diseases": ["influenza", "rsv", "covid"],
        "delivery_preferences": {"channels": ["email"], "alert_threshold": "MODERATE"},
        "status": "active",
        "created_at": "2026-01-15T10:00:00",
        "verified_at": "2026-01-15T11:00:00",
        "updated_at": "2026-01-15T11:00:00",
    }
