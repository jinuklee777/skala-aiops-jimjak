import json
import unittest

from collector import api
from collector.tests.helpers import SERVICE_KEY, fixture

ENCODED_KEY = "test%2BKey%2Fabc%3D%3D"


def response_with(items, **body):
    return json.dumps({"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                                    "body": {"items": items, **body}}}).encode()


class ParsePageTest(unittest.TestCase):
    def test_items_as_list(self):
        page = api.parse_page(fixture("arrivals_p1.json"))
        self.assertEqual((len(page.items), page.total_count, page.result_code), (3, 5, "00"))
        self.assertEqual(page.items[0]["flightId"], "SC8003")

    def test_other_item_shapes(self):
        one = {"flightId": "KE1"}
        self.assertEqual(api.parse_page(response_with({"item": one}, totalCount="1")).items, [one])
        self.assertEqual(api.parse_page(response_with({"item": [one, one]})).items, [one, one])
        self.assertEqual(api.parse_page(response_with("", totalCount=0)).items, [])

    def test_xml_body(self):
        page = api.parse_page(
            b"<response><header><resultCode>00</resultCode></header><body><items><item>"
            b"<flightId>KE1</flightId><remark/></item></items><totalCount>1</totalCount></body></response>")
        self.assertEqual((page.items, page.total_count), ([{"flightId": "KE1", "remark": ""}], 1))

    def test_gateway_errors(self):
        with self.assertRaises(api.ApiError) as quota:
            api.parse_page(fixture("error_quota.xml"))
        self.assertEqual((quota.exception.code, quota.exception.retryable), ("22", False))
        with self.assertRaises(api.ApiError) as per_second:
            api.parse_page(fixture("error_quota.xml").replace(b">22<", b">23<"))
        self.assertTrue(per_second.exception.retryable)

    def test_json_error_code(self):
        body = json.dumps({"response": {"header": {"resultCode": "30",
                                                   "resultMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}}}).encode()
        with self.assertRaises(api.ApiError) as error:
            api.parse_page(body)
        self.assertEqual((error.exception.code, error.exception.retryable), ("30", False))

    def test_unreadable_bodies(self):
        for body in (b"", b"Unauthorized", b"<html><body>Bad Gateway</body></html>", b'{"foo": 1}'):
            with self.subTest(body=body), self.assertRaises(api.ApiError):
                api.parse_page(body)


class InterpretTest(unittest.TestCase):
    def outcome(self, status, body=None, error=None):
        return api.interpret(api.Response(status, {}, body, 0, error))

    def test_accepts_a_parsed_200(self):
        self.assertEqual(len(self.outcome(200, fixture("arrivals_p1.json")).page.items), 3)

    def test_decides_whether_to_retry(self):
        cases = [(None, None, "URLError: timed out", True), (500, b"Internal Server Error", None, True),
                 (429, b"Too Many Requests", None, True), (401, b"Unauthorized", None, False),
                 (200, b"garbage", None, True), (200, fixture("error_quota.xml"), None, False)]
        for status, body, error, retryable in cases:
            with self.subTest(status=status, body=body):
                outcome = self.outcome(status, body, error)
                self.assertIsNone(outcome.page)
                self.assertEqual(outcome.retryable, retryable)


class ServiceKeyTest(unittest.TestCase):
    def test_either_key_form_is_encoded_once(self):
        for key in (SERVICE_KEY, ENCODED_KEY):
            with self.subTest(key=key):
                url = api.build_url("https://x", "op", [("type", "json")], key)
                self.assertEqual(url, f"https://x/op?serviceKey={ENCODED_KEY}&type=json")

    def test_redact_hides_every_form(self):
        redacted = api.redact(f"GET https://x/op?serviceKey={ENCODED_KEY} raw={SERVICE_KEY}", SERVICE_KEY)
        self.assertEqual(redacted, "GET https://x/op?serviceKey=*** raw=***")
