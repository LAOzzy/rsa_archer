# Change Proposal — Record ID Lookup by Application Name, Field DisplayName, and Field Value

Date: 2025-11-06
Repository: rsa_archer

Summary
-------
Add functionality to look up an Archer record's internal system ID given:
- Archer application name
- Field DisplayName (how the field appears in Archer UI)
- Field value (user-visible value)

Provide both:
- A single-value convenience method returning the record ID or None
- A bulk method accepting many values and returning a mapping value → record ID | None

Key design decisions
--------------------
- Field matching: Use DisplayName. Matching will be case-insensitive by default when resolving the field to a field id.
- No match behavior: For single lookups return None. For bulk lookups map each input to None when not found.
- Duplicate matches: If a value resolves to multiple records:
  - Single lookup: raise AmbiguousMatch(value, ids)
  - Bulk lookup: collect all ambiguous values and raise an AmbiguousMatch summarizing all conflicts (caller can change this behavior later if they prefer per-value sentinel values)
- Values List fields: When the field is a values list, the code will translate the user-facing value to the internal value id and search by that id.

Search strategy (runtime)
-------------------------
1. Preferred: use Archer REST "content record search" endpoint (POST /api/core/content/record/search).
   - Advantages: designed for fast content queries and supports searching by fieldId/valueId.
   - Approach: probe the endpoint at runtime; if available, use it for lookups.
2. Fallback: use Content API (OData) endpoints already present in the repo (GET {contentapi}/{endpoint}?$filter=...) when REST search is unavailable.
   - The repo already contains helpers for discovering contentapi endpoints; extend with a non-printing endpoint-discovery helper.

High-level workflow for a single lookup
--------------------------------------
1. Resolve application by name (existing from_application method) to set application_level_id and application fields cache.
2. Resolve field DisplayName → field id (case-insensitive lookup against application_fields_json).
3. Detect endpoint support:
   - If REST search supported:
     - If field is Values List: resolve value → valueId using get_value_id_by_field_name_and_value
     - POST a search with fieldId/operator/Value (or valueId), Page Size = 2
     - 0 results → return None
     - 1 result → return found record Id
     - ≥2 results → raise AmbiguousMatch
   - Else (fallback to Content API):
     - Find contentapi endpoint for the application
     - Issue an OData GET with $filter on the DisplayName eq 'value' and $select the endpoint id field
     - Interpret results as above

Bulk lookup considerations
--------------------------
- For REST: if the server supports combining filters, use disjunctions where possible; otherwise iterate across values with a small concurrency limit. Always stop early for a value if it returns >1 match and mark as ambiguous.
- For Content API: build $filter clauses joined with OR up to URL length limits; chunk as needed and merge responses. Map each input value to its record ID or None; collect ambiguities.

New public API (suggested method signatures)
--------------------------------------------
- get_record_id_by_field(app_name: str, field_display_name: str, field_value: str) -> Optional[int]
- get_record_ids_by_field_bulk(app_name: str, field_display_name: str, values: list[str]) -> Dict[str, Optional[int]]

Internal helpers to add
-----------------------
- _resolve_field_id_by_display_name(field_display_name: str) -> int
  - Case-insensitive; raises ValueError when not found
- supports_rest_search() -> bool
  - Harmless probe to POST /api/core/content/record/search; returns True if supported
- _rest_search_record_ids(module_id:int, field_id:int, value: str|int) -> List[int]
- _contentapi_search_record_ids(endpoint_url: str, display_field: str, value: str) -> List[int]
- get_grc_endpoint_url(app_name: str) -> str
  - Non-printing variant of existing find_grc_endpoint_url that returns the canonical endpoint url string
- AmbiguousMatch exception class to encapsulate ambiguous results with details

Endpoints used
--------------
- GET /api/core/system/application — resolve application Name → Id
- GET /api/core/system/fielddefinition/application/{moduleId}?$filter=IsActive eq true — populate application_fields_json and identify field types (Values List, Subform, etc.)
- POST /api/core/content/record/search — preferred fast search (subject to availability)
- GET /RSAarcher/contentapi — discover content api endpoints (existing code references)
- GET /RSAarcher/{endpoint_url}?$filter=... — Content API OData fallback

Values List handling
--------------------
- If the field type indicates Values List (type == 4 in the current codebase), call existing helper get_value_id_by_field_name_and_value(field_name, value) to resolve the internal value id, then search by that id for accurate results.

Caching & performance
---------------------
- Reuse existing caches:
  - application_level_id (set by from_application)
  - application_fields_json (set by get_application_fields)
  - vl_name_to_vl_id and key_field_value_to_system_id helpers already present
- Optionally cache:
  - supports_rest_search result (boolean) per instance/session
  - contentapi endpoint URL for application
- Bulk operations should chunk filters to avoid long URLs and respect server query limits.

Testing plan (offline-friendly)
-------------------------------
- All tests use unittest + unittest.mock to stub requests.get/post responses.
- Test cases:
  - Field resolution (DisplayName) exact match and case-insensitive match
  - REST supported: simulate 0, 1, and >1 results; test Values List path (value -> valueId)
  - REST unsupported (simulate 404/405): ensure fallback to Content API path works
  - Bulk: mixed results (0, 1, ambiguous); validate chunking behavior and correct mapping of inputs to ids/None; ensure AmbiguousMatch includes details
- Keep tests isolated; pre-populate ArcherInstance caches where appropriate to avoid metadata calls unless the test is specifically testing those calls.

Implementation steps
--------------------
1. Add a new non-printing endpoint discovery method: get_grc_endpoint_url(app_name).
2. Add field resolution helper: _resolve_field_id_by_display_name.
3. Add runtime probe: supports_rest_search.
4. Implement search helpers: _rest_search_record_ids and _contentapi_search_record_ids.
5. Implement public API methods: get_record_id_by_field and get_record_ids_by_field_bulk.
6. Add AmbiguousMatch exception class.
7. Add unit tests in rsa_archer/tests covering the above logic (HTTP mocking).
8. Update README and docstrings with usage notes and examples.

Usage examples
--------------
Single:
```python
ai = ArcherInstance("host", "instance", "user", "pwd").from_application("Application A")
rid = ai.get_record_id_by_field("Application A", "Ticket Number", "INC-12345")
if rid is None:
    print("No record found")
else:
    print("Found record id:", rid)
```
Bulk:
```python
results = ai.get_record_ids_by_field_bulk("Application A", "Ticket Number", ["INC-1", "INC-2", "INC-3"])
# results -> {"INC-1": 101, "INC-2": None, "INC-3": 103}
```

Assumptions & risks
-------------------
- The REST search endpoint's request and response shapes vary by Archer version. Implementation should be defensive and use the fallback when necessary.
- Content API OData responses may expose different JSON keys for the id; implementation must use the discovered endpoint_url to build the id property name (e.g., `{endpoint}_Id`) similar to existing code.
- Without direct Archer access the code will be tested with mocked HTTP responses; small adjustments may be required on first integration with a live Archer instance.

Deliverables
------------
- Document: this file (docs/changes/record-id-lookup.md)
- Code changes (to be implemented after this proposal is reviewed and accepted):
  - Helpers and public methods described above inside `rsa_archer/archer_instance.py`
  - Unit tests under `rsa_archer/tests/`
  - README and docstring updates

Contact / Next steps
--------------------
- I can implement the described changes and tests now. To proceed, allow me to:
  - Add the helpers, methods, tests, and README update to the repository.
  - Run the test suite locally (tests use HTTP mocks; no live server needed).
- If you want me to proceed, I will:
  - Implement the code and tests
  - Open a PR-style commit (or just commit changes) with the updates and tests
  - Provide instructions for how to run the tests and how to validate against a live Archer server
