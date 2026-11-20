"""Keep recall usable in clients which display the structured MCP result."""
import os

from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult

from .cloud_connector import automatic_status
from .hooks import _workspace


class NativeMemoryResults(Middleware):
    def __init__(self, owner, client):
        self.owner = owner
        self.client = client

    async def on_call_tool(self, context, call_next):
        name = context.message.name
        args = context.message.arguments or {}
        if (name == 'memory_capture' and args.get('kind', 'user') == 'user'
                and not any(args.get(k) for k in ('dry_run', 'source_url', 'evidence_ids'))):
            _, scope = _workspace({'cwd': os.getcwd()})
            status = automatic_status(self.client, self.owner, scope, args.get('text'))
            if status:
                detail = {'ok': True, 'automaticCapture': status, 'saved': 0,
                          'message': f'Automatic capture status: {status}. No second write was sent.'}
                return ToolResult(content=detail['message'], structured_content=detail)
        result = await call_next(context)
        if name in ('memory_search', 'memory_context') and args.get('compact') and not result.is_error:
            text = '\n'.join(block.text for block in result.content if block.type == 'text')
            if text:
                return ToolResult(content=result.content, meta=result.meta,
                                  structured_content={**(result.structured_content or {}), 'text': text})
        return result
