"""Offline RK route checks; nmcli and ip are replaced with isolated fakes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
UUID = '11111111-2222-3333-4444-555555555555'
ROUTE = '100.67.196.150/32 192.168.168.100 50 src=192.168.168.168'
MOCK = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
state_path = Path(os.environ['RK_ROUTE_TEST_STATE'])
s = json.loads(state_path.read_text())
a = sys.argv[1:]
tool = Path(sys.argv[0]).name
s['calls'].append([tool, *a])
state_path.write_text(json.dumps(s))
if tool == 'nmcli':
    if a[:3] == ['connection', 'modify', 'uuid']:
        assert len(a) == 6 and a[3] == s['uuid'] and a[4] == '+ipv4.routes', a
        if os.environ.get('RK_ROUTE_FAIL_SAVE'):
            sys.exit(9)
        if a[5] not in s['profile']['routes']:
            s['profile']['routes'].append(a[5])
        state_path.write_text(json.dumps(s))
    elif a == ['--wait', '20', 'device', 'reapply', 'eth0']:
        if os.environ.get('RK_ROUTE_FAIL_REAPPLY'):
            sys.exit(10)
    else:
        raise AssertionError('Unexpected nmcli mutation: ' + repr(a))
elif tool == 'ip':
    assert a == ['-4', 'route', 'get', '100.67.196.150'], a
    print(os.environ.get('RK_ROUTE_RESULT',
        '100.67.196.150 via 192.168.168.100 dev eth0 src 192.168.168.168 metric 50'))
else:
    raise AssertionError(tool)
'''


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name in ['ip', 'nmcli']:
            path = root / name
            path.write_text(MOCK)
            path.chmod(0o755)
        self.state_path = root / 'state.json'
        self.original_profile = {
            'method': 'auto', 'route_metric': 50,
            'addresses': ['192.168.168.168/24', '192.168.144.144/24'],
            'dns_search': ['~.'], 'routes': ['192.0.2.0/24 192.168.144.100'],
        }
        self.state_path.write_text(json.dumps({
            'uuid': UUID, 'profile': self.original_profile, 'calls': []}))
        self.env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                        RK_ROUTE_TEST_STATE=str(self.state_path))

    def load(self):
        return json.loads(self.state_path.read_text())

    def run_route(self, check=True):
        return subprocess.run(['bash', str(HERE / 'configure-route.sh'), UUID],
                              env=self.env, text=True, capture_output=True, check=check)

    def test_add_preserves_cpe_addresses_dns_and_other_routes(self):
        self.run_route()
        expected = dict(self.original_profile, routes=self.original_profile['routes'] + [ROUTE])
        self.assertEqual(self.load()['profile'], expected)

    def test_repeated_install_has_one_mac_route(self):
        self.run_route()
        self.run_route()
        self.assertEqual(self.load()['profile']['routes'].count(ROUTE), 1)

    def test_save_failure_does_not_reapply(self):
        self.env['RK_ROUTE_FAIL_SAVE'] = '1'
        result = self.run_route(check=False)
        self.assertEqual(result.returncode, 9)
        self.assertEqual(len(self.load()['calls']), 1)
        self.assertEqual(self.load()['profile'], self.original_profile)

    def test_reapply_failure_does_not_disconnect_or_restart(self):
        self.env['RK_ROUTE_FAIL_REAPPLY'] = '1'
        result = self.run_route(check=False)
        self.assertEqual(result.returncode, 10)
        self.assertEqual(len(self.load()['calls']), 2)
        self.assertEqual(self.load()['calls'][-1],
                         ['nmcli', '--wait', '20', 'device', 'reapply', 'eth0'])

    def test_wrong_source_is_reported(self):
        self.env['RK_ROUTE_RESULT'] = (
            '100.67.196.150 via 192.168.168.100 dev eth0 src 192.168.66.137')
        result = self.run_route(check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('current route/source differs', result.stderr)

    def test_wrong_next_hop_is_reported(self):
        self.env['RK_ROUTE_RESULT'] = (
            '100.67.196.150 via 192.168.66.1 dev eth0 src 192.168.168.168')
        result = self.run_route(check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('current route/source differs', result.stderr)


if __name__ == '__main__':
    unittest.main()
