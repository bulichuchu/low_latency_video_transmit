"""Offline regression checks; never invoke the host's real iptables."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
MOCK_IPTABLES = r'''#!/usr/bin/env python3
import json, os, shlex, sys
from pathlib import Path
p = Path(os.environ['VIDEO_TEST_STATE'])
s = json.loads(p.read_text())
a = sys.argv[1:]
assert a[:2] == ['-w', '5'], a
a = a[2:]
assert a[0] == '-t', a
table, op, chain, *args = a[1:]
key = table + '/' + chain
assert key in s['rules'], key
rules = s['rules'][key]
if os.environ.get('VIDEO_TEST_READ_FAILURE') and op == '-S':
    print('simulated backend error', file=sys.stderr)
    sys.exit(4)
if os.environ.get('VIDEO_TEST_CHECK_FAILURE') and op == '-C':
    print('simulated lock/backend error', file=sys.stderr)
    sys.exit(4)
if op == '-S':
    print('-P ' + chain + ' ' + s['policies'][key])
    for rule in rules:
        print(shlex.join(['-A', chain, *rule]))
elif op == '-C':
    sys.exit(0 if args in rules else 1)
elif op == '-D':
    rules.remove(args)
elif op == '-I':
    assert args[0] == '1'
    rules.insert(0, args[1:])
else:
    raise AssertionError('unexpected mutation: ' + op)
if op in ('-I', '-D'):
    s['writes'].append([table, op, chain, args])
    p.write_text(json.dumps(s))
'''

BASE = {
    'filter/FORWARD': [
        ['-i', 'wt0', '-j', 'NETBIRD-RT-FWD-IN'],
        ['-m', 'mark', '--mark', '0x1bd20', '-j', 'ACCEPT'],
        ['-o', 'wt0', '-j', 'NETBIRD-RT-FWD-OUT'],
        ['-i', 'wt0', '-j', 'DROP'],
        ['-j', 'DOCKER-USER'], ['-j', 'DOCKER-FORWARD'],
    ],
    'nat/POSTROUTING': [
        ['-j', 'NETBIRD-RT-NAT'],
        ['-s', '172.17.0.0/16', '!', '-o', 'docker0', '-j', 'MASQUERADE'],
        ['-o', 'wlan0', '-j', 'MASQUERADE'],
        ['-o', 'wlan0', '-j', 'MASQUERADE'],
    ],
}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        fake = root / 'iptables'
        fake.write_text(MOCK_IPTABLES)
        fake.chmod(0o755)
        self.state_path = root / 'state.json'
        self.save({'rules': BASE, 'policies': {
            'filter/FORWARD': 'DROP', 'nat/POSTROUTING': 'ACCEPT'}, 'writes': []})
        self.env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                        VIDEO_TEST_STATE=str(self.state_path))

    def load(self):
        return json.loads(self.state_path.read_text())

    def save(self, state):
        self.state_path.write_text(json.dumps(state))

    def run_rules(self, action='apply', check=True):
        return subprocess.run(['bash', str(HERE / 'qnbot-rk-video'), action],
                              env=self.env, text=True, capture_output=True, check=check)

    def assert_restored(self):
        state = self.load()
        forward = state['rules']['filter/FORWARD']
        nat = state['rules']['nat/POSTROUTING']
        self.assertIn('qnbot-rk-video-reply', forward[0])
        self.assertIn('ESTABLISHED', forward[0])
        self.assertIn('qnbot-rk-video-out', forward[1])
        self.assertIn('qnbot-rk-video-nat', nat[0])
        self.assertIn('MASQUERADE', nat[0])
        self.assertEqual(forward[2:], BASE['filter/FORWARD'])
        self.assertEqual(nat[1:], BASE['nat/POSTROUTING'])
        self.assertEqual(state['policies']['filter/FORWARD'], 'DROP')
        for rule in [forward[0], forward[1], nat[0]]:
            self.assertIn('udp', rule)
            self.assertIn('5004', rule)
            self.assertIn('192.168.168.168/32', rule)
            self.assertIn('100.67.196.150/32', rule)

    def test_missing_rules_restored_and_repeat_does_not_mutate(self):
        self.run_rules()
        self.assert_restored()
        before = self.load()
        self.run_rules()
        self.assertEqual(self.load(), before)

    def test_rules_lost_after_firewall_reload(self):
        self.run_rules()
        state = self.load()
        state['rules'] = BASE
        self.save(state)
        self.run_rules()
        self.assert_restored()

    def test_rules_displaced_below_netbird_drop(self):
        self.run_rules()
        state = self.load()
        for key, count in [('filter/FORWARD', 2), ('nat/POSTROUTING', 1)]:
            rules = state['rules'][key]
            state['rules'][key] = rules[count:] + rules[:count]
        self.save(state)
        self.run_rules()
        self.assert_restored()

    def test_legacy_manual_rules_adopted_without_duplicates(self):
        self.run_rules()
        state = self.load()
        for key, count in [('filter/FORWARD', 2), ('nat/POSTROUTING', 1)]:
            for rule in state['rules'][key][:count]:
                i = rule.index('comment') - 1
                del rule[i:i + 4]
        self.save(state)
        self.run_rules()
        self.assert_restored()

    def test_remove_keeps_other_services_and_policy(self):
        self.run_rules()
        self.run_rules('remove')
        self.assertEqual(self.load()['rules'], BASE)
        self.assertEqual(self.load()['policies']['filter/FORWARD'], 'DROP')

    def test_read_error_fails_without_writing(self):
        before = self.load()
        self.env['VIDEO_TEST_READ_FAILURE'] = '1'
        result = self.run_rules(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('simulated backend error', result.stderr)
        self.assertEqual(self.load(), before)

    def test_rule_check_error_is_not_treated_as_missing(self):
        self.run_rules()
        before = self.load()
        self.env['VIDEO_TEST_CHECK_FAILURE'] = '1'
        result = self.run_rules(check=False)
        self.assertEqual(result.returncode, 4)
        self.assertIn('simulated lock/backend error', result.stderr)
        self.assertEqual(self.load(), before)


if __name__ == '__main__':
    unittest.main()
