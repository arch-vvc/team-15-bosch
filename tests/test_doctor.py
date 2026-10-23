import subprocess
import unittest
from unittest.mock import patch

from incident_zero.doctor import Check, as_report, command_check


class DoctorTests(unittest.TestCase):
    def test_pending_inputs_do_not_report_integration_ready(self):
        report = as_report([Check("python", "pass", "3.11"), Check("schema", "pending", "missing")])
        self.assertFalse(report["ready_for_integration"])

    @patch("incident_zero.doctor.shutil.which", return_value=None)
    def test_missing_executable_is_reported(self, _which):
        self.assertEqual(command_check("docker", ["docker", "info"]).status, "fail")

    @patch("incident_zero.doctor.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 15))
    @patch("incident_zero.doctor.shutil.which", return_value="/bin/docker")
    def test_unresponsive_engine_does_not_hang(self, _which, _run):
        self.assertEqual(command_check("docker", ["docker", "info"]).status, "fail")

    @patch("incident_zero.doctor.subprocess.run", return_value=subprocess.CompletedProcess(["docker"], 1, "", "engine unavailable"))
    @patch("incident_zero.doctor.shutil.which", return_value="/bin/docker")
    def test_engine_failure_is_not_reported_as_success(self, _which, _run):
        result = command_check("docker", ["docker", "info"])
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.detail, "engine unavailable")


if __name__ == "__main__":
    unittest.main()
