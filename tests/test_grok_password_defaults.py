import unittest

from platforms.grok.plugin import GrokPlatform


class GrokPasswordDefaultsTests(unittest.TestCase):
    def test_prepare_registration_password_generates_default_when_missing(self):
        platform = GrokPlatform()

        resolved = platform._prepare_registration_password(None)

        self.assertIsInstance(resolved, str)
        self.assertTrue(resolved)
        self.assertGreaterEqual(len(resolved), 8)

    def test_prepare_registration_password_preserves_explicit_value(self):
        platform = GrokPlatform()

        resolved = platform._prepare_registration_password("MySecret123!")

        self.assertEqual(resolved, "MySecret123!")


if __name__ == "__main__":
    unittest.main()
