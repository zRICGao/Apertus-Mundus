"""Local model preparation and Compose startup ordering; no weights are downloaded."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_apertus', PROJECT / 'helpers' / 'prepare_apertus.py')
preparation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparation)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / 'snapshot'
        self.snapshot.mkdir()
        self.token_file = self.root / 'host-token'
        self.env = {'MODEL_ID': 'swiss-ai/Apertus-v1.5-8B', 'MODEL_REVISION': 'revision-one',
                    'HF_HOME': str(self.root / 'models'), 'HOST_HF_TOKEN_FILE': str(self.token_file)}
        self.download = Mock(return_value=str(self.snapshot))

    def prepare(self):
        with redirect_stdout(io.StringIO()) as output:
            preparation.prepare_model(self.download, self.env)
        return output.getvalue()

    def test_reuses_host_login_without_persisting_or_logging_token(self):
        self.token_file.write_text('test-download-credential\n', encoding='utf-8')
        output = self.prepare()
        self.assertEqual(self.download.call_args.kwargs['token'], 'test-download-credential')
        persisted = ''.join(p.read_text() for p in (self.root / 'models').rglob('*') if p.is_file())
        self.assertNotIn('test-download-credential', persisted + output)

    def test_explicit_token_overrides_host_login(self):
        self.token_file.write_text('host-credential', encoding='utf-8')
        self.env['HF_TOKEN'] = 'explicit-download-credential'
        self.prepare()
        self.assertEqual(self.download.call_args.kwargs['token'], 'explicit-download-credential')

    def test_browser_login_alone_fails_before_any_download(self):
        with self.assertRaisesRegex(preparation.PreparationError, 'hf auth login'):
            self.prepare()
        self.download.assert_not_called()

    def test_completed_snapshot_restarts_without_credentials_or_network(self):
        self.env['HF_TOKEN'] = 'test-download-credential'
        self.prepare()
        del self.env['HF_TOKEN']
        self.download.reset_mock()
        self.prepare()
        self.assertTrue(self.download.call_args.kwargs['local_files_only'])
        self.assertNotIn('token', self.download.call_args.kwargs)

    def test_interrupted_download_is_retried_and_never_marked_ready(self):
        self.env['HF_TOKEN'] = 'test-download-credential'
        self.download.side_effect = OSError('interrupted')
        with self.assertRaises(OSError):
            self.prepare()
        self.assertFalse(list((self.root / 'models').rglob('*')))
        self.download.side_effect = None
        self.prepare()
        self.assertEqual(self.download.call_count, 2)

    def test_new_revision_requires_a_new_authenticated_download(self):
        self.env['HF_TOKEN'] = 'test-download-credential'
        self.prepare()
        self.env['MODEL_REVISION'] = 'revision-two'
        self.download.reset_mock()
        self.prepare()
        self.assertEqual(self.download.call_args.kwargs['revision'], 'revision-two')
        self.assertIn('token', self.download.call_args.kwargs)


@unittest.skipUnless(shutil.which('docker'), 'Docker Compose CLI is not installed')
class ComposeTests(unittest.TestCase):
    def config(self, local=True, gpu=False, overrides=None):
        docker_config = os.environ.get('DOCKER_CONFIG', str(Path.home() / '.docker'))
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('HF_', 'HOST_HF_', 'LLM_', 'LOCAL_', 'APERTUS_'))
               and key not in {'HOME', 'USERPROFILE', 'XDG_CACHE_HOME'}}
        env.update({'DOCKER_CONFIG': docker_config, 'HOME': '/home/judge', **(overrides or {})})
        cmd = ['docker', 'compose', '--env-file', '.env.docker.example', '-f', 'compose.yaml']
        if local:
            cmd += ['-f', 'compose.local.yaml', '--profile', 'local']
        if gpu:
            cmd += ['-f', 'compose.helpers-gpu.yaml']
        result = subprocess.run(cmd + ['config', '--format', 'json'], cwd=PROJECT,
                                env=env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)['services']

    def test_local_chain_waits_for_download_and_health_without_inference_key(self):
        for gpu in (False, True):
            with self.subTest(gpu=gpu):
                services = self.config(gpu=gpu, overrides={'HF_TOKEN': 'test-download-credential'})
                app, llm, prepare = (services[name] for name in ('app', 'llm', 'prepare-model'))
                self.assertEqual(app['environment']['LLM_BASE_URL'], 'http://llm:8000/v1')
                self.assertEqual(app['environment']['LLM_API_KEY'], '')
                self.assertEqual(app['depends_on']['llm']['condition'], 'service_healthy')
                self.assertEqual(llm['depends_on']['prepare-model']['condition'], 'service_completed_successfully')
                self.assertEqual(llm['command'][llm['command'].index('--revision') + 1],
                                 prepare['environment']['MODEL_REVISION'])
                self.assertNotIn('HF_TOKEN', app['environment'])
                self.assertNotIn('HF_TOKEN', llm['environment'])
                self.assertEqual(prepare['environment']['HF_TOKEN'], 'test-download-credential')
                self.assertFalse(llm.get('ports'))

    def test_host_login_directory_resolves_on_linux_windows_and_custom_paths(self):
        for overrides, expected in (
            ({}, '/home/judge/.cache/huggingface'),
            ({'USERPROFILE': 'C:/Users/judge'}, 'C:/Users/judge/.cache/huggingface'),
            ({'XDG_CACHE_HOME': '/cache'}, '/cache/huggingface'),
            ({'HF_HOME': '/custom-hf'}, '/custom-hf'),
            ({'HOST_HF_HOME': '/mounted-hf', 'HF_HOME': '/custom-hf'}, '/mounted-hf'),
        ):
            with self.subTest(overrides=overrides):
                volumes = self.config(overrides=overrides)['prepare-model']['volumes']
                login = next(v for v in volumes if v['target'] == '/host-huggingface')
                self.assertEqual(login['source'].replace('\\', '/'), expected)
                self.assertTrue(login['read_only'])

    def test_remote_mode_still_accepts_external_endpoint(self):
        for gpu in (False, True):
            services = self.config(local=False, gpu=gpu, overrides={
                'LLM_NAME': 'external-apertus', 'LLM_BASE_URL': 'https://example.test/v1',
                'LLM_API_KEY': 'test-inference-credential'})
            self.assertNotIn('llm', services)
            self.assertNotIn('prepare-model', services)
            self.assertEqual(services['app']['environment']['LLM_BASE_URL'], 'https://example.test/v1')


if __name__ == '__main__':
    unittest.main()
