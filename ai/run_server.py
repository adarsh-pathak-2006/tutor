import asyncio
import sys

import uvicorn

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

print(
    "Event loop policy:",
    type(asyncio.get_event_loop_policy()).__name__,
    flush=True,
)

if __name__ == "__main__":
    config = uvicorn.Config(
        "tutor_ai.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=8001,
        loop="asyncio",
    )

    server = uvicorn.Server(config)
    asyncio.run(server.serve())
