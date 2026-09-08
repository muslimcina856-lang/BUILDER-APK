import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class NoInternalTimeoutTests(unittest.TestCase):
    def test_run_cmd_default_has_no_timeout(self):
        source = (ROOT / 'server' / 'builder.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'run_cmd')
        defaults = dict(zip([a.arg for a in fn.args.args[-len(fn.args.defaults):]], fn.args.defaults))
        self.assertIn('timeout', defaults)
        self.assertIsInstance(defaults['timeout'], ast.Constant)
        self.assertIsNone(defaults['timeout'].value)

    def test_production_run_cmd_calls_have_no_finite_timeout(self):
        for rel in ('server/builder.py', 'server/worker.py'):
            source = (ROOT / rel).read_text(encoding='utf-8')
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
                if name != 'run_cmd':
                    continue
                for kw in node.keywords:
                    if kw.arg == 'timeout':
                        self.assertIsInstance(kw.value, ast.Constant, f'{rel}:{node.lineno}')
                        self.assertIsNone(kw.value.value, f'finite timeout in {rel}:{node.lineno}')

    def test_network_downloads_have_no_total_timeout(self):
        worker = (ROOT / 'server' / 'worker.py').read_text(encoding='utf-8')
        self.assertNotRegex(worker, r'urlopen\([^\n]*timeout\s*=\s*\d+')
        self.assertNotRegex(worker, r'ClientTimeout\(total\s*=\s*\d+')

    def test_github_job_uses_hosted_runner_maximum(self):
        workflow = (ROOT / '.github' / 'workflows' / 'build.yml').read_text(encoding='utf-8')
        self.assertRegex(workflow, r'(?m)^\s*timeout-minutes:\s*360\s*$')


if __name__ == '__main__':
    unittest.main()
