from dotenv import load_dotenv

load_dotenv()  # searches this dir and parents -- picks up the repo-root .env

import asyncio

from agent.core.server import main

if __name__ == "__main__":
    asyncio.run(main())
