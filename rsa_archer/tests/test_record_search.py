import unittest
from unittest.mock import patch, Mock

from rsa_archer.record_search import RecordSearcher, AmbiguousMatch


class FakeArcher:
    """Minimal fake ArcherInstance compatible with RecordSearcher tests."""

    def __init__(self):
        # content of application_fields_json must include name->id and id->{...} entries
        self.application_fields_json = {}
        self.application_level_id = "100"
        self.header = {"Authorization": "Archer session-id=FAKE"}
        self.api_url_base = "https://fake/rsaarcher/api/"
        self.content_api_url_base = "https://fake/rsaarcher/contentapi/"

    def from_application(self, app_name: str):
        # noop for tests; caller will set application_fields_json before calling
        return self

    def get_value_id_by_field_name_and_value(self, field_name: str, value: str):
        # test will override or set application_fields_json such that tests expecting values list will work
        # Default: no match
        return []


class MockResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class TestRecordSearcher(unittest.TestCase):
    def setUp(self):
        self.arch = FakeArcher()
        self.rs = RecordSearcher(self.arch)

    @patch("rsa_archer.record_search.requests.post")
    def test_rest_single_result(self, mock_post):
        # Prepare application field mapping
        self.arch.application_fields_json = {"Ticket Number": 10, 10: {"Type": 1, "FieldId": 10}}
        # Probe response (first POST) -> supported
        probe_resp = MockResponse(status_code=200, json_data={})
        # Search response (second POST) -> one result with RequestedObject Id
        search_payload = [
            {"RequestedObject": {"Id": 555}}
        ]
        search_resp = MockResponse(status_code=200, json_data=search_payload)
        mock_post.side_effect = [probe_resp, search_resp]

        rid = self.rs.get_record_id_by_field("App", "Ticket Number", "SOME-VALUE")
        self.assertEqual(rid, 555)

    @patch("rsa_archer.record_search.requests.post")
    def test_rest_no_result(self, mock_post):
        self.arch.application_fields_json = {"Ticket Number": 10, 10: {"Type": 1, "FieldId": 10}}
        probe_resp = MockResponse(status_code=200, json_data={})
        search_resp = MockResponse(status_code=200, json_data=[])  # empty list
        mock_post.side_effect = [probe_resp, search_resp]

        rid = self.rs.get_record_id_by_field("App", "Ticket Number", "NOPE")
        self.assertIsNone(rid)

    @patch("rsa_archer.record_search.requests.post")
    def test_rest_multiple_results_raises(self, mock_post):
        self.arch.application_fields_json = {"Ticket Number": 10, 10: {"Type": 1, "FieldId": 10}}
        probe_resp = MockResponse(status_code=200, json_data={})
        search_resp = MockResponse(status_code=200, json_data=[
            {"RequestedObject": {"Id": 1}},
            {"RequestedObject": {"Id": 2}},
        ])
        mock_post.side_effect = [probe_resp, search_resp]

        with self.assertRaises(AmbiguousMatch) as cm:
            self.rs.get_record_id_by_field("App", "Ticket Number", "DUP")
        exc = cm.exception
        # details may be None; check message
        self.assertIn("Multiple records found", str(exc))

    @patch("rsa_archer.record_search.requests.post")
    def test_values_list_rest_search(self, mock_post):
        # Simulate field being a values list; application_fields_json must include id->info
        self.arch.application_fields_json = {"Status": 20, 20: {"Type": 4, "FieldId": 20}}
        # make get_value_id_by_field_name_and_value return an internal id
        def fake_get_value(field_name, value):
            if field_name == "Status" and value == "Open":
                return [999]
            return []
        self.arch.get_value_id_by_field_name_and_value = fake_get_value

        probe_resp = MockResponse(status_code=200, json_data={})
        search_resp = MockResponse(status_code=200, json_data=[{"RequestedObject": {"Id": 777}}])
        mock_post.side_effect = [probe_resp, search_resp]

        rid = self.rs.get_record_id_by_field("App", "Status", "Open")
        self.assertEqual(rid, 777)

    @patch("rsa_archer.record_search.requests.post")
    @patch("rsa_archer.record_search.requests.get")
    def test_contentapi_fallback(self, mock_get, mock_post):
        # Simulate REST probe returning 404 (unsupported)
        probe_resp = MockResponse(status_code=404, json_data={})
        mock_post.side_effect = [probe_resp]

        # Setup application fields
        self.arch.application_fields_json = {"Ticket Number": 10, 10: {"Type": 1, "FieldId": 10}}

        # Content API discovery: return endpoint list with exact name match
        mock_get.side_effect = [
            MockResponse(status_code=200, json_data={"value": [{"name": "App", "url": "Endpoint"}]}),
            # Content API search response
            MockResponse(status_code=200, json_data={"value": [{"Endpoint_Id": 222}]})
        ]

        rid = self.rs.get_record_id_by_field("App", "Ticket Number", "XYZ")
        self.assertEqual(rid, 222)

    def test_resolve_field_case_insensitive(self):
        # Ensure DisplayName matching is case-insensitive
        self.arch.application_fields_json = {"Ticket Number": 10, 10: {"Type": 1, "FieldId": 10}}
        fid = self.rs._resolve_field_id_by_display_name("App", "ticket number")
        self.assertEqual(fid, 10)


if __name__ == "__main__":
    unittest.main()
