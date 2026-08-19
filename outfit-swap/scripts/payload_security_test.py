"""Tests for credential-only redaction of diagnostic payloads."""

from __future__ import annotations

import unittest

from scripts import payload_security


class PayloadSecurityTest(unittest.TestCase):
    def test_preserves_business_payloads_verbatim(self) -> None:
        data_url = "data:image/png;base64,aGVsbG8="
        prompt = "Keep every pearl button and reproduce the garment exactly."
        payload = {
            "messages": [{"content": [{"type": "image_url", "image_url": {"url": data_url}},
                                        {"type": "text", "text": prompt}]}],
            "request_body": {"prompt": prompt, "image_base64": "aGVsbG8="},
        }

        self.assertEqual(payload_security.redact_credentials(payload), payload)

    def test_recursively_redacts_credentials_and_authorization(self) -> None:
        payload = {
            "headers": {
                "Authorization": "Bearer ark-secret",
                "X-API-Key": "alternate-secret",
                "Cookie": "session=private",
                "Content-Type": "application/json",
            },
            "body": {
                "api_key": "body-secret",
                "nested": [{"access_token": "access-secret"}],
                "prompt": "send to https://example.test/run?api_key=url-secret&mode=qc",
            },
        }

        redacted = payload_security.redact_credentials(payload)

        self.assertEqual(redacted["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(redacted["headers"]["X-API-Key"], "[REDACTED]")
        self.assertEqual(redacted["headers"]["Cookie"], "[REDACTED]")
        self.assertEqual(redacted["headers"]["Content-Type"], "application/json")
        self.assertEqual(redacted["body"]["api_key"], "[REDACTED]")
        self.assertEqual(redacted["body"]["nested"][0]["access_token"], "[REDACTED]")
        self.assertNotIn("url-secret", redacted["body"]["prompt"])
        self.assertIn("mode=qc", redacted["body"]["prompt"])

    def test_redacts_credential_text_without_redacting_data_urls(self) -> None:
        value = (
            "before Authorization: Bearer header-secret after\n"
            "payload=data:image/webp;base64,QUJDRA==\n"
            "refresh_token=refresh-secret"
        )

        redacted = payload_security.redact_credentials(value)

        self.assertNotIn("header-secret", redacted)
        self.assertNotIn("refresh-secret", redacted)
        self.assertIn("before", redacted)
        self.assertIn("after", redacted)
        self.assertIn("data:image/webp;base64,QUJDRA==", redacted)

    def test_serialized_request_body_and_bytes_redact_only_credentials(self) -> None:
        body = (
            '{"ARK_API_KEY":"json-secret","prompt":"full prompt",'
            '"image":"data:image/png;base64,QUJD"}'
        )

        redacted_text = payload_security.redact_credentials(body)
        redacted_bytes = payload_security.redact_credentials(body.encode("utf-8"))

        self.assertNotIn("json-secret", redacted_text)
        self.assertIn('"prompt":"full prompt"', redacted_text)
        self.assertIn("data:image/png;base64,QUJD", redacted_text)
        self.assertEqual(redacted_bytes, redacted_text.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
