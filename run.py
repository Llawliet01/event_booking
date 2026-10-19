"""Local dev entrypoint for running the API directly on a Windows host.

Use `python run.py` instead of the `uvicorn` CLI here: the CLI creates its
asyncio event loop (ProactorEventLoop on Windows) before it ever imports
app.main, so setting the event loop policy inside app code runs too late.
Docker and Render invoke uvicorn via its CLI directly (see Dockerfile) since
Linux has no ProactorEventLoop/psycopg3 incompatibility to work around.
"""

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
