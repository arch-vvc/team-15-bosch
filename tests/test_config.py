import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from incident_zero.config import model_settings
from incident_zero.model import ResponsesModel


class ConfigTests(unittest.TestCase):
    def test_dotenv_is_data_and_environment_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('INCIDENT_MODEL="from-file"\nOPENAI_API_KEY=literal-$(never-execute)\nUNRELATED_SETTING=ignored\n')
            with patch.dict(os.environ, {"INCIDENT_MODEL": "from-environment"}, clear=True):
                settings = model_settings(path)
            self.assertEqual(settings["INCIDENT_MODEL"], "from-environment")
            self.assertEqual(settings["OPENAI_API_KEY"], "literal-$(never-execute)")
            self.assertNotIn("UNRELATED_SETTING", settings)

    def test_missing_cloud_credentials_fails_before_network(self):
        with patch("incident_zero.model.model_settings", return_value={}):
            with self.assertRaises(ValueError):
                ResponsesModel()

    def test_cloud_key_cannot_be_redirected_to_an_external_gateway(self):
        with patch("incident_zero.model.model_settings", return_value={"OPENAI_API_KEY": "test"}):
            with self.assertRaises(ValueError):
                ResponsesModel(base_url="https://untrusted.example/v1")


if __name__ == "__main__":
    unittest.main()
