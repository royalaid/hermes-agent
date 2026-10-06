"""Manual restoration requires a reachable listener owned by the new process tree."""
import contextlib
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psutil

from hermes_cli import main_dashboard
from hermes_cli import _subprocess_compat

COMMAND = ["serve", "--host", "127.0.0.1", "--port", "8300"]


def listener(pid):
    return SimpleNamespace(pid=pid, status=psutil.CONN_LISTEN,
                           laddr=SimpleNamespace(ip="127.0.0.1", port=8300))


class TestDashboardRestoreReadiness(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        home = stack.enter_context(tempfile.TemporaryDirectory(prefix="hermes-readiness-unit-"))
        stack.enter_context(patch.dict(os.environ, HERMES_HOME=home))
        self.proc = MagicMock(pid=7001)
        self.proc.poll.return_value = None
        self.now = 0.0
        self.slept = []
        self.output = io.StringIO()
        stack.enter_context(contextlib.redirect_stdout(self.output))
        sock = MagicMock()
        sock.__enter__.return_value.getpeername.return_value = ("127.0.0.1", 8300)
        tree = MagicMock(pid=7001)
        tree.children.return_value = [SimpleNamespace(pid=7002)]
        stack.enter_context(patch.object(main_dashboard.subprocess, "Popen", return_value=self.proc))
        stack.enter_context(patch.object(main_dashboard.time, "monotonic", side_effect=lambda: self.now))
        stack.enter_context(patch.object(main_dashboard.time, "sleep", side_effect=self.sleep))
        self.connect = stack.enter_context(patch("socket.create_connection", return_value=sock))
        stack.enter_context(patch.object(psutil, "Process", return_value=tree))
        self.connections = stack.enter_context(patch.object(psutil, "net_connections"))
        self.kill = stack.enter_context(patch.object(_subprocess_compat, "kill_process_tree"))

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def test_occupied_port_fails_and_kills_only_spawned_tree(self):
        self.connections.return_value = [listener(9001)]
        self.assertEqual(main_dashboard._respawn_dashboard_processes([COMMAND]), [COMMAND])
        output = self.output.getvalue()
        self.assertNotIn("restarted:", output)
        self.assertIn("failed to restart", output)
        self.assertIn("restore not ready", output)
        self.assertIn("8300", output)
        self.assertIn("owner PID 9001", output)
        self.assertEqual(self.now, 30.0)
        self.assertEqual(set(self.slept), {0.5})
        self.kill.assert_called_once_with(self.proc)

    def test_early_exit_reports_code_without_waiting(self):
        self.proc.poll.return_value = 17
        self.assertEqual(main_dashboard._respawn_dashboard_processes([COMMAND]), [COMMAND])
        output = self.output.getvalue()
        self.assertIn("failed to restart", output)
        self.assertIn("code 17", output)
        self.assertNotIn("restarted:", output)
        self.assertEqual(self.slept, [])
        self.connect.assert_not_called()

    def test_slow_descendant_succeeds_only_after_tcp_connect(self):
        self.connections.return_value = [listener(7002)]
        # Ownership alone is insufficient; TCP becomes available after the old grace window.
        self.connect.side_effect = [ConnectionRefusedError(), ConnectionRefusedError(),
                                    ConnectionRefusedError(), self.connect.return_value]
        self.assertEqual(main_dashboard._respawn_dashboard_processes([COMMAND]), [])
        self.assertEqual(self.now, 1.5)
        self.assertIn("restarted:", self.output.getvalue())
        self.kill.assert_not_called()
