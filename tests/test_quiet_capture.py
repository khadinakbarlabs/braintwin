import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from my_actor import cloud_connector as cloud


class QuietCaptureTests(unittest.TestCase):
    def test_selection_before_dispatch_and_no_prompt_replay(self):
        self.assertIsNone(cloud.capture_job('cursor', {'prompt':'Hello there'}))
        job = cloud.capture_job('cursor', {'prompt':'Open the file. I prefer weekly reports on Fridays.', 'cwd': os.getcwd()})
        self.assertIn('Fridays', job['text'])
        self.assertNotIn('Open the file', job['text'])
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, BRAINTWIN_HOME=folder):
            cloud.mark_automatic('cursor', 'Owner', job, 'pending')
            self.assertEqual(cloud.automatic_status('cursor','Owner',job['scope'],job['text']), 'pending')
            self.assertIsNone(cloud.automatic_status('cursor','Other',job['scope'],job['text']))
            self.assertIsNone(cloud.automatic_status('cursor','Owner','other-project',job['text']))
            self.assertNotIn(job['text'], ''.join(p.read_text() for p in Path(folder).rglob('*.json')))

    def test_slow_delivery_does_not_hold_the_host_callback(self):
        import json
        import subprocess
        import sys
        from unittest.mock import patch
        popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, BRAINTWIN_HOME=folder):
            root = Path(folder)
            script = """import pathlib, sys, time
root = pathlib.Path(sys.argv[1])
(root / 'packet').write_bytes(sys.stdin.buffer.read())
while not (root / 'finish').exists():
    time.sleep(.01)
"""
            children = []
            def launch(_command, **options):
                child = popen([sys.executable, '-c', script, folder], **options)
                children.append(child)
                return child
            data = {'prompt':'Open my files. I prefer violet highlights.', 'cwd':folder}
            try:
                with patch.object(cloud.subprocess, 'Popen', side_effect=launch):
                    cloud.dispatch_prompt('cursor', 'Owner', data, Path('/test/bin'))
                    cloud.dispatch_prompt('cursor', 'Owner', data, Path('/test/bin'))
                self.assertEqual(len(children), 1)
                self.assertIsNone(children[0].poll())
            finally:
                (root/'finish').touch()
                for child in children:
                    self.assertEqual(child.wait(timeout=5), 0)
            packet = json.loads((root/'packet').read_bytes())
            self.assertEqual(packet[2]['text'], 'I prefer violet highlights.')


class NativeResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_result_preserves_fact_in_wire_response(self):
        from fastmcp import Client, FastMCP
        from fastmcp.server import create_proxy
        from fastmcp.tools import ToolResult
        from my_actor.native_tools import NativeMemoryResults
        backend = FastMCP('fixture')
        @backend.tool
        def memory_search(query: str, compact: bool = False) -> dict:
            return ToolResult(content='mem_fixture: weekly reports', structured_content={'ok': True})
        proxy = create_proxy(backend, name='test')
        proxy.add_middleware(NativeMemoryResults('Owner', None))
        async with Client(proxy) as client:
            result = await client.call_tool('memory_search', {'query':'reports', 'compact':True})
        self.assertIn('mem_fixture', result.structured_content['text'])

    async def test_hook_and_model_share_one_delivery_in_matching_workspace(self):
        from fastmcp import Client, FastMCP
        from my_actor.native_tools import NativeMemoryResults
        server = FastMCP('fixture')
        sends = []
        @server.tool
        def memory_capture(text: str, scope: str = '') -> dict:
            sends.append(text)
            return {'ok':True}
        server.add_middleware(NativeMemoryResults('Owner','cursor'))
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, BRAINTWIN_HOME=temp):
            job = cloud.capture_job('cursor', {'prompt':'I prefer red headings.', 'cwd':os.getcwd()})
            cloud.mark_automatic('cursor','Owner',job,'pending',turn='1')
            async with Client(server) as c:
                reply = await c.call_tool('memory_capture', {'text':job['text'],'scope':'other-topic'})
                self.assertEqual(reply.structured_content['automaticCapture'], 'pending')
                await c.call_tool('memory_capture', {'text':'Manually save another idea.'})
            self.assertEqual(sends, ['Manually save another idea.'])
