"""Deliver one selected statement independently of the host's chat event."""
import asyncio
import json
import sys
from pathlib import Path


def main():
    client, owner, job, turn, cli = json.load(sys.stdin)
    from .cloud_connector import deliver, mark_automatic, record_result
    from .connections import current_target
    if current_target(client, Path(cli)) != ('apify', owner):
        mark_automatic(client, owner, job, 'disconnected', turn)
        return
    receipt = asyncio.run(deliver(owner, job))
    mark_automatic(client, owner, job, receipt['outcome'], turn)
    record_result(client, owner, receipt)


if __name__ == '__main__':
    main()
