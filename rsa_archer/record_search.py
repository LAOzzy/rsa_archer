"""
Record search helpers for rsa_archer.

Provides RecordSearcher which implements:
- get_record_id_by_field(app_name, field_display_name, field_value) -> int | None
- get_record_ids_by_field_bulk(app_name, field_display_name, values) -> dict[str, int | None]

The implementation is REST-first (POST /api/core/content/record/search) with a Content API (OData)
fallback (GET /RSAarcher/{endpoint}?$filter=...).

This module is written to match the style in archer_instance.py and to use the existing
ArcherInstance surface (session header, api_url_base, content_api_url_base, helpers).
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Dict, List, Optional, Union

import requests

log = logging.getLogger(__name__)


class AmbiguousMatch(Exception):
    """Raised when a lookup value resolves to multiple record ids.

    Attributes:
        details: for single-value lookups a list[int], for bulk a dict[str, list[int]]
    """

    def __init__(self, message: str, details: Optional[Union[List[int], Dict[str, List[int]]]] = None):
        super().__init__(message)
        self.details = details


class RecordSearcher:
    """Search for record IDs by application name, field DisplayName and value.

    Usage:
        rs = RecordSearcher(archer_instance)
        rid = rs.get_record_id_by_field("App", "Ticket #", "INC-123")
    """

    def __init__(self, archer_instance):
        self.archer = archer_instance
        self.header = self.archer.header

    def _resolve_field_id_by_display_name(self, app_name: str, field_display_name: str) -> int:
        """Resolve a field DisplayName to its internal field id (case-insensitive)."""
        # Ensure application metadata is loaded
        self.archer.from_application(app_name)
        af = self.archer.application_fields_json

        target_lower = field_display_name.lower()
        for key, val in af.items():
            # application_fields_json stores name -> id entries where keys are strings
            if isinstance(key, str) and key.lower() == target_lower:
                return val

        raise ValueError(
            f'Field with DisplayName "{field_display_name}" not found in application "{app_name}"'
        )

    def _is_values_list(self, field_id: int) -> bool:
        """Return True if given field id is a Values List (type == 4 in this codebase)."""
        info = self.archer.application_fields_json.get(field_id)
        if isinstance(info, dict):
            try:
                return int(info.get("Type", -1)) == 4
            except Exception:
                return False
        return False

    def _get_value_or_value_id(
        self, field_display_name: str, field_id: int, field_value: str
    ) -> Union[str, int]:
        """Return appropriate search token: either raw string or internal value id for values lists."""
        if self._is_values_list(field_id):
            # existing helper returns a list of ids for a matched value
            ids = self.archer.get_value_id_by_field_name_and_value(field_display_name, field_value)
            if not ids:
                # No matching value in the values list
                return "__CLINE_VALUES_LIST_NO_MATCH__"
            return ids[0]
        else:
            return field_value

    def _supports_rest_search(self, module_id: int) -> bool:
        """Probe the REST content record search endpoint to see if it's available.

        Returns True if the endpoint looks supported, False if 404/405 or request fails.
        """
        api_url = f"{self.archer.api_url_base}core/content/record/search"
        headers = dict(self.header)
        headers["Content-type"] = "application/json"

        probe_body = {
            "ModuleId": module_id,
            "Page": {"Start": 0, "Size": 1},
            "Filters": [{"FieldId": 0, "Operator": "Equals", "Value": "__CLINE_PROBE__"}],
        }
        try:
            resp = requests.post(api_url, headers=headers, json=probe_body, verify=False, timeout=10)
            if resp.status_code in (404, 405):
                return False
            # Any other response code with JSON likely indicates the endpoint exists (even if filter is invalid)
            return True
        except Exception:
            return False

    def _rest_search_record_ids(
        self, module_id: int, field_id: int, value: Union[str, int], limit: int = 2
    ) -> List[int]:
        """Search using the REST content record search endpoint and return matching record ids.

        The request/response schema can vary between Archer versions; this implementation is defensive
        and parses common shapes.
        """
        api_url = f"{self.archer.api_url_base}core/content/record/search"
        headers = dict(self.header)
        headers["Content-type"] = "application/json"

        body = {
            "ModuleId": module_id,
            "Page": {"Start": 0, "Size": limit},
            "Filters": [{"FieldId": field_id, "Operator": "Equals", "Value": value}],
            "ReturnFields": ["Id"],
        }

        try:
            resp = requests.post(api_url, headers=headers, json=body, verify=False, timeout=15)
            if resp.status_code >= 400:
                # Treat error responses as no results for safety
                log.debug("REST search returned status %s: %s", resp.status_code, resp.text)
                return []

            data = resp.json()

            ids: List[int] = []

            # Common response shapes:
            # - list of objects, each with RequestedObject -> Id
            # - dict with 'value' list of objects, each with {<endpoint>_Id: id} or 'Id'
            if isinstance(data, list):
                for item in data:
                    rid = item.get("RequestedObject", {}).get("Id")
                    if rid:
                        ids.append(int(rid))
            elif isinstance(data, dict):
                # Case: {"value": [ {...}, ... ]}
                if "value" in data and isinstance(data["value"], list):
                    for item in data["value"]:
                        # try RequestedObject -> Id
                        rid = None
                        if isinstance(item, dict):
                            if "RequestedObject" in item:
                                rid = item["RequestedObject"].get("Id")
                            else:
                                # look for any key that endswith _Id or 'Id'
                                for k, v in item.items():
                                    if k.lower().endswith("_id") or k.lower() == "id":
                                        rid = v
                                        break
                        if rid:
                            ids.append(int(rid))
                else:
                    # Unexpected dict shape: try RequestedObject at top-level
                    rid = data.get("RequestedObject", {}).get("Id")
                    if rid:
                        ids.append(int(rid))
            else:
                log.debug("REST search returned unrecognized JSON shape: %s", type(data))

            return ids

        except Exception as e:
            log.error("REST search failed: %s", e)
            return []

    def _get_grc_endpoint_url(self, app_name: str) -> Optional[str]:
        """Discover the content API endpoint url for an application (non-printing)."""
        try:
            api_url = self.archer.content_api_url_base
            resp = requests.get(api_url, headers=self.header, verify=False, timeout=15)
            data = resp.json()
            # Expect data["value"] as a list of endpoints with 'name' and 'url'
            candidates = []
            for ep in data.get("value", []):
                name = ep.get("name", "") or ""
                url = ep.get("url")
                if name == app_name:
                    return url
                if app_name in name:
                    candidates.append(url)
            # prefer any candidate that contains the name
            if candidates:
                return candidates[0]
        except Exception as e:
            log.debug("Content API endpoint discovery failed: %s", e)
        return None

    def _contentapi_search_record_ids(
        self, endpoint_url: str, field_display_name: str, field_value: str
    ) -> List[int]:
        """Query the Content API OData endpoint for records matching field_display_name eq field_value."""
        # Build filter and select
        filter_expr = f"{field_display_name} eq '{field_value}'"
        qs_filter = urllib.parse.quote_plus(filter_expr)
        select_field = f"{endpoint_url}_Id"
        api_url = f"{self.archer.content_api_url_base}{endpoint_url}?$filter={qs_filter}&$select={select_field}"

        try:
            resp = requests.get(api_url, headers=self.header, verify=False, timeout=15)
            if resp.status_code >= 400:
                log.debug("Content API search returned %s: %s", resp.status_code, resp.text)
                return []

            data = resp.json()
            ids: List[int] = []

            for item in data.get("value", []):
                # Try property named like {endpoint_url}_Id first
                rid = None
                if isinstance(item, dict):
                    rid = item.get(select_field)
                    if rid is None:
                        # fallback to any key ending in _Id or 'Id'
                        for k, v in item.items():
                            if k.lower().endswith("_id") or k.lower() == "id":
                                rid = v
                                break
                if rid is not None:
                    ids.append(int(rid))

            return ids
        except Exception as e:
            log.error("Content API search failed: %s", e)
            return []

    def get_record_id_by_field(
        self, app_name: str, field_display_name: str, field_value: str
    ) -> Optional[int]:
        """Return a single matching record id or None. Raise AmbiguousMatch if multiple found."""
        # Resolve metadata
        field_id = self._resolve_field_id_by_display_name(app_name, field_display_name)

        module_id_raw = self.archer.application_level_id
        try:
            module_id = int(module_id_raw)
        except Exception:
            # Fallback: try to coerce or raise
            try:
                module_id = int(str(module_id_raw))
            except Exception:
                raise RuntimeError(f"Invalid application level id: {module_id_raw}")

        # Prepare search value (value or internal id for values list)
        search_token = self._get_value_or_value_id(field_display_name, field_id, field_value)

        # Try REST search first
        if self._supports_rest_search(module_id):
            ids = self._rest_search_record_ids(module_id, field_id, search_token, limit=2)
        else:
            # Fallback to Content API
            endpoint = self._get_grc_endpoint_url(app_name)
            if not endpoint:
                # No endpoint discovered; return no results
                return None
            ids = self._contentapi_search_record_ids(endpoint, field_display_name, field_value)

        if not ids:
            return None
        if len(ids) == 1:
            return int(ids[0])
        # multiple results -> ambiguous
        raise AmbiguousMatch(
            f"Multiple records found for value '{field_value}' in field '{field_display_name}'", details=ids
        )

    def get_record_ids_by_field_bulk(
        self, app_name: str, field_display_name: str, values: List[str]
    ) -> Dict[str, Optional[int]]:
        """Bulk lookup: return mapping value -> record id | None. If any values are ambiguous, raise AmbiguousMatch at end."""
        results: Dict[str, Optional[int]] = {}
        ambiguities: Dict[str, List[int]] = {}

        for v in values:
            try:
                rid = self.get_record_id_by_field(app_name, field_display_name, v)
                results[v] = rid
            except AmbiguousMatch as a:
                # Normalize details into a list[int] for storage in ambiguities[v]
                details = a.details if a.details else []
                if isinstance(details, list):
                    coerced: List[int] = []
                    for x in details:
                        try:
                            coerced.append(int(x))
                        except Exception:
                            continue
                    ambiguities[v] = coerced
                elif isinstance(details, dict):
                    flat: List[int] = []
                    for val in details.values():
                        if isinstance(val, list):
                            for x in val:
                                try:
                                    flat.append(int(x))
                                except Exception:
                                    continue
                        else:
                            try:
                                flat.append(int(val))
                            except Exception:
                                continue
                    ambiguities[v] = flat
                else:
                    try:
                        ambiguities[v] = [int(details)]
                    except Exception:
                        ambiguities[v] = []
                results[v] = None
            except Exception as e:
                # For other errors, log and map to None to keep bulk operation tolerant
                log.debug("Lookup for value %s raised error: %s", v, e)
                results[v] = None

        if ambiguities:
            raise AmbiguousMatch("Ambiguous matches found for one or more input values", details=ambiguities)

        return results
